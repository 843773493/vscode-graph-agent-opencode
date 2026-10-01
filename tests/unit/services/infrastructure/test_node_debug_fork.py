"""Node 调试方案 source capture 与 target prepublish 的定向测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugLaunchProfileDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceDriftError,
    capture_source_copy_snapshot,
    validate_target_prepublication,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

_SESSION_ID = "ses_00000000400040008000000000000001"
_CONFIGURATION_ID = "dbgcfg_11111111111111111111111111111111"
_CONFIGURATION_ID_2 = "dbgcfg_22222222222222222222222222222222"


def _create_session(sessions_root: Path, session_id: str) -> Path:
    return seed_catalog_session_bundle(sessions_root, session_id, title=session_id).directory


def _configuration(configuration_id: str, name: str, path: str) -> NodeDebugConfigurationDTO:
    now = datetime.now(UTC)
    return NodeDebugConfigurationDTO(
        configuration_id=configuration_id,
        name=name,
        revision=3,
        script_path=path,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def source_store(tmp_path: Path) -> tuple[NodeDebugSessionStore, Path]:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    session_dir = _create_session(sessions_root, _SESSION_ID)
    store = NodeDebugSessionStore(resolver)
    store.write_configuration(
        _SESSION_ID,
        _configuration(_CONFIGURATION_ID, "主方案", "main.mjs"),
        "main",
    )
    store.write_configuration(
        _SESSION_ID,
        _configuration(_CONFIGURATION_ID_2, "备用方案", "other.mjs"),
        "main",
    )
    store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=_SESSION_ID,
            thread_id="main",
            active_configuration_id=_CONFIGURATION_ID,
            configuration_ids=(_CONFIGURATION_ID, _CONFIGURATION_ID_2),
            updated_at=datetime.now(UTC),
        )
    )
    return store, session_dir


def test_capture_matrix_is_manifest_bound_and_does_not_copy_runtime_pointer(
    source_store: tuple[NodeDebugSessionStore, Path],
) -> None:
    store, session_dir = source_store
    unregistered = session_dir / "debug" / "node" / "configurations" / (
        "dbgcfg_33333333333333333333333333333333.json"
    )
    unregistered.write_bytes(
        _configuration("dbgcfg_33333333333333333333333333333333", "未登记", "main.mjs")
        .model_dump_json(indent=2)
        .encode()
    )

    context = capture_source_copy_snapshot(
        store,
        session_id=_SESSION_ID,
        thread_id="main",
        capture_mode="context_fork",
        workspace_config_revision="workspace-debug-v1",
        workspace_config_hash="config-hash-v1",
    )
    assert [item.configuration_id for item in context.configuration_artifacts] == [
        _CONFIGURATION_ID
    ]
    assert context.active_configuration_id is None
    assert b"active_configuration_id" in context.manifest_bytes
    artifact = context.configuration_artifacts[0]
    assert artifact.size_bytes == len(artifact.payload_bytes)
    assert artifact.sha256 == hashlib.sha256(artifact.payload_bytes).hexdigest()
    assert artifact.configuration().revision == 3

    history = capture_source_copy_snapshot(
        store,
        session_id=_SESSION_ID,
        thread_id="main",
        capture_mode="history_prefix_fork",
    )
    assert history.configuration_artifacts == ()

    full = capture_source_copy_snapshot(
        store,
        session_id=_SESSION_ID,
        thread_id="main",
        capture_mode="full_rollout_copy",
    )
    assert [item.configuration_id for item in full.configuration_artifacts] == [
        _CONFIGURATION_ID,
        _CONFIGURATION_ID_2,
    ]


def test_capture_fails_closed_when_registered_source_drifts(
    source_store: tuple[NodeDebugSessionStore, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, session_dir = source_store
    original = store.read_registered_configuration_payloads
    calls = 0

    def drift(*args: object, **kwargs: object):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            path = (
                session_dir
                / "debug"
                / "node"
                / "configurations"
                / f"{_CONFIGURATION_ID}.json"
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["revision"] = 4
            path.write_text(json.dumps(payload), encoding="utf-8")
        return result

    monkeypatch.setattr(store, "read_registered_configuration_payloads", drift)
    with pytest.raises(NodeDebugSourceDriftError, match="bytes/revision 漂移"):
        capture_source_copy_snapshot(
            store,
            session_id=_SESSION_ID,
            thread_id="main",
            capture_mode="context_fork",
        )


def test_target_prepublication_rechecks_paths_profile_and_config_revision(
    source_store: tuple[NodeDebugSessionStore, Path],
    tmp_path: Path,
) -> None:
    store, _session_dir = source_store
    (tmp_path / "main.mjs").write_text("console.log('main')", encoding="utf-8")
    snapshot = capture_source_copy_snapshot(
        store,
        session_id=_SESSION_ID,
        thread_id="main",
        capture_mode="context_fork",
        workspace_config_revision="workspace-debug-v1",
        workspace_config_hash="config-hash-v1",
        workspace_id="workspace-a",
    )
    result = validate_target_prepublication(
        snapshot,
        target_workspace_root=tmp_path,
        target_workspace_config_revision="workspace-debug-v1",
        target_workspace_config_hash="config-hash-v1",
        target_workspace_id="workspace-a",
        launch_profiles={
            "node-default": NodeDebugLaunchProfileDTO(
                name="node-default",
                adapter="node_inspector",
                runtime="node",
                supported=True,
            )
        },
    )
    assert result.active_configuration_id is None
    assert result.validated_configuration_ids == (_CONFIGURATION_ID,)

    with pytest.raises(RuntimeError, match="配置 revision 已漂移"):
        validate_target_prepublication(
            snapshot,
            target_workspace_root=tmp_path,
            target_workspace_config_revision="workspace-debug-v2",
            target_workspace_config_hash="config-hash-v1",
            target_workspace_id="workspace-a",
            launch_profiles={},
        )

    with pytest.raises(RuntimeError, match="不支持跨 Workspace"):
        validate_target_prepublication(
            snapshot,
            target_workspace_root=tmp_path,
            target_workspace_config_revision="workspace-debug-v1",
            target_workspace_config_hash="config-hash-v1",
            target_workspace_id="workspace-b",
            launch_profiles={},
        )
