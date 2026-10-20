"""``permissions.federation`` 策略快照与 grant 校验的纯单元测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.resource_uri import thread_resource_uri
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_GRANT_AUDIENCE_MISMATCH,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_GRANT_INVALID_SIGNATURE,
    FEDERATION_GRANT_KIND_MISMATCH,
    FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
    FEDERATION_GRANT_PATH_MISMATCH,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    ContextGrantTarget,
    FederationGrant,
    ForkGrantTarget,
    GrantTarget,
    OperationKind,
    ResourceNavigationGrantTarget,
    TextMutationGrantTarget,
    decode_grant_target,
    required_wait_grant_lifetime,
    sign_grant,
    validate_grant_operation,
    verify_grant,
)
from app.gateway.federation.identity import (
    FederationPeerIdentity,
    load_or_create_signing_key,
    public_key_pem,
)
from app.gateway.federation.policy import (
    FederationPolicyStore,
    default_policy_payload,
    normalize_policy,
)
from app.gateway.federation.rpc import FederationRpcService
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.support.communication import TARGET_SESSION, WORKSPACE_ID


def test_default_policy_allows_all_core_capabilities() -> None:
    """内置默认值：default_effect=allow、rules=[]、hardening 关闭。"""

    store = FederationPolicyStore()
    snapshot = store.snapshot
    assert snapshot.default_effect == "allow"
    assert snapshot.rules == ()
    assert snapshot.hardening_enabled is False
    for capability in ("discovery", "send", "read", "wait", "reply", "transit", "fork_source", "fork_target", "file_write"):
        assert snapshot.evaluate(
            capability=capability,  # type: ignore[arg-type]
            origin_gateway_id="gateway_peer",
            principal_ref="prn_x",
        )


@pytest.mark.parametrize("rules, expected", [
    ([], False),
    ([{"capability": "read", "effect": "allow", "principal_ref": "peer-principal"}], False),
    ([{"capability": "managed_workspace_admin", "effect": "allow"}], False),
    ([{"capability": "managed_workspace_admin", "effect": "allow", "origin_gateway_id": "peer"}], False),
    ([{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": "other"}], False),
    ([{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": "peer-principal"}], True),
    ([{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": "peer-principal"},
      {"capability": "managed_workspace_admin", "effect": "deny", "principal_ref": "peer-principal"}], False),
])
def test_workspace_admin_requires_explicit_authenticated_peer_principal(rules, expected) -> None:
    snapshot = normalize_policy({"default_effect": "allow", "rules": rules})
    assert snapshot.evaluate(capability="managed_workspace_admin", origin_gateway_id="peer", principal_ref="peer-principal") is expected


def test_publish_advances_revision_atomically() -> None:
    store = FederationPolicyStore()
    base = store.snapshot.revision
    published = store.publish({"rules": [{"capability": "read", "effect": "deny"}]})
    assert published.revision == base + 1
    assert (
        store.snapshot.evaluate(
            capability="read", origin_gateway_id="peer", principal_ref="prn"
        )
        is False
    )
    # 未声明的能力仍按默认 allow。
    assert (
        store.snapshot.evaluate(
            capability="send", origin_gateway_id="peer", principal_ref="prn"
        )
        is True
    )


def test_invalid_candidate_is_rejected_without_partial_effect() -> None:
    store = FederationPolicyStore()
    before = store.snapshot
    with pytest.raises(ValueError):
        store.publish({"rules": [{"capability": "teleport", "effect": "deny"}]})
    assert store.snapshot is before
    assert store.snapshot.revision == before.revision


def test_normalize_rejects_non_boolean_hardening() -> None:
    with pytest.raises(TypeError):
        normalize_policy({"hardening_enabled": "yes"})


def test_default_policy_payload_matches_design_contract() -> None:
    assert default_policy_payload() == {
        "default_effect": "allow",
        "rules": [],
        "hardening_enabled": False,
    }


def _identity(root: Path, gateway_id: str) -> FederationPeerIdentity:
    return FederationPeerIdentity(
        gateway_id=gateway_id,
        connection_id="rgw_test",
        public_key_pem=public_key_pem(load_or_create_signing_key(root)),
    )


@pytest.fixture
def grant_request() -> dict[str, object]:
    return {
        "request_id": "req",
        "session_id": "ses_019b9dffc29a779e8564d196f78bb0a6",
        "nonce": "policy-contract-nonce",
        "visited": ["gateway_b", "gateway_hub"],
        "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
        "max_gateway_hops": MAX_GATEWAY_HOPS,
    }


@pytest.fixture
def wait_request(grant_request: dict[str, object]) -> dict[str, object]:
    admitted = datetime.now(UTC)
    source = {
        "gateway_id": "gateway_b",
        "workspace_id": "11111111-1111-4111-8111-111111111111",
        "session_id": "ses_019c390d6a707c858709afddc785bf93",
        "thread_id": "thr_019bb545cb1f7ddc999cc895fa6285bc",
    }
    target = {
        "gateway_id": "gateway_c",
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "session_id": "ses_019b9dffc29a779e8564d196f78bb0a6",
        "thread_id": "thr_01a100c4a1897a2d8d40b568d23fcbe6",
    }
    return {
        **{key: value for key, value in grant_request.items() if key != "session_id"},
        "operation": "wait",
        "target": {key: value for key, value in target.items() if key != "thread_id"},
        "body": {
            "observation_id": "obs_policy",
            "source_call_id": "cop_policy",
            "page_identity": "wait-root",
            "page_ordinal": 0,
            "expires_at": (admitted + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "source": source,
            "target": target,
            "operation": "wait",
            "query": {
                "until": "terminal",
                "timeout_seconds": 300,
                "selector": {"kind": "job", "selector_id": "job_policy"},
                "deadline": {
                    "timeout_seconds": 300,
                    "admitted_at_utc": admitted.isoformat(),
                    "deadline_at_utc": (admitted + timedelta(seconds=300)).isoformat(),
                    "monotonic_origin_id": "policy-clock",
                    "monotonic_deadline_ns": 300_000_000_000,
                },
            },
        },
    }


def test_verify_grant_accepts_bound_audience_and_path(
    tmp_path: Path, grant_request: dict[str, object]
) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        request_preimage=grant_request,
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    verify_grant(
        grant,
        issuer=issuer,
        local_gateway_id="gateway_c",
        expected_kind="discovery",
        request_preimage=grant_request,
        expected_origin="gateway_b",
        expected_path=("gateway_b", "gateway_hub", "gateway_c"),
    )


def test_verify_grant_rejects_wrong_audience(
    tmp_path: Path, grant_request: dict[str, object]
) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        request_preimage=grant_request,
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            grant,
            issuer=issuer,
            local_gateway_id="gateway_other",
            expected_kind="discovery",
            request_preimage=grant_request,
        )
    assert error.value.code == FEDERATION_GRANT_AUDIENCE_MISMATCH


def test_verify_grant_rejects_path_with_two_transits(
    tmp_path: Path, grant_request: dict[str, object]
) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    too_long = (
        "gateway_b",
        "gateway_hub",
        "gateway_mid",
        "gateway_c",
    )
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=too_long,
        request_id="req",
        request_preimage=grant_request,
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            grant,
            issuer=issuer,
            local_gateway_id="gateway_c",
            expected_kind="discovery",
            request_preimage=grant_request,
            expected_path=too_long,
        )
    assert error.value.code == FEDERATION_GRANT_PATH_MISMATCH
    assert len(too_long) - 2 > MAX_TRANSIT_GATEWAYS
    assert MAX_GATEWAY_HOPS == 2


def test_verify_grant_expired_and_lifetime_insufficient(
    tmp_path: Path, grant_request: dict[str, object], wait_request: dict[str, object]
) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    expired = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        request_preimage=grant_request,
        principal_ref="prn_b",
        lifetime_seconds=30,
        now=datetime.now(UTC) - timedelta(seconds=120),
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            expired,
            issuer=issuer,
            local_gateway_id="gateway_c",
            expected_kind="discovery",
            request_preimage=grant_request,
        )
    assert error.value.code == FEDERATION_GRANT_EXPIRED

    short = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="operation",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        request_preimage=wait_request,
        principal_ref="prn_b",
        lifetime_seconds=10,
        target=GrantTarget(**wait_request["target"]),
        operation="wait",
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            short,
            issuer=issuer,
            local_gateway_id="gateway_c",
            expected_kind="operation",
            request_preimage=wait_request,
            minimum_remaining_seconds=required_wait_grant_lifetime(
                effective_timeout_seconds=300
            ),
        )
    assert error.value.code == FEDERATION_GRANT_LIFETIME_INSUFFICIENT


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "req_changed"),
        ("session_id", "ses_changed"),
        ("nonce", "different"),
        ("visited", ["gateway_hub"]),
    ],
)
def test_grant_rejects_changed_request_preimage(
    tmp_path: Path, grant_request: dict[str, object], field: str, value: object
) -> None:
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        principal_ref="prn_b",
        request_preimage=grant_request,
        lifetime_seconds=30,
    )
    changed = deepcopy(grant_request)
    changed[field] = value
    with pytest.raises(FederationError) as error:
        verify_grant(
            grant,
            issuer=_identity(tmp_path, "gateway_hub"),
            local_gateway_id="gateway_c",
            expected_kind="discovery",
            request_preimage=changed,
        )
    assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE


def test_required_wait_grant_lifetime_covers_timeout_and_skew() -> None:
    assert required_wait_grant_lifetime(effective_timeout_seconds=60) == 65.0
    assert required_wait_grant_lifetime(effective_timeout_seconds=300) == 305.0
    with pytest.raises(ValueError):
        required_wait_grant_lifetime(effective_timeout_seconds=0)


def test_transit_limit_constant_is_documented() -> None:
    # design.md §8.9-A：只允许一次 hub transit，恰好 2 跳。
    assert MAX_TRANSIT_GATEWAYS == 1
    assert MAX_GATEWAY_HOPS == 2


@pytest.fixture(params=("session", "workspace_inventory", "gateway_inventory"))
def context_grant_target(request: pytest.FixtureRequest) -> ContextGrantTarget:
    gateway = "gateway_c"
    if request.param == "session":
        identity = session_resource_identity(WORKSPACE_ID, TARGET_SESSION)
        uri = session_resource_uri(WORKSPACE_ID, TARGET_SESSION, gateway_authority=gateway)
        scope = "workspace"
    elif request.param == "workspace_inventory":
        identity = workspace_session_inventory_identity(WORKSPACE_ID)
        uri = workspace_session_inventory_uri(WORKSPACE_ID, gateway_authority=gateway)
        scope = "workspace"
    else:
        identity = gateway_session_inventory_identity(gateway)
        uri = gateway_session_inventory_uri(gateway, gateway_authority=gateway)
        scope = "gateway"
    return ContextGrantTarget(gateway, str(identity), uri, scope)


@pytest.mark.parametrize("query_kind", ["read", "search"])
def test_context_grant_roundtrip_binds_all_resource_and_query_fields(
    tmp_path: Path, grant_request: dict[str, object],
    context_grant_target: ContextGrantTarget, query_kind: str,
) -> None:
    fields = {
        key: context_grant_target.to_payload()[key] for key in ("resource_identity", "uri", "scope")
    }
    query = {**fields, "view": "inventory" if context_grant_target.scope == "gateway" else "information"}
    if query_kind == "search":
        query = {**fields, "query": "exact marker", "sources": ["session_information"], "limit": 2}
    preimage = {
        **{key: value for key, value in grant_request.items() if key != "session_id"},
        "operation": "read", "target": context_grant_target.to_payload(),
        "body": {"source_gateway_id": "gateway_b", "resource_target": context_grant_target.to_payload(), query_kind: query},
    }
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path), grant_kind="operation",
        issuer_gateway_id="gateway_hub", origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c", transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req", request_preimage=preimage, principal_ref="prn_b", lifetime_seconds=30,
        target=context_grant_target, operation="read",
    )
    decoded = FederationGrant.decode(grant.encode())
    assert isinstance(decoded.target, ContextGrantTarget)
    assert decoded.target == context_grant_target
    assert decoded.encode() == grant.encode()
    verify_grant(
        decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
        expected_kind="operation", request_preimage=preimage, expected_origin="gateway_b",
        expected_path=("gateway_b", "gateway_hub", "gateway_c"),
    )
    for field in ("gateway_id", "resource_identity", "uri", "scope"):
        changed = deepcopy(preimage)
        changed["target"][field] = "changed"
        with pytest.raises(FederationError) as error:
            verify_grant(
                decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
                expected_kind="operation", request_preimage=changed,
            )
        assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE
    changed = deepcopy(preimage)
    changed["body"][query_kind]["query" if query_kind == "search" else "view"] = "changed"
    with pytest.raises(FederationError) as error:
        verify_grant(
            decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
            expected_kind="operation", request_preimage=changed,
        )
    assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE


@pytest.mark.parametrize("bad", ["extra_field", "missing_uri", "wrong_authority", "wrong_identity"])
def test_context_grant_target_rejects_wrong_type_or_owner(
    context_grant_target: ContextGrantTarget, bad: str,
) -> None:
    raw = context_grant_target.to_payload()
    if bad == "extra_field":
        raw["session_id"] = TARGET_SESSION
    elif bad == "missing_uri":
        del raw["uri"]
    elif bad == "wrong_authority":
        raw["gateway_id"] = "gateway_other"
    else:
        raw["resource_identity"] = "unrelated-identity"
    with pytest.raises(FederationError) as error:
        decode_grant_target(raw)
    assert error.value.code == "federation-malformed-frame"


@pytest.fixture(params=("source", "target"))
def fork_grant_target(request: pytest.FixtureRequest) -> ForkGrantTarget:
    return ForkGrantTarget(
        gateway_id="gateway_c",
        uri=thread_resource_uri(
            WORKSPACE_ID, TARGET_SESSION, "thr_00000000400070008000000000000001", gateway_authority="gateway_c",
        ),
        fork_role="source" if request.param == "source" else "target",
    )


@pytest.fixture
def fork_preimage(grant_request: dict[str, object], fork_grant_target: ForkGrantTarget) -> dict[str, object]:
    source_uri = fork_grant_target.uri if fork_grant_target.fork_role == "source" else thread_resource_uri(
        WORKSPACE_ID, TARGET_SESSION, "thr_00000000400070008000000000000002", gateway_authority="gateway_b",
    )
    target_uri = fork_grant_target.uri if fork_grant_target.fork_role == "target" else session_resource_uri(
        WORKSPACE_ID, TARGET_SESSION, gateway_authority="gateway_b",
    )
    return {
        **{key: value for key, value in grant_request.items() if key != "session_id"},
        "operation": "fork_" + fork_grant_target.fork_role,
        "target": fork_grant_target.to_payload(),
        "body": {
            "source_gateway_id": "gateway_b", "fork_target": fork_grant_target.to_payload(),
            "action": "intent" if fork_grant_target.fork_role == "source" else "create",
            "request": {
                "idempotency_key": "fork-grant-exact-child", "source_uri": source_uri,
                "target_container_uri": target_uri, "creation_kind": "new_child_thread",
                "source_view_id": "view-fixed", "source_view_revision": 7,
                "anchor_item_id": None, "mode": "full_rollout_copy",
            },
        },
    }


def test_fork_grant_roundtrip_binds_role_target_and_complete_request(
    tmp_path: Path, fork_grant_target: ForkGrantTarget, fork_preimage: dict[str, object],
) -> None:
    operation: OperationKind = "fork_source" if fork_grant_target.fork_role == "source" else "fork_target"
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path), grant_kind="operation",
        issuer_gateway_id="gateway_hub", origin_gateway_id="gateway_b", audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"), request_id="req", principal_ref="prn_b",
        request_preimage=fork_preimage, lifetime_seconds=30, target=fork_grant_target, operation=operation,
    )
    decoded = FederationGrant.decode(grant.encode())
    assert isinstance(decoded.target, ForkGrantTarget) and decoded.target == fork_grant_target
    assert decoded.operation == operation and decoded.encode() == grant.encode()
    verify_grant(
        decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
        expected_kind="operation", request_preimage=fork_preimage, expected_origin="gateway_b",
        expected_path=("gateway_b", "gateway_hub", "gateway_c"),
    )
    for field in ("source_uri", "target_container_uri", "creation_kind", "source_view_id", "source_view_revision", "anchor_item_id"):
        changed = deepcopy(fork_preimage)
        changed["body"]["request"][field] = "changed"
        with pytest.raises(FederationError) as error:
            verify_grant(
                decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
                expected_kind="operation", request_preimage=changed,
            )
        assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE
    for field in ("gateway_id", "uri", "fork_role"):
        changed = deepcopy(fork_preimage)
        changed["target"][field] = "changed"
        with pytest.raises(FederationError) as error:
            verify_grant(
                decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
                expected_kind="operation", request_preimage=changed,
            )
        assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE


@pytest.mark.parametrize("operation", ["send", "read", "wait", "reply"])
def test_fork_grant_cannot_be_signed_with_read_or_communication_capability(
    tmp_path: Path, fork_grant_target: ForkGrantTarget, fork_preimage: dict[str, object], operation: OperationKind,
) -> None:
    with pytest.raises(FederationError) as error:
        sign_grant(
            private_key=load_or_create_signing_key(tmp_path), grant_kind="operation",
            issuer_gateway_id="gateway_hub", origin_gateway_id="gateway_b", audience_gateway_id="gateway_c",
            transit_path=("gateway_b", "gateway_hub", "gateway_c"), request_id="req", principal_ref="prn_b",
            request_preimage=fork_preimage, lifetime_seconds=30, target=fork_grant_target, operation=operation,
        )
    assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH
    opposite: OperationKind = "fork_target" if fork_grant_target.fork_role == "source" else "fork_source"
    with pytest.raises(FederationError) as error:
        validate_grant_operation(fork_grant_target, opposite)
    assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH


@pytest.fixture
def read_only_fork_relay() -> FederationRpcService:
    return FederationRpcService(
        gateway_id="gateway_c",
        policy_store=FederationPolicyStore(initial={
            "default_effect": "deny", "rules": [{"capability": "read", "effect": "allow"}],
        }),
        control_store=Mock(), signing_key=Mock(), catalog=Mock(),
        session_main=Mock(execute=AsyncMock(side_effect=AssertionError("未获 fork 权限不得访问业务 owner"))),
        credential_store=Mock(),
    )


async def test_real_relay_owner_denies_fork_when_only_read_is_allowed(
    read_only_fork_relay: FederationRpcService, fork_grant_target: ForkGrantTarget, fork_preimage: dict[str, object],
) -> None:
    operation: OperationKind = "fork_source" if fork_grant_target.fork_role == "source" else "fork_target"
    assert read_only_fork_relay.policy_store.snapshot.evaluate(
        capability="read", origin_gateway_id="gateway_c", principal_ref="prn_c",
    )
    frozen_body = fork_preimage["body"]
    assert isinstance(frozen_body, dict)
    body = {**frozen_body, "source_gateway_id": read_only_fork_relay.gateway_id}
    with pytest.raises(FederationError) as error:
        await read_only_fork_relay.relay_operation(
            target=fork_grant_target, operation=operation, body=body,
        )
    assert error.value.code == FEDERATION_AUTHORIZATION_REVOKED
    assert error.value.detail["capability"] == operation


async def test_real_relay_owner_reports_unreachable_fork_target(
    read_only_fork_relay: FederationRpcService, fork_grant_target: ForkGrantTarget, fork_preimage: dict[str, object],
) -> None:
    read_only_fork_relay.policy_store.publish(default_policy_payload())
    target = ForkGrantTarget(
        gateway_id="gateway_offline",
        uri=thread_resource_uri(
            WORKSPACE_ID, TARGET_SESSION, "thr_00000000400070008000000000000001", gateway_authority="gateway_offline",
        ),
        fork_role=fork_grant_target.fork_role,
    )
    operation: OperationKind = "fork_source" if target.fork_role == "source" else "fork_target"
    frozen_body = fork_preimage["body"]
    assert isinstance(frozen_body, dict)
    frozen_request = frozen_body["request"]
    assert isinstance(frozen_request, dict)
    uri_field = "source_uri" if target.fork_role == "source" else "target_container_uri"
    body = {
        **frozen_body, "source_gateway_id": read_only_fork_relay.gateway_id,
        "fork_target": target.to_payload(), "request": {**frozen_request, uri_field: target.uri},
    }
    with pytest.raises(FederationError) as error:
        await read_only_fork_relay.relay_operation(target=target, operation=operation, body=body)
    assert error.value.code == FEDERATION_CHANNEL_CLOSED


def test_signed_read_grant_target_cannot_authorize_fork(
    tmp_path: Path, grant_request: dict[str, object], context_grant_target: ContextGrantTarget,
) -> None:
    fields = {key: context_grant_target.to_payload()[key] for key in ("resource_identity", "uri", "scope")}
    preimage = {
        **{key: value for key, value in grant_request.items() if key != "session_id"},
        "operation": "read", "target": context_grant_target.to_payload(),
        "body": {
            "source_gateway_id": "gateway_b", "resource_target": context_grant_target.to_payload(),
            "read": {**fields, "view": "inventory" if context_grant_target.scope == "gateway" else "information"},
        },
    }
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path), grant_kind="operation",
        issuer_gateway_id="gateway_hub", origin_gateway_id="gateway_b", audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"), request_id="req", principal_ref="prn_b",
        request_preimage=preimage, lifetime_seconds=30, target=context_grant_target, operation="read",
    )
    decoded = FederationGrant.decode(grant.encode())
    assert isinstance(decoded.target, ContextGrantTarget)
    verify_grant(
        decoded, issuer=_identity(tmp_path, "gateway_hub"), local_gateway_id="gateway_c",
        expected_kind="operation", request_preimage=preimage,
    )
    operations: tuple[OperationKind, ...] = ("fork_source", "fork_target")
    for operation in operations:
        with pytest.raises(FederationError) as error:
            validate_grant_operation(decoded.target, operation)
        assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH


@pytest.fixture
def file_write_target():
    return TextMutationGrantTarget(gateway_id="gateway_c", uri=f"boxteam://gateway_c/workspace/{WORKSPACE_ID}/resources/files/root")


@pytest.mark.parametrize("operation", ["send", "read", "wait", "reply", "fork_source", "fork_target"])
def test_file_write_target_rejects_other_capabilities(file_write_target, operation):
    with pytest.raises(FederationError) as error:
        validate_grant_operation(file_write_target, operation)
    assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH
    with pytest.raises(FederationError) as error:
        validate_grant_operation(ResourceNavigationGrantTarget(gateway_id=file_write_target.gateway_id, uri=file_write_target.uri), "file_write")
    assert error.value.code == FEDERATION_GRANT_KIND_MISMATCH


def test_file_write_policy_is_independent_of_communication_read():
    policy = normalize_policy({"default_effect": "deny", "rules": [{"capability": "read", "effect": "allow"}]})
    assert policy.evaluate(capability="read", origin_gateway_id="gateway_b", principal_ref="prn_b")
    assert not policy.evaluate(capability="file_write", origin_gateway_id="gateway_b", principal_ref="prn_b")


def test_file_write_signature_binds_fixed_target_versions_and_content(tmp_path, grant_request, file_write_target):
    issuer = _identity(tmp_path, "gateway_hub")
    preimage = {**grant_request, "operation": "file_write", "target": file_write_target.to_payload(),
        "body": {"expected_versions": {"file.txt": "sha256:" + "a" * 64}, "content": "intended"}}
    grant = sign_grant(private_key=load_or_create_signing_key(tmp_path), grant_kind="operation",
        issuer_gateway_id="gateway_hub", origin_gateway_id="gateway_b", audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"), request_id="req", request_preimage=preimage,
        principal_ref="prn_b", lifetime_seconds=30, target=file_write_target, operation="file_write")
    decoded = FederationGrant.decode(grant.encode())
    assert decoded.target == file_write_target and decoded.operation == "file_write"
    verify_grant(decoded, issuer=issuer, local_gateway_id="gateway_c", expected_kind="operation", request_preimage=preimage)
    for field in ("target", "version", "content"):
        changed = deepcopy(preimage)
        if field == "target":
            changed["target"]["uri"] += "/elsewhere"
        elif field == "version":
            changed["body"]["expected_versions"]["file.txt"] = "absent"
        else:
            changed["body"]["content"] = "different"
        with pytest.raises(FederationError) as error:
            verify_grant(decoded, issuer=issuer, local_gateway_id="gateway_c", expected_kind="operation", request_preimage=changed)
        assert error.value.code == FEDERATION_GRANT_INVALID_SIGNATURE
