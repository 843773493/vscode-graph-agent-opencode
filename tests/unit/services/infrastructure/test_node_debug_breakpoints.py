from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.public_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationCreateRequest,
)
from app.services.infrastructure.node_debug_breakpoints import (
    anchor_breakpoint,
    reconcile_breakpoint,
)
from app.services.infrastructure.node_debug_service import NodeDebugService
from app.services.infrastructure.node_debug_session_store import NodeDebugSessionStore


class _SessionPathResolverStub:
    def __init__(self, session_root: Path) -> None:
        self._session_root = session_root

    def resolve_session_node(self, session_id: str) -> Path:
        path = self._session_root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _breakpoint(path: str, line: int) -> NodeDebugBreakpointDTO:
    return NodeDebugBreakpointDTO(
        breakpoint_id="node-bp-test",
        path=path,
        line=line,
        original_line=line,
        created_at=datetime.now(UTC),
    )


def test_breakpoint_becomes_invalid_after_lines_are_inserted(tmp_path: Path) -> None:
    source = tmp_path / "worker.mjs"
    source.write_text(
        "export function compute(value) {\n  const doubled = value * 2;\n  return doubled;\n}\n",
        encoding="utf-8",
    )
    anchored = anchor_breakpoint(_breakpoint("worker.mjs", 2), source)

    source.write_text(
        "// 新增说明\nconst offset = 0;\nexport function compute(value) {\n  const doubled = value * 2;\n  return doubled + offset;\n}\n",
        encoding="utf-8",
    )
    invalidated = reconcile_breakpoint(anchored, source)

    assert invalidated.line == 2
    assert invalidated.original_line == 2
    assert invalidated.relocation_status == "pending_update"
    assert invalidated.relocation_message is not None
    assert "未自动重定位" in invalidated.relocation_message
    assert invalidated.verified is False


def test_invalidated_breakpoint_does_not_auto_recover(tmp_path: Path) -> None:
    source = tmp_path / "worker.mjs"
    source.write_text("const value = 1;\nrun(value);\n", encoding="utf-8")
    anchored = anchor_breakpoint(_breakpoint("worker.mjs", 2), source)

    source.write_text("// changed\nconst value = 1;\nrun(value);\n", encoding="utf-8")
    invalidated = reconcile_breakpoint(anchored, source)
    restored = reconcile_breakpoint(invalidated, source)

    assert restored == invalidated
    assert restored.line == 2
    assert restored.relocation_status == "pending_update"


def test_breakpoint_marks_ambiguous_and_deleted_source(tmp_path: Path) -> None:
    source = tmp_path / "worker.mjs"
    source.write_text("const value = 1;\nrun(value);\n", encoding="utf-8")
    anchored = anchor_breakpoint(_breakpoint("worker.mjs", 2), source)

    source.write_text("run(value);\nrun(value);\n", encoding="utf-8")
    ambiguous = reconcile_breakpoint(anchored, source)
    assert ambiguous.relocation_status == "pending_update"

    source.unlink()
    deleted = reconcile_breakpoint(ambiguous, source)
    assert deleted.relocation_status == "source_deleted"


@pytest.mark.asyncio
async def test_pending_breakpoints_are_restored_from_session_store(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    source = workspace_root / "entry.mjs"
    source.write_text("const answer = 42;\nconsole.log(answer);\n", encoding="utf-8")
    resolver = _SessionPathResolverStub(tmp_path / "sessions")
    store = NodeDebugSessionStore(resolver)
    session_id = "session-debug-store"

    first = NodeDebugService(workspace_root=workspace_root, session_store=store)
    created = await first.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=session_id,
            name="入口调试",
            script_path="entry.mjs",
        )
    )
    first_state = await first.apply_action(
        session_id=session_id,
        action="set_breakpoint",
        params={
            "path": "entry.mjs",
            "line": 2,
            "condition": "answer > 0",
            "hit_condition": 2,
            "log_message": "answer={answer}",
        },
    )
    assert first_state.configuration_revision > 0
    assert first_state.active_configuration_id == created.active_configuration_id

    restored = NodeDebugService(workspace_root=workspace_root, session_store=store)
    restored_state = await restored.get_state(session_id)
    assert restored_state.status == "idle"
    assert restored_state.breakpoints[0].path == "entry.mjs"
    assert restored_state.breakpoints[0].line == 2
    assert restored_state.breakpoints[0].condition == "answer > 0"
    assert restored_state.breakpoints[0].hit_condition == 2
    assert restored_state.breakpoints[0].log_message == "answer={answer}"
    assert restored_state.configuration_revision == first_state.configuration_revision


@pytest.mark.asyncio
async def test_multiple_configurations_are_isolated_and_portable(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "first.mjs").write_text(
        "console.log('first');\n", encoding="utf-8"
    )
    (workspace_root / "second.mjs").write_text(
        "console.log('second');\n", encoding="utf-8"
    )
    resolver = _SessionPathResolverStub(tmp_path / "sessions")
    store = NodeDebugSessionStore(resolver)
    service = NodeDebugService(workspace_root=workspace_root, session_store=store)

    first = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id="source-session",
            name="第一套",
            script_path="first.mjs",
        )
    )
    first_id = first.active_configuration_id
    assert first_id is not None
    await service.apply_action(
        session_id="source-session",
        action="set_breakpoint",
        params={"path": "first.mjs", "line": 1},
    )
    second = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id="source-session",
            name="第二套",
            script_path="second.mjs",
        )
    )
    second_id = second.active_configuration_id
    assert second_id is not None and second_id != first_id
    assert second.breakpoints == []
    assert second.script_path == "second.mjs"

    restored_first = await service.activate_configuration(
        "source-session",
        first_id,
    )
    assert [breakpoint.path for breakpoint in restored_first.breakpoints] == [
        "first.mjs"
    ]
    assert restored_first.script_path == "first.mjs"

    copied = await service.copy_configuration(
        source_session_id="source-session",
        target_session_id="target-session",
        configuration_id=first_id,
        activate=True,
    )
    target = await service.get_state("target-session")
    assert target.active_configuration_id == copied.configuration_id
    assert target.active_configuration_name == "第一套"
    assert copied.model_dump().keys().isdisjoint({"session_id", "pid", "actions"})
    assert (
        copied.breakpoints[0]
        .model_dump()
        .keys()
        .isdisjoint({"verified", "actual_line", "inspector_id"})
    )
    copied_path = (
        tmp_path
        / "sessions"
        / "target-session"
        / "debug"
        / "node"
        / "configurations"
        / f"{copied.configuration_id}.json"
    )
    assert copied_path.is_file()


@pytest.mark.asyncio
async def test_legacy_single_configuration_file_is_not_loaded(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    resolver = _SessionPathResolverStub(tmp_path / "sessions")
    session_node = resolver.resolve_session_node("legacy-session")
    legacy_file = session_node / "debug" / "node.json"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text(
        '{"session_id":"legacy-session","script_path":"old.mjs"}',
        encoding="utf-8",
    )

    service = NodeDebugService(
        workspace_root=workspace_root,
        session_store=NodeDebugSessionStore(resolver),
    )
    state = await service.get_state("legacy-session")
    assert state.configurations == []
    assert state.active_configuration_id is None


@pytest.mark.asyncio
async def test_configuration_file_can_be_copied_directly_into_loaded_session(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "portable.mjs").write_text(
        "console.log('portable');\n",
        encoding="utf-8",
    )
    sessions_root = tmp_path / "sessions"
    resolver = _SessionPathResolverStub(sessions_root)
    store = NodeDebugSessionStore(resolver)
    service = NodeDebugService(workspace_root=workspace_root, session_store=store)

    source_state = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id="source-session",
            name="可复制方案",
            script_path="portable.mjs",
        )
    )
    configuration_id = source_state.active_configuration_id
    assert configuration_id is not None

    empty_target = await service.get_state("target-session")
    assert empty_target.configurations == []
    source_file = (
        sessions_root
        / "source-session"
        / "debug"
        / "node"
        / "configurations"
        / f"{configuration_id}.json"
    )
    target_directory = (
        sessions_root / "target-session" / "debug" / "node" / "configurations"
    )
    target_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, target_directory / source_file.name)

    discovered = await service.get_state("target-session")
    assert [item.configuration_id for item in discovered.configurations] == [
        configuration_id
    ]
    assert discovered.active_configuration_id is None
    activated = await service.activate_configuration(
        "target-session",
        configuration_id,
    )
    assert activated.active_configuration_name == "可复制方案"
    assert activated.script_path == "portable.mjs"


@pytest.mark.asyncio
async def test_duplicate_breakpoint_is_rejected_for_shared_operators(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "shared.mjs").write_text(
        "const value = 23;\nconsole.log(value);\n",
        encoding="utf-8",
    )
    service = NodeDebugService(workspace_root=workspace_root)

    await service.apply_action(
        session_id="shared-session",
        action="set_breakpoint",
        params={"path": "shared.mjs", "line": 2},
        actor="human",
    )
    with pytest.raises(ValueError, match="源码断点已存在"):
        await service.apply_action(
            session_id="shared-session",
            action="set_breakpoint",
            params={
                "path": "shared.mjs",
                "line": 2,
                "condition": "value > 0",
            },
            actor="ai",
        )

    state = await service.get_state("shared-session")
    assert [(item.path, item.line) for item in state.breakpoints] == [("shared.mjs", 2)]


@pytest.mark.asyncio
async def test_pending_breakpoint_can_be_atomically_changed_to_logpoint(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "editable.mjs").write_text(
        "const value = 23;\nconsole.log(value);\n",
        encoding="utf-8",
    )
    service = NodeDebugService(workspace_root=workspace_root)

    created = await service.apply_action(
        session_id="editable-session",
        action="set_breakpoint",
        params={"path": "editable.mjs", "line": 2},
    )
    breakpoint_id = created.breakpoints[0].breakpoint_id
    updated = await service.apply_action(
        session_id="editable-session",
        action="update_breakpoint",
        params={
            "breakpoint_id": breakpoint_id,
            "condition": "value > 10",
            "hit_condition": 3,
            "log_message": "value={value}",
        },
    )

    assert len(updated.breakpoints) == 1
    assert updated.breakpoints[0].breakpoint_id == breakpoint_id
    assert updated.breakpoints[0].condition == "value > 10"
    assert updated.breakpoints[0].hit_condition == 3
    assert updated.breakpoints[0].log_message == "value={value}"
    assert updated.actions[-1].action == "update_breakpoint"
