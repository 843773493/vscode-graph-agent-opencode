"""Node Debug 启动/重启入口链路。

承载 start/restart 两条产品入口与启动编排器的构造：owner 准入后在 per-owner
临界区内执行整段启动序列（核实旧登记 → durable 登记 → spawn → 身份核对 → 握手）。
由 NodeDebugService 继承（宿主必须提供全部协作者与装配字段），不反向依赖顶层
service.py。
"""

from __future__ import annotations

from typing import Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug.process.launch_orchestrator import (
    NodeDebugLaunchContext,
    NodeDebugLaunchOrchestrator,
    NodeDebugLaunchRequest,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
)


class NodeDebugLaunchEntryMixin:
    """启动/重启入口链路的方法族（由 NodeDebugService 继承）。"""

    async def start(
        self,
        *,
        session_id: str,
        path: str,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        thread_id: str,
        configuration_id: str | None = None,
        launch_profile_name: str | None = None,
        working_directory: str | None = None,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        """公开启动入口：Session 准入后，按 owner 串行执行整段启动序列。

        per-owner 临界区覆盖"核实旧登记 → durable 登记 → spawn → 身份核对 → 握手 → running"。
        并发 start（Web 与 Agent 同时按下）否则会各自登记一代 claim 并各自 spawn 进程：
        后写的登记会覆盖先启动实例的登记，先启动的进程随之游离，Inspector 端口也被抢。
        R5b 起同一临界区也覆盖 ``apply_action("stop")``、``restart()`` 与 ``close()`` 的
        停止路径：stop 落在 spawn 窗口不会再产生"exited + 进程存活"的假终态。临界区内
        的 await 全部有界（旧实例停止的 terminate/kill 核实各有 3s 超时、socket.close
        与已 cancel 任务的 gather 均有界），不会与 ``_runtimes_lock`` 形成反向持锁
        （``_owner_lock → _runtimes_lock → runtime.state_lock`` 单向）。
        """
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        async with self._owner_lock(owner):
            result = await self._create_launch_orchestrator().launch(
                NodeDebugLaunchRequest(
                    owner=owner,
                    path=path,
                    args=args,
                    breakpoints=breakpoints,
                    configuration_id=configuration_id,
                    launch_profile_name=launch_profile_name,
                    working_directory=working_directory,
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
            )
            return await self.get_state(*result.owner)

    async def restart(
        self,
        session_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        # 重启 = 停止旧实例 + 启动新实例，必须整体处于 per-owner 临界区内（R3b 复核
        # 残留项）：否则 stop 落在另一并发启动的 spawn 窗口时会得到假终态。临界区内
        # 直接调用启动编排器，绝不能再经 `start()` 重入同一把 asyncio.Lock
        # （不可重入，重入即自锁）。
        async with self._owner_lock(owner):
            self._session_state.ensure_loaded(session_id, thread_id)
            runtime = self._runtimes.get(owner)
            if runtime is None:
                self._assert_no_unsettled_claim(owner, operation="重启调试")
                raise RuntimeError(
                    f"Node 调试会话不存在: session_id={session_id}, thread_id={thread_id}"
                )
            async with runtime.state_lock:
                path = runtime.relative_script_path
                args = list(runtime.args)
                configuration_id = runtime.configuration_id
                launch_profile_name = runtime.launch_profile_name
                working_directory = (
                    str(runtime.working_directory)
                    if runtime.working_directory is not None
                    else ""
                )
                breakpoints = [
                    NodeDebugBreakpointRequest(
                        path=breakpoint.path,
                        line=breakpoint.line,
                        column=breakpoint.column,
                        condition=breakpoint.condition,
                        hit_condition=breakpoint.hit_condition,
                        log_message=breakpoint.log_message,
                    )
                    for breakpoint in runtime.breakpoints.values()
                    if breakpoint.relocation_status == "current"
                ]
            outcome = await self._lifecycle.stop_runtime(runtime)
            if outcome == "reconcile_required":
                # 旧实例无法核实终态：保持 reconcile_required，绝不为同一 owner 启动新实例。
                self._session_state.persist_runtime_state(
                    session_id, thread_id, runtime
                )
                raise RuntimeError(
                    "旧调试实例无法核实终态，保持 reconcile_required；拒绝重启: "
                    f"session_id={session_id}, thread_id={thread_id}"
                )
            async with runtime.state_lock:
                runtime.status = "exited"
            result = await self._create_launch_orchestrator().launch(
                NodeDebugLaunchRequest(
                    owner=owner,
                    path=path,
                    args=args,
                    breakpoints=breakpoints,
                    configuration_id=configuration_id,
                    launch_profile_name=launch_profile_name,
                    working_directory=working_directory,
                    actor=actor,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
            )
            return await self.get_state(*result.owner)

    def _create_launch_orchestrator(self) -> NodeDebugLaunchOrchestrator:
        """为本次启动读取最新 service 回调，避免缓存旧的 monkeypatch/运行态。"""
        return NodeDebugLaunchOrchestrator(
            NodeDebugLaunchContext(
                workspace_root=self._workspace_root,
                node_bin=self._node_bin,
                configuration_factory=self._configuration_factory,
                breakpoint_mutations=self._breakpoint_mutations,
                inspector=self._inspector,
                lifecycle=self._lifecycle,
                runtimes=self._runtimes,
                session_state=self._session_state,
                set_selection=self._configuration_registry.set_selection,
                runtimes_lock=self._runtimes_lock,
                max_actions=MAX_NODE_DEBUG_ACTIONS,
                load_session=self._session_state.ensure_loaded,
                reconcile_sources=self._reconcile_session_sources,
                select_configuration=self._configuration_registry.select_for_start,
                read_configuration=self._configuration_registry.get,
                read_runtime_config=self._get_typed_debug_runtime_config,
                read_source_digests=self._source_digests_for_runtime,
                persist_state=self._session_state.persist_runtime_state,
                write_claim=self._claim_runtime.write_launch_claim,
                mark_claim_running=self._claim_runtime.mark_claim_running,
                append_action=self._append_action,
                is_paused_at_breakpoint=self._observer.paused_at_breakpoint,
                read_stream=self._observer.read_stream,
                monitor_process=self._observer.monitor_process,
                terminal_error_message=self._observer.terminal_error_message,
                handshake_timeout_message=self._observer.handshake_timeout_message,
            )
        )
