from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.prompting import PromptSection, internal_message_factory
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.session import SessionDTO
from app.schemas.public_v2.session_navigation import (
    SessionGenerationExecuteRequest,
    SessionGenerationExecuteResultDTO,
    SessionGenerationOutputDTO,
)

logger = logging.getLogger(__name__)


class SessionGenerationReportingSupport:
    """封装生成分支终态监听、结果回写与报告任务生命周期。"""

    def _schedule_report_back(
        self,
        payload: SessionGenerationExecuteRequest,
        *,
        ledger_path: Path,
        target_session_id: str,
        generated_session_id: str,
        generated_job_id: str,
        existing_report_back_job_id: str | None,
    ) -> None:
        existing = self._report_tasks.get(ledger_path)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._report_back_after_job(
                payload,
                ledger_path=ledger_path,
                target_session_id=target_session_id,
                generated_session_id=generated_session_id,
                generated_job_id=generated_job_id,
                existing_report_back_job_id=existing_report_back_job_id,
            ),
            name=f"generation-report-back:{payload.run_id}",
        )
        self._report_tasks[ledger_path] = task
        task.add_done_callback(
            lambda completed, path=ledger_path: self._report_task_finished(
                path,
                completed,
            )
        )
        task.add_done_callback(self._log_report_task_failure)

    def _schedule_child_completion(
        self,
        payload: SessionGenerationExecuteRequest,
        *,
        ledger_path: Path,
        session_id: str,
        job_id: str,
    ) -> None:
        existing = self._report_tasks.get(ledger_path)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._complete_after_child_job(
                payload,
                ledger_path=ledger_path,
                session_id=session_id,
                job_id=job_id,
            ),
            name=f"generation-child-monitor:{payload.run_id}",
        )
        self._report_tasks[ledger_path] = task
        task.add_done_callback(
            lambda completed, path=ledger_path: self._report_task_finished(
                path,
                completed,
            )
        )
        task.add_done_callback(self._log_report_task_failure)

    async def _complete_after_child_job(
        self,
        payload: SessionGenerationExecuteRequest,
        *,
        ledger_path: Path,
        session_id: str,
        job_id: str,
    ) -> None:
        try:
            terminal_status = await self._wait_for_job_terminal(
                job_id,
                session_id=session_id,
                subscriber_kind="session_generation_completion",
                run_id=payload.run_id,
            )
            status = (
                "completed"
                if terminal_status == EventType.JOB_COMPLETED
                else "failed"
            )
            error = (
                None
                if status == "completed"
                else f"生成 Job 失败: {terminal_status}"
            )
            self._delete_message_intent(
                payload,
                session_id,
                self._execution_phase(payload),
            )
            self._update_child_ledger(
                ledger_path,
                status=status,
                error=error,
            )
        except Exception as error:
            self._update_child_ledger(
                ledger_path,
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            raise

    def _update_child_ledger(
        self,
        ledger_path: Path,
        *,
        status: str,
        error: str | None,
    ) -> None:
        record = self._read_ledger(ledger_path)
        raw_result = record.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError(f"会话生成运行记录缺少 result: {ledger_path}")
        result = SessionGenerationExecuteResultDTO.model_validate(raw_result)
        record["status"] = status
        record["result"] = result.model_copy(
            update={"status": status, "error": error}
        ).model_dump(mode="json")
        record["ended_at"] = datetime.now(timezone.utc).isoformat()
        self._write_ledger(ledger_path, record)

    @staticmethod
    def _execution_phase(payload: SessionGenerationExecuteRequest) -> str:
        mode = payload.session_strategy.mode
        if mode == "continue_existing":
            return "continue_existing"
        if mode == "fork_new_and_report_back":
            return "fork_child"
        return "new_session"

    async def _report_back_after_job(
        self,
        payload: SessionGenerationExecuteRequest,
        *,
        ledger_path: Path,
        target_session_id: str,
        generated_session_id: str,
        generated_job_id: str,
        existing_report_back_job_id: str | None,
    ) -> None:
        report_back_job_id = existing_report_back_job_id
        try:
            terminal_status = await self._wait_for_job_terminal(
                generated_job_id,
                session_id=generated_session_id,
                subscriber_kind="session_generation_report_back",
                run_id=payload.run_id,
            )
            self._delete_message_intent(
                payload,
                generated_session_id,
                "fork_child",
            )
            report_terminal_status = (
                await self._known_terminal_status(
                    target_session_id,
                    report_back_job_id,
                )
                if report_back_job_id is not None
                else None
            )
            if report_terminal_status is None:
                branch_result = await self._branch_result(generated_session_id)
                report_back_job_id = await self._dispatch_report_back_message(
                    payload,
                    target_session_id,
                    self._report_back_message(
                        payload,
                        generated_session_id=generated_session_id,
                        generated_job_id=generated_job_id,
                        terminal_status=terminal_status,
                        branch_result=branch_result,
                    ),
                    ledger_path=ledger_path,
                )
                self._update_report_ledger(
                    ledger_path,
                    status="reporting",
                    report_back_job_id=report_back_job_id,
                    error=None,
                )
            report_terminal_status = await self._wait_for_job_terminal(
                report_back_job_id,
                session_id=target_session_id,
                subscriber_kind="session_generation_report_back_completion",
                run_id=payload.run_id,
            )
            self._delete_message_intent(
                payload,
                target_session_id,
                "report_back",
            )
            if (
                report_terminal_status == EventType.JOB_COMPLETED
                and terminal_status == EventType.JOB_COMPLETED
            ):
                self._update_report_ledger(
                    ledger_path,
                    status="completed",
                    report_back_job_id=report_back_job_id,
                    error=None,
                )
                return
            self._update_report_ledger(
                ledger_path,
                status="failed",
                report_back_job_id=report_back_job_id,
                error=(
                    f"分支 Job 失败: {terminal_status}"
                    if terminal_status != EventType.JOB_COMPLETED
                    else f"回写 Job 失败: {report_terminal_status}"
                ),
            )
        except Exception as error:
            self._update_report_ledger(
                ledger_path,
                status="failed",
                report_back_job_id=report_back_job_id,
                error=f"{type(error).__name__}: {error}",
            )
            raise

    async def _dispatch_report_back_message(
        self,
        payload: SessionGenerationExecuteRequest,
        target_session_id: str,
        internal_message: PreparedInternalMessage,
        *,
        ledger_path: Path,
    ) -> str:
        message = self._load_prepared_message(
            payload=payload,
            session_id=target_session_id,
            phase="report_back",
        )
        if message is None:
            message = await self._session_orchestrator.prepare_internal_message(
                target_session_id,
                internal_message,
            )
            self._persist_prepared_message(payload, "report_back", message)
        record = self._read_ledger(ledger_path)
        raw_job_id = record.get("report_back_job_id")
        if raw_job_id is not None and not isinstance(raw_job_id, str):
            raise RuntimeError(f"生成回报账本的 Job ID 类型无效: {ledger_path}")
        job_id = raw_job_id or self._trace_job_id_for_message(
            target_session_id,
            message.message_id,
        )
        if job_id is None:
            job_id = create_prefixed_id("job")
        record["report_back_phase"] = "message_prepared"
        record["report_back_message_id"] = message.message_id
        record["report_back_job_id"] = job_id
        self._write_ledger(ledger_path, record)
        if self._persisted_terminal_status(
            target_session_id,
            job_id,
            self._terminal_event_types(),
        ) is None:
            dispatch = await self._session_orchestrator.dispatch_existing_message(
                target_session_id,
                message,
                job_id=job_id,
            )
            if dispatch.job_id != job_id:
                raise RuntimeError(
                    "会话生成回报没有采用预留 Job ID: "
                    f"reserved={job_id}, actual={dispatch.job_id}"
                )
        record = self._read_ledger(ledger_path)
        record["report_back_phase"] = "job_dispatched"
        record["report_back_job_id"] = job_id
        self._write_ledger(ledger_path, record)
        return job_id

    async def _wait_for_job_terminal(
        self,
        job_id: str,
        *,
        session_id: str,
        subscriber_kind: str,
        run_id: str,
    ) -> str:
        terminal_types = self._terminal_event_types()
        persisted = self._persisted_terminal_status(
            session_id,
            job_id,
            terminal_types,
        )
        if persisted is not None:
            return persisted
        subscription = await self._job_event_bus.subscribe(
            job_id,
            subscriber_kind=subscriber_kind,
            metadata={"run_id": run_id},
            event_types=terminal_types,
        )
        try:
            history = await self._job_event_bus.list_events(job_id, limit=1000)
            terminal_event = next(
                (event for event in reversed(history) if event.type in terminal_types),
                None,
            )
            if terminal_event is not None:
                return terminal_event.type
            persisted = self._persisted_terminal_status(
                session_id,
                job_id,
                terminal_types,
            )
            if persisted is not None:
                return persisted
            return (await subscription.get()).type
        finally:
            await self._job_event_bus.unsubscribe(
                job_id,
                subscription,
                reason="会话生成回写已结束",
            )

    def _persisted_terminal_status(
        self,
        session_id: str,
        job_id: str,
        terminal_types: frozenset[str],
    ) -> str | None:
        events = self._trace_event_store.read_events(session_id)
        terminal_event = next(
            (
                event
                for event in reversed(events)
                if event.job_id == job_id and event.type in terminal_types
            ),
            None,
        )
        return terminal_event.type if terminal_event is not None else None

    async def _known_terminal_status(
        self,
        session_id: str,
        job_id: str,
    ) -> str | None:
        terminal_types = self._terminal_event_types()
        persisted = self._persisted_terminal_status(
            session_id,
            job_id,
            terminal_types,
        )
        if persisted is not None:
            return persisted
        history = await self._job_event_bus.list_events(job_id, limit=1_000)
        terminal_event = next(
            (event for event in reversed(history) if event.type in terminal_types),
            None,
        )
        return terminal_event.type if terminal_event is not None else None

    @staticmethod
    def _terminal_event_types() -> frozenset[str]:
        return frozenset(
            {
                EventType.JOB_COMPLETED,
                EventType.JOB_FAILED,
                EventType.JOB_CANCELLED,
                EventType.SESSION_INTERRUPTED,
            }
        )

    async def _branch_result(self, generated_session_id: str) -> str:
        messages = await self._message_service.list(
            session_id=generated_session_id,
            limit=50,
        )
        assistant_messages = [
            message.content.strip()
            for message in messages.items
            if message.role == MessageRole.assistant and message.content.strip()
        ]
        if not assistant_messages:
            return "（分支会话没有产生可见的 Assistant 回复）"
        return assistant_messages[-1][-16_384:]

    def _update_report_ledger(
        self,
        ledger_path: Path,
        *,
        status: str,
        report_back_job_id: str | None,
        error: str | None,
    ) -> None:
        record = self._read_ledger(ledger_path)
        raw_result = record.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError(f"会话生成回写记录缺少 result: {ledger_path}")
        result = SessionGenerationExecuteResultDTO.model_validate(raw_result)
        record["status"] = status
        record["result"] = result.model_copy(
            update={
                "status": status,
                "report_back_job_id": report_back_job_id,
                "error": error,
            }
        ).model_dump(mode="json")
        record["ended_at"] = (
            datetime.now(timezone.utc).isoformat()
            if status in {"completed", "failed"}
            else None
        )
        self._write_ledger(ledger_path, record)

    def _report_task_finished(
        self,
        ledger_path: Path,
        task: asyncio.Task[None],
    ) -> None:
        if self._report_tasks.get(ledger_path) is task:
            self._report_tasks.pop(ledger_path, None)

    @staticmethod
    def _log_report_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "会话生成分支回写失败",
                exc_info=(type(error), error, error.__traceback__),
            )

    @staticmethod
    def _report_back_prompt(
        payload: SessionGenerationExecuteRequest,
        *,
        generated_session_id: str,
        generated_job_id: str,
        terminal_status: str,
        branch_result: str,
    ) -> str:
        return SessionGenerationReportingSupport._report_back_message(
            payload,
            generated_session_id=generated_session_id,
            generated_job_id=generated_job_id,
            terminal_status=terminal_status,
            branch_result=branch_result,
        ).content

    @staticmethod
    def _report_back_message(
        payload: SessionGenerationExecuteRequest,
        *,
        generated_session_id: str,
        generated_job_id: str,
        terminal_status: str,
        branch_result: str,
    ) -> PreparedInternalMessage:
        context = {
            "report_back": payload.session_strategy.report_back,
            "generated_session_id": generated_session_id,
            "generated_job_id": generated_job_id,
            "terminal_status": terminal_status,
        }
        return internal_message_factory.build(
            kind="generated_session_result",
            control=(
                "这是系统直接注入当前会话的分支执行结果，不是来自其它会话的通信请求。"
                "请只在当前会话中基于结果继续处理并回复当前用户；禁止调用 "
                "send_message_to_session、reply_to_session 或其它跨会话通信工具，"
                "也不要重新访问、唤醒或启动生成会话。"
                "分支最终回复已由系统直接读取并注入如下，不要猜测文件路径，"
                "也不需要再次读取其它会话。"
            ),
            sections=(
                PromptSection("control_context", context),
                PromptSection("generated_session_result", branch_result),
            ),
            metadata={
                "boxteam_generation_run_id": payload.run_id,
                "boxteam_generator_id": payload.generator_id,
                "boxteam_generation_phase": "report_back",
            },
            display_content="生成分支已结束，主会话正在处理返回结果。",
        )

    def _result(
        self,
        payload: SessionGenerationExecuteRequest,
        session: SessionDTO,
        message_id: str,
        job_id: str,
        *,
        workspace_id: str,
    ) -> SessionGenerationExecuteResultDTO:
        return SessionGenerationExecuteResultDTO(
            run_id=payload.run_id,
            status="queued",
            outputs=[
                SessionGenerationOutputDTO(
                    workspace_id=workspace_id,
                    session_id=session.session_id,
                    title=session.title,
                    navigation_path=payload.navigation_path,
                    storage_relative_path=(
                        self._session_catalog_service.path_resolver.relative_path(
                            session.session_id
                        )
                    ),
                )
            ],
            message_id=message_id,
            job_id=job_id,
        )
