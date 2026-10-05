from __future__ import annotations

import asyncio
import logging
from urllib.error import URLError

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.terminal_execution_monitor import (
    TerminalExecutionMonitorClientProtocol,
)
from app.core.exceptions import NotFoundError
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.shielded_cleanup import run_cleanup_shielded
from app.prompting.factory import internal_message_factory
from app.services.business.workspace_resources.owner import ResourceConflict

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
        except Exception as error:
            logger.error(
                "终端 steering 监控任务关闭时发现未处理异常",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def scan_once(self) -> None:
        errors: list[Exception] = []
        try:
            await self._terminal_client.sweep_orphans()
        except ExceptionGroup as error:
            errors.append(error)
        terminals = await self._terminal_client.list_resources()
        for terminal in terminals:
            if not self._is_candidate(terminal):
                continue
            try:
                await self._dispatch_terminal(terminal)
            except (SessionDeletionPendingError, NotFoundError) as error:
                logger.info("Terminal 来源 Session 已关闭准入，跳过完成通知: %s", error)
            except (ConnectionError, TimeoutError, URLError, ResourceConflict) as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Terminal owner 扫描存在未收敛操作", errors)

    async def _dispatch_terminal(self, terminal: dict[str, object]) -> None:
        terminal_id = self._required_string(terminal, "terminal_id")
        owner_session_id = self._required_string(terminal, "session_id")
        async with self._terminal_client.operation(
            session_id=owner_session_id,
            thread_id=self._required_string(terminal, "source_thread_id"),
            holder_id=f"terminal-monitor:{terminal_id}",
            operation_id=self._required_string(terminal, "completion_event_id"),
        ) as operation:
            claim = await operation.claim_terminal_steering(terminal_id)
            if claim.get("claimed") is not True:
                return
            dispatched = False

            async def finish() -> None:
                await operation.finish_terminal_steering(terminal_id, dispatched=dispatched)

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
                            "terminal_completion_event_id": self._required_string(terminal, "completion_event_id"),
                            "terminal_id": terminal_id,
                        },
                    ),
                    delivery_policy="after_tool_result",
                    thread_id=self._required_string(terminal, "source_thread_id"),
                )
                dispatched = True
            finally:
                await run_cleanup_shielded(finish)

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, TimeoutError, URLError, ResourceConflict, ExceptionGroup) as error:
                logger.warning(
                    "终端 owner 监控存在未收敛操作，将重试: %s",
                    error,
                )
            await asyncio.sleep(self._poll_interval_seconds)

    @staticmethod
    def _is_candidate(terminal: dict[str, object]) -> bool:
        return (
            terminal.get("status") in {"running", "completed"}
            and terminal.get("reconciliation_status") == "verified"
            and terminal.get("source_thread") in terminal.get("thread_associations", [])
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
