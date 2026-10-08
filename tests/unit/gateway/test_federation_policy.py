"""``permissions.federation`` 策略快照与 grant 校验的纯单元测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.gateway.federation.errors import (
    FEDERATION_GRANT_AUDIENCE_MISMATCH,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_GRANT_INVALID_SIGNATURE,
    FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
    FEDERATION_GRANT_PATH_MISMATCH,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    ContextGrantTarget,
    FederationGrant,
    GrantTarget,
    decode_grant_target,
    required_wait_grant_lifetime,
    sign_grant,
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
    for capability in ("discovery", "send", "read", "wait", "reply", "transit"):
        assert snapshot.evaluate(
            capability=capability,  # type: ignore[arg-type]
            origin_gateway_id="gateway_peer",
            principal_ref="prn_x",
        )


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
