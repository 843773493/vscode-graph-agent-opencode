'''Node Debug 源码对账模块的分支覆盖单元测试。

直接构造 NodeDebugSourceReconciliation，注入记录型假 command 回调和假
append_action，精确打靶 Inspector 失效清理、源码变化标记、pending 分支和
relocation 兜底文案，不依赖真实 Inspector 进程。
'''

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.node_debug import NodeDebugBreakpointDTO
from app.services.infrastructure.node_debug.breakpoint.source_reconciliation import (
    NodeDebugSourceReconciliation,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)

_SESSION_ID = 'ses_0000000000000000000000000000c0de'
_THREAD_ID = 'main'
_CONFIGURATION_ID = 'dbgcfg_' + '1' * 32


@dataclass(slots=True)
class CommandCall:
    '''一次假 command 调用记录。'''

    runtime: NodeDebugRuntime
    method: str
    params: dict[str, object] | None


@dataclass(slots=True)
class ActionRecord:
    '''一次假 append_action 记录。'''

    runtime: NodeDebugRuntime
    action: str
    message: str
    actor: str
    result: str


class RecordingCommand:
    '''记录调用的方法名与参数，并可按方法名抛出异常。'''

    def __init__(self, *, failing_methods: set[str] | None = None) -> None:
        self.calls: list[CommandCall] = []
        self.failing_methods: set[str] = failing_methods or set()

    async def __call__(
        self,
        runtime: NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append(CommandCall(runtime, method, params))
        if method in self.failing_methods:
            raise RuntimeError(f'{method} 调用失败')
        return {}

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


class RecordingActionAppender:
    '''记录动作名、文案、actor 与 result。'''

    def __init__(self) -> None:
        self.records: list[ActionRecord] = []

    def __call__(
        self,
        runtime: NodeDebugRuntime,
        action: str,
        message: str,
        *,
        actor: str = 'human',
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        extension_catalog_binding: object = None,
        result: str = 'success',
    ) -> None:
        self.records.append(
            ActionRecord(runtime, action, message, actor, result)
        )

    def actions(self) -> list[str]:
        return [record.action for record in self.records]

    def find(self, action: str) -> ActionRecord:
        for record in self.records:
            if record.action == action:
                return record
        raise AssertionError(f'未找到动作 {action}，实际为 {self.actions()}')


@dataclass(slots=True)
class PersistCall:
    session_id: str
    thread_id: str
    runtime: NodeDebugRuntime | None


class RecordingSessionState(NodeDebugSessionState):
    '''在真实会话状态之上记录 pending 与持久化写入。'''

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.persist_calls: list[PersistCall] = []
        self.pending_action_calls: list[tuple[str, str, str, str, str]] = []
        self.set_pending_calls: list[tuple[tuple[str, str], list[str]]] = []

    def persist_runtime_state(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        self.persist_calls.append(PersistCall(session_id, thread_id, runtime))

    def set_pending_breakpoints(
        self, owner: tuple[str, str], breakpoints: object
    ) -> None:
        items = list(breakpoints)  # type: ignore[arg-type]
        self.set_pending_calls.append((owner, [item.path for item in items]))
        super().set_pending_breakpoints(owner, items)

    def append_pending_action(
        self,
        session_id: str,
        thread_id: str,
        action: str,
        message: str,
        *,
        actor: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        extension_catalog_binding: object = None,
        result: str = 'success',
    ) -> None:
        self.pending_action_calls.append((session_id, thread_id, action, message, actor))
        super().append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor=actor,  # type: ignore[arg-type]
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            extension_catalog_binding=extension_catalog_binding,  # type: ignore[arg-type]
            result=result,  # type: ignore[arg-type]
        )


def _breakpoint(
    *,
    path: str,
    line: int = 2,
    source_digest: str | None,
    relocation_status: str = 'current',
    relocation_message: str | None = None,
    breakpoint_id: str = 'node-bp-source',
    source_line: str | None = 'const value = 1;',
) -> NodeDebugBreakpointDTO:
    return NodeDebugBreakpointDTO(
        breakpoint_id=breakpoint_id,
        path=path,
        line=line,
        original_line=line,
        source_line=source_line,
        source_digest=source_digest,
        relocation_status=relocation_status,  # type: ignore[arg-type]
        relocation_message=relocation_message,
        created_at=datetime.now(UTC),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding='utf-8')
    return _digest(path)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / 'workspace'
    root.mkdir()
    return root


@pytest.fixture
def entry_source(workspace_root: Path) -> Path:
    source = workspace_root / 'entry.mjs'
    _write(source, 'const answer = 42;\nconsole.log(answer);\n')
    return source


@pytest.fixture
def session_state(workspace_root: Path) -> RecordingSessionState:
    registry = NodeDebugConfigurationRegistry(
        store=None,
        configuration_factory=NodeDebugConfigurationFactory(
            workspace_root=workspace_root
        ),
    )
    return RecordingSessionState(configuration_registry=registry)


@pytest.fixture
def command() -> RecordingCommand:
    return RecordingCommand()


@pytest.fixture
def appender() -> RecordingActionAppender:
    return RecordingActionAppender()


@pytest.fixture
def reconciler(
    workspace_root: Path,
    session_state: RecordingSessionState,
    command: RecordingCommand,
    appender: RecordingActionAppender,
) -> NodeDebugSourceReconciliation:
    return NodeDebugSourceReconciliation(
        workspace_root=workspace_root,
        session_state=session_state,
        command=command,
        append_action=appender,
    )


def _runtime(
    workspace_root: Path,
    *,
    status: str = 'running',
    breakpoints: list[NodeDebugBreakpointDTO] | None = None,
    inspector_ids: dict[str, str] | None = None,
    socket: object | None = None,
) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id=_SESSION_ID,
        thread_id=_THREAD_ID,
        configuration_id=_CONFIGURATION_ID,
        workspace_root=workspace_root,
        script_path=workspace_root / 'entry.mjs',
        relative_script_path='entry.mjs',
        status=status,  # type: ignore[arg-type]
    )
    runtime.breakpoints = {
        item.breakpoint_id: item for item in (breakpoints or [])
    }
    runtime.inspector_breakpoint_ids = dict(inspector_ids or {})
    runtime.inspector.socket = socket  # type: ignore[assignment]
    return runtime


async def test_breakpoint_invalidation_failure_degrades_without_blocking(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    session_state: RecordingSessionState,
    command: RecordingCommand,
    appender: RecordingActionAppender,
) -> None:
    """Inspector 清理失败时降级为 error 动作，且不阻断后续持久化。"""
    # 断点锚定的摘要与磁盘不一致，触发 pending_update 失效。
    stale = _breakpoint(path="entry.mjs", source_digest="stale-digest")
    runtime = _runtime(
        workspace_root,
        status="running",
        breakpoints=[stale],
        inspector_ids={stale.breakpoint_id: "inspector-bp-1"},
        socket=object(),
    )
    command.failing_methods.add("Debugger.removeBreakpoint")

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    assert command.methods == ["Debugger.removeBreakpoint"]
    assert command.calls[0].params == {"breakpointId": "inspector-bp-1"}
    failure = appender.find("breakpoint_invalidation_failed")
    assert failure.actor == "system"
    assert failure.result == "error"
    assert failure.message == (
        "清理失效 Inspector 断点失败: Debugger.removeBreakpoint 调用失败"
    )
    # 失效清理失败不能阻断后续流程：断点对账动作与持久化都照常进行。
    assert "breakpoint_reconciled" in appender.actions()
    assert (
        runtime.breakpoints[stale.breakpoint_id].relocation_status == "pending_update"
    )
    assert runtime.inspector_breakpoint_ids == {}
    assert session_state.persist_calls == [
        PersistCall(_SESSION_ID, _THREAD_ID, runtime)
    ]


async def test_invalidated_breakpoint_without_inspector_id_is_not_removed(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    command: RecordingCommand,
    appender: RecordingActionAppender,
) -> None:
    """失效断点在运行时没有 Inspector 绑定 id 时不得调用 removeBreakpoint。"""
    stale = _breakpoint(path="entry.mjs", source_digest="stale-digest")
    runtime = _runtime(
        workspace_root,
        status="running",
        breakpoints=[stale],
        socket=object(),
    )

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    assert (
        runtime.breakpoints[stale.breakpoint_id].relocation_status == "pending_update"
    )
    assert command.calls == []
    assert "breakpoint_invalidation_failed" not in appender.actions()
    assert "breakpoint_reconciled" in appender.actions()


async def test_relocation_message_falls_back_to_default_text(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    appender: RecordingActionAppender,
) -> None:
    """relocation_message 为空时回退为默认位置文案。"""
    # 首次锚定：没有摘要和源码行，anchor_breakpoint 不产生 relocation_message。
    unanchored = _breakpoint(
        path="entry.mjs",
        source_digest=None,
        breakpoint_id="node-bp-anchor",
        source_line=None,
    )
    runtime = _runtime(workspace_root, status="running", breakpoints=[unanchored])

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    reconciled = appender.find("breakpoint_reconciled")
    assert reconciled.message == "断点状态已更新: entry.mjs:2"
    assert reconciled.actor == "system"
    assert reconciled.result == "success"
    assert (
        runtime.breakpoints[unanchored.breakpoint_id].source_line
        == "console.log(answer);"
    )


async def test_missing_inspector_socket_skips_breakpoint_removal(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    command: RecordingCommand,
    appender: RecordingActionAppender,
) -> None:
    """inspector.socket 为 None 时应跳过 removeBreakpoint 调用。"""
    stale = _breakpoint(path="entry.mjs", source_digest="stale-digest")
    runtime = _runtime(
        workspace_root,
        status="running",
        breakpoints=[stale],
        inspector_ids={stale.breakpoint_id: "inspector-bp-1"},
        socket=None,
    )

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    assert command.calls == []
    assert "breakpoint_invalidation_failed" not in appender.actions()
    assert runtime.inspector_breakpoint_ids == {}
    assert (
        runtime.breakpoints[stale.breakpoint_id].relocation_status == "pending_update"
    )


async def test_source_change_marks_restart_and_appends_action(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    session_state: RecordingSessionState,
    appender: RecordingActionAppender,
) -> None:
    """活跃运行时的磁盘源码变化应置位 requires_restart 并追加 source_changed。"""
    runtime = _runtime(workspace_root, status="paused")
    runtime.loaded_source_digests = {"entry.mjs": "digest-at-launch"}

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    assert runtime.requires_restart is True
    assert runtime.source_changed_paths == {"entry.mjs"}
    changed = appender.find("source_changed")
    assert changed.message == (
        "磁盘源码已变化，相关断点已失效；如需运行新源码可重启调试: entry.mjs"
    )
    assert changed.actor == "system"
    assert changed.result == "success"
    assert session_state.persist_calls == [
        PersistCall(_SESSION_ID, _THREAD_ID, runtime)
    ]


async def test_second_reconcile_does_not_repeat_source_changed_action(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    session_state: RecordingSessionState,
    appender: RecordingActionAppender,
) -> None:
    """源码已标记过时二次对账不得重复追加 source_changed。"""
    runtime = _runtime(workspace_root, status="starting")
    runtime.loaded_source_digests = {"entry.mjs": "digest-at-launch"}

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)
    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, runtime)

    assert appender.actions().count("source_changed") == 1
    assert len(session_state.persist_calls) == 1
    assert runtime.source_changed_paths == {"entry.mjs"}


async def test_pending_branch_uses_pending_breakpoints_and_pending_actions(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
    session_state: RecordingSessionState,
    appender: RecordingActionAppender,
) -> None:
    """runtime 为 None 时对账待安装断点并写入 pending 动作。"""
    unanchored = _breakpoint(
        path="entry.mjs",
        source_digest=None,
        breakpoint_id="node-bp-pending",
        source_line=None,
    )
    session_state.set_pending_breakpoints((_SESSION_ID, _THREAD_ID), [unanchored])
    session_state.set_pending_calls.clear()

    await reconciler.reconcile(_SESSION_ID, _THREAD_ID, None)

    assert session_state.set_pending_calls == [
        ((_SESSION_ID, _THREAD_ID), ["entry.mjs"])
    ]
    stored = session_state.pending_breakpoints((_SESSION_ID, _THREAD_ID))
    assert stored[0].line == 2
    assert stored[0].source_line == "console.log(answer);"
    assert session_state.pending_action_calls == [
        (
            _SESSION_ID,
            _THREAD_ID,
            "breakpoint_reconciled",
            "断点状态已更新: entry.mjs:2",
            "system",
        )
    ]
    assert appender.records == []
    assert session_state.persist_calls == [PersistCall(_SESSION_ID, _THREAD_ID, None)]


async def test_source_digests_cover_script_and_breakpoints(
    workspace_root: Path,
    entry_source: Path,
    reconciler: NodeDebugSourceReconciliation,
) -> None:
    """source_digests 汇总脚本与全部断点源码的当前摘要。"""
    other = workspace_root / "helper.mjs"
    other_digest = _write(other, "export const helper = 1;\n")
    entry_digest = _digest(entry_source)
    missing = _breakpoint(
        path="gone.mjs", source_digest=None, breakpoint_id="node-bp-missing"
    )
    runtime = _runtime(
        workspace_root,
        status="running",
        breakpoints=[
            _breakpoint(path="helper.mjs", source_digest=other_digest),
            missing,
        ],
    )

    assert reconciler.source_digests(runtime) == {
        "entry.mjs": entry_digest,
        "helper.mjs": other_digest,
        "gone.mjs": None,
    }
