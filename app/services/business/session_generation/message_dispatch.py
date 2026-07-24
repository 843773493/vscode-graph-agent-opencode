from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.message import MessageDTO
from app.schemas.public_v2.session_navigation import SessionGenerationExecuteRequest
from app.services.business.session_generation.reporting import (
    SessionGenerationReportingSupport,
)


class SessionGenerationMessageDispatchSupport(SessionGenerationReportingSupport):
    """封装生成消息的持久化意图、预留 Job 与幂等派发。"""

    async def _dispatch_generation_message(
        self,
        payload: SessionGenerationExecuteRequest,
        session_id: str,
        prompt: str,
        *,
        phase: str,
        ledger_path: Path,
    ) -> tuple[str, str]:
        self._update_execution_phase(
            ledger_path,
            phase="session_ready",
            session_id=session_id,
        )
        message = self._load_prepared_message(
            payload=payload,
            session_id=session_id,
            phase=phase,
        )
        if message is None:
            message = await self._session_orchestrator.prepare_user_message(
                session_id,
                prompt,
                metadata=self._message_metadata(payload, phase),
            )
            self._persist_prepared_message(payload, phase, message)
        self._update_execution_phase(
            ledger_path,
            phase="message_prepared",
            session_id=session_id,
            message=message,
        )
        record = self._read_ledger(ledger_path)
        raw_job_id = record.get("dispatched_job_id")
        if raw_job_id is not None and not isinstance(raw_job_id, str):
            raise RuntimeError(f"生成账本的 Job ID 类型无效: {ledger_path}")
        job_id = raw_job_id or self._trace_job_id_for_message(
            session_id,
            message.message_id,
        )
        if job_id is None:
            job_id = create_prefixed_id("job")
        self._update_execution_phase(
            ledger_path,
            phase="job_reserved",
            session_id=session_id,
            message=message,
            job_id=job_id,
        )
        terminal_types = self._terminal_event_types()
        if self._persisted_terminal_status(session_id, job_id, terminal_types) is None:
            dispatch = await self._session_orchestrator.dispatch_existing_message(
                session_id,
                message,
                job_id=job_id,
            )
            if dispatch.job_id != job_id:
                raise RuntimeError(
                    "会话生成派发没有采用预留 Job ID: "
                    f"reserved={job_id}, actual={dispatch.job_id}"
                )
        self._update_execution_phase(
            ledger_path,
            phase="child_dispatched",
            session_id=session_id,
            message=message,
            job_id=job_id,
        )
        return message.message_id, job_id

    def _load_prepared_message(
        self,
        *,
        payload: SessionGenerationExecuteRequest,
        session_id: str,
        phase: str,
    ) -> MessageDTO | None:
        intent_path = self._message_intent_path(payload, session_id, phase)
        if not intent_path.is_file():
            return None
        raw_message = self._read_ledger(intent_path).get("message")
        if not isinstance(raw_message, dict):
            raise RuntimeError(f"会话生成消息意图格式无效: {intent_path}")
        message = MessageDTO.model_validate(raw_message)
        expected_metadata = self._message_metadata(payload, phase)
        if (
            message.session_id != session_id
            or message.role != MessageRole.user
            or any(
                message.metadata.get(key) != value
                for key, value in expected_metadata.items()
            )
        ):
            raise RuntimeError(
                "会话生成消息意图身份不匹配: "
                f"path={intent_path}, session_id={session_id}, phase={phase}, "
                f"message_id={message.message_id}"
            )
        return message

    def _persist_prepared_message(
        self,
        payload: SessionGenerationExecuteRequest,
        phase: str,
        message: MessageDTO,
    ) -> None:
        intent_path = self._message_intent_path(
            payload,
            message.session_id,
            phase,
        )
        if intent_path.exists():
            existing = self._load_prepared_message(
                payload=payload,
                session_id=message.session_id,
                phase=phase,
            )
            if existing != message:
                raise RuntimeError(
                    f"会话生成消息意图已存在且内容冲突: {intent_path}"
                )
            return
        self._write_ledger(
            intent_path,
            {
                "schema_version": 1,
                "run_id": payload.run_id,
                "generator_id": payload.generator_id,
                "phase": phase,
                "message": message.model_dump(mode="json"),
            },
        )

    def _message_intent_path(
        self,
        payload: SessionGenerationExecuteRequest,
        session_id: str,
        phase: str,
    ) -> Path:
        session_path = (
            self._session_catalog_service.path_resolver.resolve_session_dir(session_id)
        )
        run_digest = hashlib.sha256(
            f"{payload.generator_id}\n{payload.run_id}".encode("utf-8")
        ).hexdigest()
        return session_path / "generation_intents" / run_digest / f"{phase}.json"

    def _delete_message_intent(
        self,
        payload: SessionGenerationExecuteRequest,
        session_id: str,
        phase: str,
    ) -> None:
        try:
            intent_path = self._message_intent_path(payload, session_id, phase)
        except KeyError:
            # 会话物理节点已删除时，意图文件已随完整会话包一并清理。
            return
        intent_path.unlink(missing_ok=True)
        parent = intent_path.parent
        self._remove_empty_intent_directory(parent)
        root = parent.parent
        self._remove_empty_intent_directory(root)

    @staticmethod
    def _remove_empty_intent_directory(path: Path) -> None:
        try:
            if not path.is_dir() or any(path.iterdir()):
                return
            path.rmdir()
        except FileNotFoundError:
            # 会话包或同一运行的意图目录并发删除后，清理目标已经不存在。
            return

    def _trace_job_id_for_message(
        self,
        session_id: str,
        message_id: str,
    ) -> str | None:
        job_ids = {
            event.job_id
            for event in self._trace_event_store.read_events(
                session_id,
            )
            if event.type == EventType.MESSAGE_CREATED
            and getattr(event.payload, "message_id", None) == message_id
        }
        if len(job_ids) > 1:
            raise RuntimeError(
                "同一生成消息关联了多个 Job，拒绝选择恢复目标: "
                f"session_id={session_id}, message_id={message_id}, "
                f"job_ids={','.join(sorted(job_ids))}"
            )
        return next(iter(job_ids), None)

    def _update_execution_phase(
        self,
        ledger_path: Path,
        *,
        phase: str,
        session_id: str,
        message: MessageDTO | None = None,
        job_id: str | None = None,
    ) -> None:
        record = self._read_ledger(ledger_path)
        record.update(
            {
                "status": "executing",
                "phase": phase,
                "generated_session_id": session_id,
            }
        )
        if message is not None:
            record["prepared_message_id"] = message.message_id
        if job_id is not None:
            record["dispatched_job_id"] = job_id
        self._write_ledger(ledger_path, record)
