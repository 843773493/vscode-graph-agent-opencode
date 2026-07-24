from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.runtime.session_orchestrator import SessionOrchestrator
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.schemas.public_v2.session import (
    SessionDTO,
    SessionGenerationOriginDTO,
)
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.message import MessageDTO
from app.schemas.public_v2.session_navigation import (
    SessionGenerationExecuteRequest,
    SessionGenerationExecuteResultDTO,
    SessionGenerationOutputDTO,
    SessionGenerationCapabilitiesDTO,
    SessionGenerationCapabilityDTO,
)
from app.schemas.public_v2.session_navigation.models import (
    SessionGenerationTargetDTO,
)
from app.services.business.session_context_fork_service import (
    SessionContextForkService,
)
from app.services.business.session_generation.providers import (
    SessionGenerationProviderProtocol,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_service import SessionService
from app.services.business.message_service import MessageService
from app.core.session_paths import validate_generator_physical_segment
from app.services.infrastructure.trace_event_store import TraceEventStore


logger = logging.getLogger(__name__)


class SessionGenerationService:
    def __init__(
        self,
        *,
        workspace_root: Path,
        session_service: SessionService,
        session_catalog_service: SessionCatalogService,
        session_context_fork_service: SessionContextForkService,
        session_orchestrator: SessionOrchestrator,
        job_event_bus: JobEventBusProtocol,
        message_service: MessageService,
        trace_event_store: TraceEventStore,
        providers: list[SessionGenerationProviderProtocol],
    ) -> None:
        self._workspace_root = workspace_root
        self._session_service = session_service
        self._session_catalog_service = session_catalog_service
        self._session_context_fork_service = session_context_fork_service
        self._session_orchestrator = session_orchestrator
        self._job_event_bus = job_event_bus
        self._message_service = message_service
        self._trace_event_store = trace_event_store
        self._providers = {provider.type_id: provider for provider in providers}
        if len(self._providers) != len(providers):
            raise ValueError("会话生成器 provider type_id 重复")
        for provider in providers:
            Draft202012Validator.check_schema(provider.config_schema)
        self._runs_dir = workspace_root / ".boxteam" / "generation-runs"
        self._report_tasks: dict[Path, asyncio.Task[None]] = {}
        self._idempotency_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._idempotency_lock_users: dict[tuple[str, str], int] = {}
        self._idempotency_lock_guard = asyncio.Lock()

    async def start(self) -> None:
        """恢复上次进程中尚未完成的分支回报意图。"""
        if self._report_tasks:
            raise RuntimeError("会话生成服务已经启动")
        if not self._runs_dir.is_dir():
            return
        for ledger_path in sorted(self._runs_dir.glob("*/*.json")):
            record = self._read_ledger(ledger_path)
            status = record.get("status")
            if status not in {"executing", "running", "reporting"}:
                continue
            raw_request = record.get("request")
            if not isinstance(raw_request, dict):
                raise RuntimeError(
                    f"待恢复的会话生成记录缺少 request: {ledger_path}"
                )
            payload = SessionGenerationExecuteRequest.model_validate(raw_request)
            if status in {"executing", "running"}:
                await self._resume_executing(payload, ledger_path)
                continue
            raw_result = record.get("result")
            if not isinstance(raw_result, dict):
                raise RuntimeError(
                    f"待恢复的会话生成回报记录缺少 result: {ledger_path}"
                )
            result = SessionGenerationExecuteResultDTO.model_validate(raw_result)
            target = self._require_target(payload)
            if not result.outputs or result.job_id is None:
                raise RuntimeError(
                    f"待恢复的会话生成回报记录缺少输出或 Job: {ledger_path}"
                )
            if await self._known_terminal_status(
                result.outputs[0].session_id,
                result.job_id,
            ) is None:
                recovered_result = await self._execute_strategy(
                    payload,
                    self._resolve_provider(payload).build_prompt(payload.config),
                    ledger_path=ledger_path,
                )
                self._assert_same_dispatch_result(result, recovered_result)
                record = self._read_ledger(ledger_path)
                record["status"] = "reporting"
                record["result"] = result.model_dump(mode="json")
                self._write_ledger(ledger_path, record)
            stored_report_job_id = record.get("report_back_job_id")
            if stored_report_job_id is not None and not isinstance(
                stored_report_job_id,
                str,
            ):
                raise RuntimeError(
                    f"待恢复的回报 Job ID 类型无效: {ledger_path}"
                )
            self._schedule_report_back(
                payload,
                ledger_path=ledger_path,
                target_session_id=target.session_id,
                generated_session_id=result.outputs[0].session_id,
                generated_job_id=result.job_id,
                existing_report_back_job_id=(
                    result.report_back_job_id or stored_report_job_id
                ),
            )

    async def shutdown(self) -> None:
        tasks = tuple(self._report_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._report_tasks.clear()

    def list_capabilities(self) -> SessionGenerationCapabilitiesDTO:
        return SessionGenerationCapabilitiesDTO(
            items=[
                SessionGenerationCapabilityDTO(
                    type_id=provider.type_id,
                    supported_versions=sorted(provider.supported_versions),
                    config_schema=provider.config_schema,
                )
                for provider in sorted(
                    self._providers.values(),
                    key=lambda item: item.type_id,
                )
            ]
        )

    async def execute(
        self,
        payload: SessionGenerationExecuteRequest,
    ) -> SessionGenerationExecuteResultDTO:
        lock_key = (payload.generator_id, payload.idempotency_key)
        async with self._idempotency_lock_guard:
            lock = self._idempotency_locks.setdefault(
                lock_key,
                asyncio.Lock(),
            )
            self._idempotency_lock_users[lock_key] = (
                self._idempotency_lock_users.get(lock_key, 0) + 1
            )
        try:
            async with lock:
                return await self._execute_locked(payload)
        finally:
            async with self._idempotency_lock_guard:
                remaining_users = self._idempotency_lock_users[lock_key] - 1
                if remaining_users == 0:
                    self._idempotency_lock_users.pop(lock_key)
                    self._idempotency_locks.pop(lock_key)
                else:
                    self._idempotency_lock_users[lock_key] = remaining_users

    async def _execute_locked(
        self,
        payload: SessionGenerationExecuteRequest,
    ) -> SessionGenerationExecuteResultDTO:
        self._validate_workspace_locators(payload)
        ledger_path = self._ledger_path(
            payload.generator_id,
            payload.idempotency_key,
        )
        if ledger_path.exists():
            record = self._read_ledger(ledger_path)
            if record.get("generator_id") != payload.generator_id:
                raise RuntimeError(
                    "会话生成幂等记录 generator_id 不匹配: "
                    f"expected={payload.generator_id}, "
                    f"actual={record.get('generator_id')}"
                )
            if record.get("status") not in {"completed", "running", "reporting"}:
                raise RuntimeError(
                    "同一幂等键存在未完成的生成记录，拒绝重复创建: "
                    f"idempotency_key={payload.idempotency_key}, "
                    f"status={record.get('status')}"
                )
            raw_result = record.get("result")
            if not isinstance(raw_result, dict):
                raise RuntimeError(f"已完成的生成记录缺少 result: {ledger_path}")
            return SessionGenerationExecuteResultDTO.model_validate(raw_result)

        provider = self._resolve_provider(payload)
        prompt = provider.build_prompt(payload.config)
        self._write_ledger(
            ledger_path,
            {
                "schema_version": 1,
                "status": "executing",
                "run_id": payload.run_id,
                "generator_id": payload.generator_id,
                "idempotency_key": payload.idempotency_key,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "request": payload.model_dump(mode="json"),
            },
        )
        try:
            result = await self._execute_strategy(
                payload,
                prompt,
                ledger_path=ledger_path,
            )
        except Exception as error:
            self._write_ledger(
                ledger_path,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "run_id": payload.run_id,
                    "generator_id": payload.generator_id,
                    "idempotency_key": payload.idempotency_key,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(error).__name__}: {error}",
                    "request": payload.model_dump(mode="json"),
                },
            )
            raise
        return self._persist_strategy_result(payload, ledger_path, result)

    def _persist_strategy_result(
        self,
        payload: SessionGenerationExecuteRequest,
        ledger_path: Path,
        result: SessionGenerationExecuteResultDTO,
    ) -> SessionGenerationExecuteResultDTO:
        requires_report_back = (
            payload.session_strategy.mode == "fork_new_and_report_back"
            and payload.session_strategy.report_back != "none"
        )
        stored_result = (
            result.model_copy(update={"status": "reporting"})
            if requires_report_back
            else result
        )
        record = self._read_ledger(ledger_path)
        record.update(
            {
                "schema_version": 1,
                "status": "reporting" if requires_report_back else "running",
                "run_id": payload.run_id,
                "generator_id": payload.generator_id,
                "idempotency_key": payload.idempotency_key,
                "ended_at": None,
                "request": payload.model_dump(mode="json"),
                "result": stored_result.model_dump(mode="json"),
            }
        )
        self._write_ledger(ledger_path, record)
        if requires_report_back:
            target = self._require_target(payload)
            if not stored_result.outputs or stored_result.job_id is None:
                raise RuntimeError("分支回报缺少生成会话或分支 Job")
            self._schedule_report_back(
                payload,
                ledger_path=ledger_path,
                target_session_id=target.session_id,
                generated_session_id=stored_result.outputs[0].session_id,
                generated_job_id=stored_result.job_id,
                existing_report_back_job_id=None,
            )
        else:
            if not stored_result.outputs or stored_result.job_id is None:
                raise RuntimeError("会话生成运行缺少生成会话或 Job")
            self._schedule_child_completion(
                payload,
                ledger_path=ledger_path,
                session_id=stored_result.outputs[0].session_id,
                job_id=stored_result.job_id,
            )
        return stored_result

    @staticmethod
    def _assert_same_dispatch_result(
        expected: SessionGenerationExecuteResultDTO,
        actual: SessionGenerationExecuteResultDTO,
    ) -> None:
        expected_session_ids = [item.session_id for item in expected.outputs]
        actual_session_ids = [item.session_id for item in actual.outputs]
        if (
            expected_session_ids != actual_session_ids
            or expected.message_id != actual.message_id
            or expected.job_id != actual.job_id
        ):
            raise RuntimeError(
                "会话生成恢复改变了稳定派发身份: "
                f"expected_sessions={expected_session_ids}, "
                f"actual_sessions={actual_session_ids}, "
                f"expected_message={expected.message_id}, "
                f"actual_message={actual.message_id}, "
                f"expected_job={expected.job_id}, actual_job={actual.job_id}"
            )

    async def _resume_executing(
        self,
        payload: SessionGenerationExecuteRequest,
        ledger_path: Path,
    ) -> None:
        self._validate_workspace_locators(payload)
        provider = self._resolve_provider(payload)
        try:
            result = await self._execute_strategy(
                payload,
                provider.build_prompt(payload.config),
                ledger_path=ledger_path,
            )
        except Exception as error:
            record = self._read_ledger(ledger_path)
            record.update(
                {
                    "status": "failed",
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"恢复执行失败: {type(error).__name__}: {error}",
                }
            )
            self._write_ledger(ledger_path, record)
            raise
        self._persist_strategy_result(payload, ledger_path, result)

    def get_run_status(
        self,
        *,
        generator_id: str,
        idempotency_key: str,
    ) -> SessionGenerationExecuteResultDTO:
        ledger_path = self._ledger_path(generator_id, idempotency_key)
        if not ledger_path.is_file():
            raise KeyError(
                "会话生成运行记录不存在: "
                f"generator_id={generator_id}, idempotency_key={idempotency_key}"
            )
        record = self._read_ledger(ledger_path)
        raw_result = record.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError(f"会话生成运行记录缺少 result: {ledger_path}")
        return SessionGenerationExecuteResultDTO.model_validate(raw_result)

    @staticmethod
    def _validate_workspace_locators(
        payload: SessionGenerationExecuteRequest,
    ) -> None:
        validate_generator_physical_segment(payload.title)
        for segment in payload.navigation_path:
            validate_generator_physical_segment(segment)
        execution_workspace_id = payload.execution_workspace_id
        if payload.placement.workspace_id != execution_workspace_id:
            raise ValueError(
                "生成会话挂载工作区与执行工作区不一致: "
                f"placement={payload.placement.workspace_id}, "
                f"execution={execution_workspace_id}"
            )
        target = payload.session_strategy.target
        if target is not None and target.workspace_id != execution_workspace_id:
            raise ValueError(
                "生成策略目标工作区与执行工作区不一致: "
                f"target={target.workspace_id}, execution={execution_workspace_id}"
            )
        context_source = payload.context_source
        if (
            context_source.kind == "live_session"
            and context_source.workspace_id != execution_workspace_id
        ):
            raise ValueError(
                "生成上下文来源工作区与执行工作区不一致: "
                f"context={context_source.workspace_id}, "
                f"execution={execution_workspace_id}"
            )

    def _resolve_provider(
        self,
        payload: SessionGenerationExecuteRequest,
    ) -> SessionGenerationProviderProtocol:
        provider = self._providers.get(payload.generator_type.type_id)
        if provider is None:
            raise ValueError(
                "工作区未注册会话生成器类型: "
                f"{payload.generator_type.type_id}"
            )
        if payload.generator_type.version not in provider.supported_versions:
            raise ValueError(
                "工作区不支持会话生成器版本: "
                f"type_id={payload.generator_type.type_id}, "
                f"version={payload.generator_type.version}"
            )
        try:
            Draft202012Validator(provider.config_schema).validate(payload.config)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            raise ValueError(
                "会话生成器配置不符合 provider schema: "
                f"type_id={provider.type_id}, path={location}, error={error.message}"
            ) from error
        return provider

    async def _execute_strategy(
        self,
        payload: SessionGenerationExecuteRequest,
        prompt: str,
        *,
        ledger_path: Path,
    ) -> SessionGenerationExecuteResultDTO:
        mode = payload.session_strategy.mode
        origin = SessionGenerationOriginDTO(
            generator_id=payload.generator_id,
            run_id=payload.run_id,
            idempotency_key=payload.idempotency_key,
            generator_type_id=payload.generator_type.type_id,
            generator_type_version=payload.generator_type.version,
        )
        if mode == "continue_existing":
            target = self._require_target(payload)
            session = await self._session_service.get(target.session_id)
            message_id, job_id = await self._dispatch_generation_message(
                payload,
                session.session_id,
                prompt,
                phase="continue_existing",
                ledger_path=ledger_path,
            )
            return self._result(
                payload,
                session,
                message_id,
                job_id,
                workspace_id=payload.execution_workspace_id,
            )

        if mode == "fork_new_and_report_back":
            target = self._require_target(payload)
            session = await self._find_generated_session(payload, ledger_path)
            if session is None:
                parent_node_id = await self._resolve_placement_folder(payload)
                session = await self._session_context_fork_service.fork(
                    target.session_id,
                    generation_origin=origin,
                    title=payload.title,
                    parent_node_id=parent_node_id,
                    place_under_source=False,
                )
            message_id, job_id = await self._dispatch_generation_message(
                payload,
                session.session_id,
                prompt,
                phase="fork_child",
                ledger_path=ledger_path,
            )
            return self._result(
                payload,
                session,
                message_id,
                job_id,
                workspace_id=payload.execution_workspace_id,
            )

        if mode != "new_per_run":
            raise ValueError(f"不支持的会话生成策略: {mode}")
        if payload.context_source.kind == "snapshot":
            raise ValueError("当前工作区尚未注册 snapshot 上下文导入 provider")
        session = await self._find_generated_session(payload, ledger_path)
        if session is None:
            parent_node_id = await self._resolve_placement_folder(payload)
            if payload.context_source.kind == "live_session":
                source_session_id = payload.context_source.session_id
                if source_session_id is None:
                    raise ValueError("live_session context source 缺少 session_id")
                session = await self._session_context_fork_service.fork(
                    source_session_id,
                    generation_origin=origin,
                    title=payload.title,
                    parent_node_id=parent_node_id,
                    place_under_source=False,
                )
            else:
                session = await self._session_service.create_generated(
                    title=payload.title,
                    agent_id=None,
                    parent_session_id=None,
                    generation_origin=origin,
                    parent_node_id=parent_node_id,
                )
        message_id, job_id = await self._dispatch_generation_message(
            payload,
            session.session_id,
            prompt,
            phase="new_session",
            ledger_path=ledger_path,
        )
        return self._result(
            payload,
            session,
            message_id,
            job_id,
            workspace_id=payload.execution_workspace_id,
        )

    async def _resolve_placement_folder(
        self,
        payload: SessionGenerationExecuteRequest,
    ) -> str | None:
        if payload.placement.workspace_id != payload.execution_workspace_id:
            raise ValueError(
                "生成会话挂载工作区与执行工作区不一致: "
                f"placement={payload.placement.workspace_id}, "
                f"execution={payload.execution_workspace_id}"
            )
        if payload.placement.kind == "session":
            anchor_session_id = payload.placement.session_id
            if anchor_session_id is None:
                raise ValueError("session placement 缺少 session_id")
            anchor = self._session_catalog_service.path_resolver.get_node(
                anchor_session_id
            )
            if anchor.kind != "session":
                raise ValueError(f"生成器挂载目标不是会话: {anchor_session_id}")
            parent_node_id = anchor.node_id
        elif payload.placement.kind == "session_folder":
            parent_node_id = payload.placement.folder_id
            if parent_node_id is None:
                raise ValueError("session_folder placement 缺少 folder_id")
        else:
            parent_node_id = None
        return await self._session_catalog_service.ensure_folder_path(
            payload.navigation_path,
            parent_folder_id=parent_node_id,
        )

    @staticmethod
    def _require_target(
        payload: SessionGenerationExecuteRequest,
    ) -> SessionGenerationTargetDTO:
        target = payload.session_strategy.target
        if target is None:
            raise ValueError(f"{payload.session_strategy.mode} 缺少 target")
        return target

    @staticmethod
    def _message_metadata(
        payload: SessionGenerationExecuteRequest,
        phase: str,
    ) -> dict[str, object]:
        return {
            "boxteam_generation_run_id": payload.run_id,
            "boxteam_generator_id": payload.generator_id,
            "boxteam_generation_phase": phase,
        }

    async def _find_generated_session(
        self,
        payload: SessionGenerationExecuteRequest,
        ledger_path: Path,
    ) -> SessionDTO | None:
        record = self._read_ledger(ledger_path)
        generated_session_id = record.get("generated_session_id")
        if generated_session_id is not None:
            if not isinstance(generated_session_id, str):
                raise RuntimeError(
                    f"生成账本的 generated_session_id 类型无效: {ledger_path}"
                )
            session = await self._session_service.get(generated_session_id)
            self._validate_generation_origin(payload, session)
            return session
        sessions = await self._session_service.list(limit=100_000)
        matches = [
            session
            for session in sessions.items
            if session.generation_origin is not None
            and session.generation_origin.run_id == payload.run_id
            and session.generation_origin.generator_id == payload.generator_id
            and session.generation_origin.idempotency_key == payload.idempotency_key
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "同一生成运行存在多个物理会话，拒绝猜测恢复目标: "
                f"run_id={payload.run_id}, "
                f"sessions={','.join(item.session_id for item in matches)}"
            )
        return matches[0] if matches else None

    @staticmethod
    def _validate_generation_origin(
        payload: SessionGenerationExecuteRequest,
        session: SessionDTO,
    ) -> None:
        origin = session.generation_origin
        if (
            origin is None
            or origin.run_id != payload.run_id
            or origin.generator_id != payload.generator_id
            or origin.idempotency_key != payload.idempotency_key
        ):
            raise RuntimeError(
                "生成账本指向的会话来源不匹配: "
                f"session_id={session.session_id}, run_id={payload.run_id}, "
                f"generator_id={payload.generator_id}"
            )

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
                    self._report_back_prompt(
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
        prompt: str,
        *,
        ledger_path: Path,
    ) -> str:
        message = self._load_prepared_message(
            payload=payload,
            session_id=target_session_id,
            phase="report_back",
        )
        if message is None:
            message = await self._session_orchestrator.prepare_user_message(
                target_session_id,
                prompt,
                metadata=self._message_metadata(payload, "report_back"),
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
        return (
            "这是系统直接注入当前会话的分支执行结果，不是来自其它会话的通信请求。"
            "请只在当前会话中基于结果继续处理并回复当前用户；禁止调用 "
            "send_message_to_session、reply_to_session 或其它跨会话通信工具，"
            "也不要重新访问、唤醒或启动生成会话。"
            f"回报模式: {payload.session_strategy.report_back}。"
            f"生成会话 ID: {generated_session_id}；生成 Job ID: {generated_job_id}。"
            f"分支终态: {terminal_status}。分支最终回复已由系统直接读取并注入如下，"
            "不要猜测文件路径，也不需要再次读取其它会话：\n\n"
            f"<generated_session_result>\n{branch_result}\n</generated_session_result>"
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

    def _ledger_path(self, generator_id: str, idempotency_key: str) -> Path:
        generator_digest = hashlib.sha256(generator_id.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(
            f"{generator_id}\n{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return self._runs_dir / generator_digest / f"{digest}.json"

    @staticmethod
    def _read_ledger(path: Path) -> dict[str, object]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"会话生成幂等记录必须是对象: {path}")
        return value

    @staticmethod
    def _write_ledger(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
