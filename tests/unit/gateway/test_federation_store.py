"""联邦控制面 store 的单元测试：replay registry、channel epoch 与 route hint。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_EPOCH_STALE,
    FEDERATION_GRANT_REPLAY,
    FEDERATION_GRANT_REPLAY_CONFLICT,
    FederationError,
)
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)


@pytest.fixture
def store(tmp_path: Path):
    control = FederationControlStore(
        database=federation_control_database(gateway_root=tmp_path / "gateway")
    )
    try:
        yield control
    finally:
        control.close()


def test_replay_first_use_is_adopted_then_rejected(store: FederationControlStore) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    kwargs = {
        "issuer_gateway_id": "gateway_hub",
        "origin_gateway_id": "gateway_b",
        "audience_gateway_id": "gateway_c",
        "grant_kind": "discovery",
        "nonce": "nonce_1",
        "preimage": {"session_id": "ses_1"},
        "expires_at": expires,
    }
    store.adopt_grant(**kwargs)
    with pytest.raises(FederationError) as error:
        store.adopt_grant(**kwargs)
    assert error.value.code == FEDERATION_GRANT_REPLAY


def test_replay_same_nonce_different_preimage_conflicts(
    store: FederationControlStore,
) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    base = {
        "issuer_gateway_id": "gateway_hub",
        "origin_gateway_id": "gateway_b",
        "audience_gateway_id": "gateway_c",
        "grant_kind": "discovery",
        "nonce": "nonce_2",
        "expires_at": expires,
    }
    store.adopt_grant(**base, preimage={"session_id": "ses_1"})
    with pytest.raises(FederationError) as error:
        store.adopt_grant(**base, preimage={"session_id": "ses_2"})
    assert error.value.code == FEDERATION_GRANT_REPLAY_CONFLICT


def test_replay_registry_survives_reopen(tmp_path: Path) -> None:
    """Gateway 重启不得清空有效窗口（跨重启防 replay）。"""

    root = tmp_path / "gateway"
    expires = datetime.now(UTC) + timedelta(seconds=30)
    kwargs = {
        "issuer_gateway_id": "gateway_hub",
        "origin_gateway_id": "gateway_b",
        "audience_gateway_id": "gateway_c",
        "grant_kind": "discovery",
        "nonce": "nonce_3",
        "preimage": {"session_id": "ses_1"},
        "expires_at": expires,
    }
    first = FederationControlStore(database=federation_control_database(gateway_root=root))
    first.adopt_grant(**kwargs)
    entries = first.replay_entries()
    assert len(entries) == 1
    assert entries[0].nonce == "nonce_3"
    assert entries[0].expires_at > expires
    first.close()

    second = FederationControlStore(
        database=federation_control_database(gateway_root=root)
    )
    try:
        with pytest.raises(FederationError) as error:
            second.adopt_grant(**kwargs)
        assert error.value.code == FEDERATION_GRANT_REPLAY
    finally:
        second.close()


def test_channel_epoch_must_strictly_advance(store: FederationControlStore) -> None:
    store.register_channel(
        channel_instance_id="chan_1",
        peer_gateway_id="gateway_hub",
        connection_id="rgw_1",
        channel_epoch=1,
        direction="inbound",
    )
    with pytest.raises(FederationError) as error:
        store.register_channel(
            channel_instance_id="chan_stale",
            peer_gateway_id="gateway_hub",
            connection_id="rgw_1",
            channel_epoch=1,
            direction="inbound",
        )
    assert error.value.code == FEDERATION_CHANNEL_EPOCH_STALE
    # 推进 epoch 允许替换旧 channel。
    record = store.register_channel(
        channel_instance_id="chan_2",
        peer_gateway_id="gateway_hub",
        connection_id="rgw_1",
        channel_epoch=2,
        direction="inbound",
    )
    assert record.channel_epoch == 2


def test_route_hint_expires_and_is_not_authoritative(
    store: FederationControlStore,
) -> None:
    now = datetime.now(UTC)
    hint = store.record_route_hint(
        gateway_id="gateway_c",
        workspace_id="ws_c",
        session_id="ses_c",
        resolved_main_thread_id="thr_1",
        catalog_revision="rev_1",
        now=now,
    )
    assert hint.expires_at > now
    assert (
        store.read_route_hint(
            gateway_id="gateway_c", workspace_id="ws_c", session_id="ses_c", now=now
        )
        is not None
    )
    # 过期后必须返回 None，不返回虚假默认值。
    assert (
        store.read_route_hint(
            gateway_id="gateway_c",
            workspace_id="ws_c",
            session_id="ses_c",
            now=now + timedelta(seconds=120),
        )
        is None
    )


def test_unregister_channel_returns_none_for_missing(store: FederationControlStore) -> None:
    store.unregister_channel(channel_instance_id="chan_missing")
    assert store.active_channels() == ()
    assert store.channels_for_connection("rgw_missing") == ()
