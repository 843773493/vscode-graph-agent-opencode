"""显式导入旧生成器；只在迁移请求中读取旧账本，正常运行不双读。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.exceptions import NotFoundError
from app.core.resource_uri import parse_session_thread_uri
from app.core.trace_middleware import get_current_gateway_id
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.generator_import import (
    LegacyGenerationRun,
    SessionGeneratorImportRequest,
)
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.persistent_task import (
    SessionGenerationActionDTO,
    TaskRunPhase,
)
from app.schemas.internal_v2.session_navigation.generation_state import (
    GenerationActionState,
    GenerationHistoricalState,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationExecuteRequest,
    SessionGenerationExecuteResultDTO,
    SessionGenerationOutputDTO,
    SessionGenerationValidateRequest,
)
from app.services.infrastructure.persistent_tasks.generator_import import (
    ImportedGenerationRun,
    imported_files,
    install_generator,
)

if TYPE_CHECKING:
    from app.services.orchestration.persistent_tasks.runtime import (
        PersistentTaskRuntime,
    )


def _read(path: Path, root: Path, files: dict[str, str]) -> dict:
    if path.is_symlink():
        raise ValueError("生成器迁移拒绝符号链接输入")
    raw = path.read_bytes()
    files[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("旧生成器输入必须是对象")  # noqa: TRY004 - 非法迁移输入按公开合同返回 4xx。
    return result


def _retire_files(root: Path, task_id: str, files: dict[str, str]) -> None:
    backup = root / ".boxteam/migrations/session-generators" / hashlib.sha256(task_id.encode()).hexdigest()
    for relative, digest in files.items():
        source = root / relative
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("生成器迁移文件超出目标工作区")
        target = backup / relative
        existing = source if source.exists() else target
        if existing.is_symlink() or not existing.is_file() or hashlib.sha256(existing.read_bytes()).hexdigest() != digest:
            raise RuntimeError("生成器迁移输入已变化或备份缺失，停止退休旧文件")
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeError("生成器迁移备份发生冲突")
            source.replace(target)


def _normalized_request(raw: dict, request: SessionGeneratorImportRequest, workspace_id: str) -> SessionGenerationExecuteRequest:
    payload = SessionGenerationExecuteRequest.model_validate(raw)

    def canonical(value: str) -> str:
        if value == workspace_id:
            return value
        if value not in request.workspace_ids:
            raise ValueError(f"旧生成器运行工作区没有明确映射: {value}")
        return request.workspace_ids[value]

    values = payload.model_dump(mode="json")
    values["execution_workspace_id"] = canonical(payload.execution_workspace_id)
    values["placement"]["workspace_id"] = canonical(payload.placement.workspace_id)
    if payload.context_source.workspace_id is not None:
        values["context_source"]["workspace_id"] = canonical(payload.context_source.workspace_id)
    if payload.session_strategy.target is not None:
        values["session_strategy"]["target"]["workspace_id"] = canonical(payload.session_strategy.target.workspace_id)
    return SessionGenerationExecuteRequest.model_validate(values)


async def _run(runtime: PersistentTaskRuntime, request: SessionGeneratorImportRequest,
               legacy: LegacyGenerationRun, files: dict[str, str]) -> ImportedGenerationRun:
    store = runtime.store
    root = runtime._workspace_root
    if legacy.generator_id != request.task_id:
        raise ValueError("旧生成器运行归属不匹配")
    digest = hashlib.sha256(f"{request.task_id}\n{legacy.idempotency_key}".encode()).hexdigest()
    directory = hashlib.sha256(request.task_id.encode()).hexdigest()
    ledger_path = root / ".boxteam/generation-runs" / directory / f"{digest}.json"
    record = _read(ledger_path, root, files) if ledger_path.exists() else None
    state = None
    configuration = request.configuration
    phase: TaskRunPhase = "pending"
    terminal = legacy.status in {"completed", "partial", "failed", "cancelled", "skipped"}
    result = SessionGenerationExecuteResultDTO.model_validate(record["result"]) if record and record.get("result") else None
    # 工作区已经完成、Gateway 尚未收到回执的窗口，以目标执行终态为准。
    status = result.status if not terminal and result and result.status in {"completed", "failed", "skipped"} else legacy.status
    terminal = terminal or status in {"completed", "failed", "skipped"}
    if record is not None:
        payload = _normalized_request(record["request"], request, store.workspace_id)
        if (payload.generator_id, payload.run_id, payload.idempotency_key) != (request.task_id, legacy.run_id, legacy.idempotency_key):
            raise ValueError("旧生成器工作区账本身份不匹配")
        runtime.generation._validate_definition_workspaces(payload)
        if payload.execution_workspace_id != store.workspace_id:
            raise ValueError("旧运行不属于当前工作区")
        action = configuration.action
        assert isinstance(action, SessionGenerationActionDTO)
        historical = action.generation.model_copy(update={
            "generator_type": payload.generator_type, "config": payload.config, "placement": payload.placement,
            "context_source": payload.context_source, "session_strategy": payload.session_strategy,
        })
        configuration = configuration.model_copy(update={"name": payload.name, "action": action.model_copy(update={"generation": historical})})
        if not terminal:
            result = SessionGenerationExecuteResultDTO.model_validate(record["result"]) if record.get("result") else None
            session_id = record.get("generated_session_id") or (result.outputs[0].session_id if result and result.outputs else None)
            if session_id is None and not terminal:
                with runtime._sessions.path_resolver.catalog_store.read_transaction() as connection:
                    matches = connection.execute(
                        "SELECT node_id FROM nodes WHERE kind='session' AND state='active' "
                        "AND json_extract(generation_origin_json,'$.generator_id')=? "
                        "AND json_extract(generation_origin_json,'$.run_id')=? "
                        "AND json_extract(generation_origin_json,'$.idempotency_key')=? LIMIT 2",
                        (payload.generator_id, payload.run_id, payload.idempotency_key),
                    ).fetchall()
                if len(matches) > 1:
                    raise RuntimeError("旧生成器身份对应多个会话，不能猜测迁移目标")
                session_id = matches[0][0] if matches else None
            identity = UserExecutionIdentity(**record["dispatched_execution_identity"]) if record.get("dispatched_execution_identity") else None
            message = None
            thread_id = None
            if session_id is not None and not terminal:
                async with runtime._sessions.session_read_admission(session_id):
                    target = payload.session_strategy.target
                    thread_id = target.thread_id if payload.session_strategy.mode == "continue_existing" and target else await runtime._sessions.resolve_main_thread(session_id)
                    session_root = runtime._sessions.path_resolver.resolve_session_node(session_id)
                    run_digest = hashlib.sha256(f"{request.task_id}\n{legacy.run_id}".encode()).hexdigest()
                    message_phase = "continue_existing" if payload.session_strategy.mode == "continue_existing" else "fork_child" if payload.session_strategy.mode == "fork_new_and_report_back" else "new_session"
                    intent = session_root / "generation_intents" / run_digest / f"{message_phase}.json"
                    if intent.exists():
                        intent_value = _read(intent, root, files)
                        if (intent_value["generator_id"], intent_value["run_id"], intent_value["phase"]) != (request.task_id, legacy.run_id, message_phase):
                            raise ValueError("旧生成器消息意图身份不匹配")
                        message = MessageDTO.model_validate(intent_value["message"])
                        if message.session_id != session_id or message.thread_id != thread_id:
                            raise ValueError("旧生成器消息意图的Session/Thread归属不匹配")
                    job_id = identity.job_id if identity else legacy.job_id
                    if job_id is not None:
                        try:
                            admission = runtime._saver.execution_admission_for_job(session_id, thread_id=thread_id, job_id=job_id)
                        except KeyError:
                            if message is None:
                                raise RuntimeError("旧生成器准入未接受且准备消息缺失") from None
                        else:
                            if admission.turn_id is None:
                                raise RuntimeError("旧生成器执行不是用户消息准入")
                            found = UserExecutionIdentity(job_id=admission.job_id, turn_id=admission.turn_id,
                                                          admission_idempotency_key=admission.admission_idempotency_key)
                            if identity is not None and identity != found:
                                raise RuntimeError("旧生成器执行引用与canonical身份冲突")
                            identity, message = found, None
            if legacy.job_id is not None and identity is None:
                raise RuntimeError("旧生成器缺少完整执行身份，不能猜测或重新派发")
            state = GenerationActionState(
                title=payload.title, navigation_path=payload.navigation_path,
                session_id=session_id, thread_id=thread_id, message_id=record.get("prepared_message_id") or (result.message_id if result else None) or legacy.message_id,
                execution_identity=identity, prepared_message=message,
                report_back_job_id=record.get("report_back_job_id") or legacy.report_back_job_id,
                status="reporting" if legacy.status == "reporting" else "queued",
            )
            phase = "delivering"
    elif not terminal and legacy.status in {"running", "reporting"}:
        raise RuntimeError("旧生成器已派发但目标工作区账本缺失，停止迁移")
    if terminal:
        if status not in ("completed", "partial", "failed", "cancelled", "skipped"):
            raise RuntimeError("旧生成器终态包含非终结运行状态")
        phase = "delivered" if status == "completed" else "skipped" if status == "skipped" else "failed"
        raw_outputs = result.outputs if result is not None else legacy.outputs
        outputs = []
        for output in raw_outputs:
            owner = request.workspace_ids.get(output.workspace_id, output.workspace_id)
            if owner != store.workspace_id:
                raise ValueError("旧生成器输出不属于目标工作区")
            outputs.append(SessionGenerationOutputDTO.model_validate({**output.model_dump(mode="json"), "workspace_id": owner}))
        state = GenerationHistoricalState(value=SessionGenerationExecuteResultDTO(
            run_id=legacy.run_id, status=status,
            outputs=outputs, message_id=result.message_id if result else legacy.message_id,
            job_id=result.job_id if result else legacy.job_id,
            report_back_job_id=result.report_back_job_id if result else legacy.report_back_job_id,
            error=result.error if result else legacy.error,
        ))
    return_error = (result.error or legacy.error) if result and terminal else legacy.error
    return ImportedGenerationRun(
        run_id=legacy.run_id, trigger_key=legacy.idempotency_key, trigger_type=legacy.trigger_type,
        scheduled_for=None if legacy.trigger_type == "manual" else legacy.scheduled_for,
        created_at=legacy.started_at or legacy.scheduled_for, updated_at=legacy.ended_at or legacy.started_at or legacy.scheduled_for,
        phase=phase, state=state, configuration=configuration, error=return_error,
    )


async def import_generator(runtime: PersistentTaskRuntime, request: SessionGeneratorImportRequest) -> str:
    runtime.assert_healthy()
    action = request.configuration.action
    if not isinstance(action, SessionGenerationActionDTO):
        raise ValueError("旧生成器导入只能安装会话动作")  # noqa: TRY004 - 非法动作按公开合同返回 4xx。
    generation = action.generation
    if generation.gateway_id != get_current_gateway_id():
        raise ValueError("旧生成器必须归属当前明确 Gateway")
    if generation.created_from_uri is not None:
        parse_session_thread_uri(generation.created_from_uri)
    definition = SessionGenerationValidateRequest(
        name=request.configuration.name, execution_workspace_id=runtime.store.workspace_id,
        **generation.model_dump(exclude={"created_from_uri", "gateway_id", "ui_policy", "mount_missing", "delete_outputs"}),
    )
    runtime.generation._validate_definition_workspaces(definition)
    async with runtime.locks.lock_for(request.task_id):
        files = imported_files(runtime.store, request)
        if files is None:
            files = {}
            runs = [await _run(runtime, request, legacy, files) for legacy in sorted(request.runs, key=lambda item: (item.scheduled_for, item.run_id))]
            # 缺失 provider 或已删除 locator 只阻止新动作，不能抹去旧历史/已接受运行。
            try:
                result = await runtime.generation.validate_definition(definition)
                reason = result.reason
            except (NotFoundError, KeyError, ValueError) as error:
                reason = f"旧生成器导入后需要处理: {error}"
            install_generator(runtime.store, request, runs, files, reason=reason)
        _retire_files(runtime._workspace_root, request.task_id, files)
        runtime.notify(request.task_id)
    return request.task_id
