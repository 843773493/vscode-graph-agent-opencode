"""Node Debug run claim 的 owner、持久化及阶段迁移合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.process.launch_claim import (
    claim_marked,
    claim_running,
    claim_with_target_pid,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.catalog_session_bundle import (
    fixture_main_thread_id,
    seed_catalog_session_bundle,
)
from tests.support.node_debug_dependencies import node_debug_binding_snapshot

_PARENT_SESSION_ID = "ses_00000000400070008000000000000001"
_CHILD_SESSION_ID = "ses_00000000400070008000000000000002"
_CONFIGURATION_ID = "dbgcfg_11111111111111111111111111111111"
_PARENT_MAIN_THREAD_ID = fixture_main_thread_id(_PARENT_SESSION_ID)
_CHILD_MAIN_THREAD_ID = fixture_main_thread_id(_CHILD_SESSION_ID)


def _create_session(resolver: object, session_id: str, *, parent_session_id: str | None = None) -> Path:
    return seed_catalog_session_bundle(
        resolver.sessions_root,
        session_id,
        title=f"测试会话 {session_id}",
        parent_node_id=parent_session_id,
        main_thread_id=fixture_main_thread_id(session_id),
    ).directory


@pytest.fixture
def session_tree(tmp_path: Path):
    from app.core.path_utils import get_session_path_resolver

    resolver = get_session_path_resolver(tmp_path / ".boxteam" / "sessions")
    resolver.initialize()
    _create_session(resolver, _PARENT_SESSION_ID)
    _create_session(resolver, _CHILD_SESSION_ID, parent_session_id=_PARENT_SESSION_ID)
    return resolver


def _claim(session_id: str = _PARENT_SESSION_ID, thread_id: str = _PARENT_MAIN_THREAD_ID):
    return new_launch_claim(
        session_id=session_id,
        thread_id=thread_id,
        run_id=create_prefixed_id("op"),
        configuration_id=_CONFIGURATION_ID,
        debugger_binding=node_debug_binding_snapshot(),
        script_path="entry.mjs",
        args=[],
        node_executable="node",
        working_directory="",
        launch_profile_name=None,
        breakpoints=[],
    )


def test_launch_claim_round_trip_and_owner_validation(session_tree) -> None:
    store = NodeDebugSessionStore(session_tree)
    assert store.read_launch_claim(_PARENT_SESSION_ID, _PARENT_MAIN_THREAD_ID) is None

    claim = _claim()
    store.write_launch_claim(claim)
    path = session_tree.resolve_thread_node(_PARENT_SESSION_ID, _PARENT_MAIN_THREAD_ID)
    claim_path = path / "debug" / "node" / "launch-claim.json"
    assert claim_path.is_file()
    loaded = store.read_launch_claim(_PARENT_SESSION_ID, _PARENT_MAIN_THREAD_ID)
    assert loaded == claim.model_copy(update={"updated_at": loaded.updated_at})
    assert loaded is not None and loaded.phase == "launch_pending" and loaded.pid is None

    second = _claim()
    assert second.run_id != claim.run_id
    assert second.process_instance_id != claim.process_instance_id

    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["thread_id"] = _CHILD_MAIN_THREAD_ID
    claim_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SessionThread 不匹配"):
        store.read_launch_claim(_PARENT_SESSION_ID, _PARENT_MAIN_THREAD_ID)


def test_store_write_paths_reject_alias_owner(session_tree) -> None:
    store = NodeDebugSessionStore(session_tree)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        store.write_manifest(NodeDebugSessionManifestDTO(
            session_id=_PARENT_SESSION_ID,
            thread_id=_CHILD_SESSION_ID,
            updated_at=now,
        ))
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        store.write_launch_claim(_claim(_PARENT_SESSION_ID, _CHILD_SESSION_ID))
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        store.write_configuration(
            _PARENT_SESSION_ID,
            NodeDebugConfigurationDTO(
                configuration_id=_CONFIGURATION_ID,
                name="别名方案",
                created_at=now,
                updated_at=now,
            ),
            _CHILD_SESSION_ID,
        )
    with pytest.raises(ValueError, match="thread_id 形态非法"):
        store.delete_configuration(_PARENT_SESSION_ID, _CONFIGURATION_ID, _CHILD_SESSION_ID)

    child_node = session_tree.resolve_thread_node(_CHILD_SESSION_ID, _CHILD_MAIN_THREAD_ID)
    assert not (child_node / "debug" / "node" / "manifest.json").exists()
    store.write_manifest(NodeDebugSessionManifestDTO(
        session_id=_CHILD_SESSION_ID,
        thread_id=_CHILD_MAIN_THREAD_ID,
        updated_at=now,
    ))
    assert (child_node / "debug" / "node" / "manifest.json").is_file()


def test_claim_phase_transitions_use_plugin_run_identity() -> None:
    pending = _claim()
    with pytest.raises(RuntimeError, match="必须收到插件进程身份回执"):
        claim_running(pending)

    target_pid = claim_with_target_pid(pending, pid=4242)
    running = claim_running(target_pid)
    assert running.phase == "running"
    assert running.pid == 4242
    assert running.run_id == pending.run_id
    assert running.process_instance_id == pending.process_instance_id

    lost = claim_marked(running, phase="execution_lost", reason="原run状态无法查询")
    assert lost.phase == "execution_lost"
    assert lost.reconcile_reason == "原run状态无法查询"
    settled = claim_marked(running, phase="settled")
    assert settled.phase == "settled" and settled.reconcile_reason is None
