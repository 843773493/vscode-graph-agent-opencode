from __future__ import annotations

import asyncio
import logging

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.terminal_execution_monitor import (
    TerminalExecutionMonitorClientProtocol,
)
from app.prompting.factory import internal_message_factory

logger = logging.getLogger(__name__)


class TerminalSteeringService:
    def __init__(
        self,
        *,
        terminal_client: TerminalExecutionMonitorClientProtocol,
        session_orchestrator: SessionOrchestratorProtocol,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        self._terminal_client = terminal_client
        self._session_orchestrator = session_orchestrator
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("TerminalSteeringService 已经启动")
        self._task = asyncio.create_task(
            self._run(),
            name="terminal-steering-monitor",
        )
        self._task.add_done_callback(self._report_failure)

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def scan_once(self) -> None:
        terminals = await self._terminal_client.list_terminals()
        for terminal in terminals:
            if not self._is_candidate(terminal):
                continue
            terminal_id = self._required_string(terminal, "terminal_id")
            owner_session_id = self._required_string(terminal, "session_id")
            claim = await self._terminal_client.claim_terminal_steering(terminal_id)
            if claim.get("claimed") is not True:
                continue
            try:
                await self._session_orchestrator.create_and_run_internal(
                    owner_session_id,
                    internal_message_factory.build(
                        kind="terminal_execution_completed",
                        control=(
                            "后台终端命令已经结束。使用 write_stdin 读取 session_id="
                            f"{terminal_id} 的最终未消费输出，并根据结果继续完成当前任务。"
                            "不要仅根据完成通知推断命令成功。"
                        ),
                        metadata={
                            "terminal_completion_event_id": self._required_string(
                                terminal,
                                "completion_event_id",
                            ),
                            "terminal_id": terminal_id,
                        },
                    ),
                    delivery_policy="after_tool_result",
                )
            except BaseException:
                await self._terminal_client.finish_terminal_steering(
                    terminal_id,
                    dispatched=False,
                )
                raise
            await self._terminal_client.finish_terminal_steering(
                terminal_id,
                dispatched=True,
            )

    async def _run(self) -> None:
        while True:
            await self.scan_once()
            await asyncio.sleep(self._poll_interval_seconds)

    @staticmethod
    def _is_candidate(terminal: dict[str, object]) -> bool:
        return (
            terminal.get("status") in {"running", "completed"}
            and terminal.get("model_backgrounded") is True
            and terminal.get("last_command_status") == "completed"
            and terminal.get("completion_observed_by_model") is not True
            and terminal.get("steering_dispatching") is not True
            and terminal.get("steering_dispatched") is not True
        )

    @staticmethod
    def _required_string(terminal: dict[str, object], field: str) -> str:
        value = terminal.get(field)
        if not isinstance(value, str) or not value:
            raise TypeError(f"终端 completion 记录缺少 {field}: {terminal}")
        return value

    @staticmethod
    def _report_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.critical(
                "终端 steering 监控异常退出",
                exc_info=(type(error), error, error.__traceback__),
            )


__all__ = ["TerminalSteeringService"]
