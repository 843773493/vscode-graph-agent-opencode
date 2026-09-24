"""``permissions.federation`` 策略快照与 grant 校验的纯单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.gateway.federation.errors import (
    FEDERATION_GRANT_AUDIENCE_MISMATCH,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
    FEDERATION_GRANT_PATH_MISMATCH,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    GrantTarget,
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
    published = store.publish(
        {"rules": [{"capability": "read", "effect": "deny"}]}
    )
    assert published.revision == base + 1
    assert store.snapshot.evaluate(
        capability="read", origin_gateway_id="peer", principal_ref="prn"
    ) is False
    # 未声明的能力仍按默认 allow。
    assert store.snapshot.evaluate(
        capability="send", origin_gateway_id="peer", principal_ref="prn"
    ) is True


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


def test_verify_grant_accepts_bound_audience_and_path(tmp_path: Path) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    verify_grant(
        grant,
        issuer=issuer,
        local_gateway_id="gateway_c",
        expected_kind="discovery",
        expected_origin="gateway_b",
        expected_path=("gateway_b", "gateway_hub", "gateway_c"),
    )


def test_verify_grant_rejects_wrong_audience(tmp_path: Path) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    grant = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            grant,
            issuer=issuer,
            local_gateway_id="gateway_other",
            expected_kind="discovery",
        )
    assert error.value.code == FEDERATION_GRANT_AUDIENCE_MISMATCH


def test_verify_grant_rejects_path_with_two_transits(tmp_path: Path) -> None:
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
        principal_ref="prn_b",
        lifetime_seconds=30,
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            grant,
            issuer=issuer,
            local_gateway_id="gateway_c",
            expected_kind="discovery",
            expected_path=too_long,
        )
    assert error.value.code == FEDERATION_GRANT_PATH_MISMATCH
    assert len(too_long) - 2 > MAX_TRANSIT_GATEWAYS
    assert MAX_GATEWAY_HOPS == 2


def test_verify_grant_expired_and_lifetime_insufficient(tmp_path: Path) -> None:
    issuer = _identity(tmp_path, "gateway_hub")
    expired = sign_grant(
        private_key=load_or_create_signing_key(tmp_path),
        grant_kind="discovery",
        issuer_gateway_id="gateway_hub",
        origin_gateway_id="gateway_b",
        audience_gateway_id="gateway_c",
        transit_path=("gateway_b", "gateway_hub", "gateway_c"),
        request_id="req",
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
        principal_ref="prn_b",
        lifetime_seconds=10,
        target=GrantTarget(
            gateway_id="gateway_c", workspace_id="ws", session_id="ses_1"
        ),
        operation="wait",
    )
    with pytest.raises(FederationError) as error:
        verify_grant(
            short,
            issuer=issuer,
            local_gateway_id="gateway_c",
            expected_kind="operation",
            minimum_remaining_seconds=required_wait_grant_lifetime(
                effective_timeout_seconds=300
            ),
        )
    assert error.value.code == FEDERATION_GRANT_LIFETIME_INSUFFICIENT


def test_required_wait_grant_lifetime_covers_timeout_and_skew() -> None:
    assert required_wait_grant_lifetime(effective_timeout_seconds=60) == 65.0
    assert required_wait_grant_lifetime(effective_timeout_seconds=300) == 305.0
    with pytest.raises(ValueError):
        required_wait_grant_lifetime(effective_timeout_seconds=0)


def test_transit_limit_constant_is_documented() -> None:
    # design.md §8.9-A：只允许一次 hub transit，恰好 2 跳。
    assert MAX_TRANSIT_GATEWAYS == 1
    assert MAX_GATEWAY_HOPS == 2
