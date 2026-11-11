"""Node Debug 插件事件到 Backend 状态与 DAP 输出的投影。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugPluginRuntimeProtocol,
    NodeDebugRuntime,
    confirm_target_exit,
)
from tests.support.node_debug_dependencies import (
    TEST_THREAD_ID,
    node_debug_binding_snapshot,
)


class PluginEvents:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1

    async def events(self) -> AsyncIterator[dict[str, object]]:
        for event in self._events:
            yield event


class Recorder:
    def __init__(self) -> None:
        self.phases: list[tuple[str, str | None]] = []
        self.actions: list[tuple[str, str]] = []

    async def mark(self, runtime: NodeDebugRuntime, phase: str, reason: str | None = None) -> None:
        self.phases.append((phase, reason))

    def action(self, runtime: NodeDebugRuntime, action: str, message: str, **_kwargs: object) -> None:
        self.actions.append((action, message))

    def clear(self, runtime: NodeDebugRuntime) -> None:
        return None


def runtime(tmp_path: Path, plugin: PluginEvents) -> NodeDebugRuntime:
    binding = node_debug_binding_snapshot()
    result = NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        debugger_binding=binding,
        workspace_root=tmp_path,
        script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs",
    )
    result.plugin_runtime = cast(NodeDebugPluginRuntimeProtocol, plugin)
    return result


def observer(recorder: Recorder) -> NodeDebugRuntimeObserver:
    return NodeDebugRuntimeObserver(
        mark_claim_phase=recorder.mark,
        clear_stop_snapshot=recorder.clear,
        append_action=recorder.action,
        max_output_lines=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_already_confirmed", [False, True])
async def test_plugin_output_is_bounded_and_exit_settles_run(
    tmp_path: Path, stop_already_confirmed: bool,
) -> None:
    recorder = Recorder()
    plugin = PluginEvents([
        {"event": "stdout", "payload": {"text": "one\ntwo\nthree\n"}},
        {"event": "process.exited", "payload": {"exit_code": 0}},
    ])
    target = runtime(tmp_path, plugin)
    events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    target.dap_subscribers.add(events)
    if stop_already_confirmed:
        confirm_target_exit(target, 0)

    await observer(recorder).monitor_process(target)

    assert target.output == ["two", "three"]
    assert target.status == "exited"
    assert target.target_exit_code == 0
    assert target.target_exited
    assert target.plugin_runtime is plugin
    assert plugin.closed == 0
    assert recorder.phases == [("settled", "Debug 插件已确认目标进程退出")]
    published = [events.get_nowait() for _ in range(events.qsize())]
    assert [event for event in published if event["event"] in {"exited", "terminated"}] == [
        {"event": "exited", "body": {"exitCode": 0}},
        {"event": "terminated", "body": {}},
    ]


@pytest.mark.asyncio
async def test_lost_plugin_channel_does_not_claim_target_exit(tmp_path: Path) -> None:
    recorder = Recorder()
    target = runtime(tmp_path, PluginEvents([]))

    await observer(recorder).monitor_process(target)

    assert target.status == "execution_lost"
    assert target.target_exit_code is None
    assert not target.target_exited
    assert recorder.phases == [("execution_lost", target.error_message)]
