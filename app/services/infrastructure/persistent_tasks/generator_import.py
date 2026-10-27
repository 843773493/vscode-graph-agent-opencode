"""旧生成器快照的单事务安装；原文件的退休由工作区业务 owner 协调。"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

from app.abstractions.state_conflict import ClientStateConflictError
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.generator_import import SessionGeneratorImportRequest
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    TaskDecisionDTO,
    TaskRunPhase,
)
from app.schemas.internal_v2.session_navigation.generation_state import GenerationState

from .store import PersistentTaskStore


@dataclass(frozen=True)
class ImportedGenerationRun:
    run_id: str
    trigger_key: str
    trigger_type: str
    scheduled_for: datetime | None
    created_at: datetime
    updated_at: datetime
    phase: TaskRunPhase
    state: GenerationState | None
    configuration: PersistentTaskConfigurationDTO
    error: str | None


def imported_files(store: PersistentTaskStore, request: SessionGeneratorImportRequest) -> dict[str, str] | None:
    with closing(store._store.connection()) as connection:
        row = connection.execute("SELECT source_hash,files_json FROM persistent_task_generator_imports WHERE task_id=?", (request.task_id,)).fetchone()
    if row is None:
        return None
    if row[0] != request.source_hash:
        raise ClientStateConflictError("旧生成器已导入，但源快照发生变化")
    return json.loads(row[1])


def install_generator(store: PersistentTaskStore, request: SessionGeneratorImportRequest,
                      runs: list[ImportedGenerationRun], files: dict[str, str], *, reason: str | None) -> None:
    with store._transaction() as connection:
        if connection.execute("SELECT 1 FROM persistent_tasks WHERE task_id=?", (request.task_id,)).fetchone() is not None:
            raise ClientStateConflictError("生成器导入身份已存在，不能覆盖当前任务")
        connection.execute("INSERT INTO persistent_tasks "
                           "(task_id,workspace_id,creation_key,creation_hash,revision,definition_revision,lifecycle,reason,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)", (
            request.task_id, store.workspace_id, f"legacy-generator:{request.task_id}", request.source_hash,
            request.revision, 1, "needs_attention" if reason else request.lifecycle, reason or request.reason,
            request.created_at.isoformat(), request.updated_at.isoformat(),
        ))
        configurations = [request.configuration]
        for run in runs:
            if run.configuration not in configurations:
                configurations.append(run.configuration)
        connection.executemany("INSERT INTO persistent_task_definitions VALUES (?,?,?)", [
            (request.task_id, index, value.model_dump_json()) for index, value in enumerate(configurations, 1)
        ])
        schedule = request.configuration.trigger.schedule()
        if schedule is not None:
            candidate = request.next_run_at or schedule.next_time(request.created_at)
            accepted_points = {run.scheduled_for for run in runs if run.trigger_type == schedule.kind}
            # 旧调度在派发后才写游标；崩溃留下的已接受前缀必须在同次安装中对账。
            while candidate in accepted_points:
                candidate = schedule.next_time(candidate)
            connection.execute("INSERT INTO persistent_task_schedules VALUES (?,?,?,?)", (
                request.task_id, 1, request.created_at.isoformat(),
                candidate.isoformat(),
            ))
        for run in runs:
            decision = TaskDecisionDTO(decision="proceed", reason="由旧生成器显式导入的已启动动作") if run.phase != "pending" else None
            connection.execute("INSERT INTO persistent_task_runs "
                "(run_id,task_id,task_revision,definition_revision,trigger_key,trigger_hash,request_id,check_only,phase,decision_json,"
                "error,created_at,updated_at,trigger_type,scheduled_for,generation_json) VALUES (?,?,?,?,?,?,NULL,0,?,?,?,?,?,?,?,?)", (
                run.run_id, request.task_id, request.revision, configurations.index(run.configuration) + 1,
                run.trigger_key, sha256_jcs({"legacy_trigger_key": run.trigger_key, "source_hash": request.source_hash}),
                run.phase, decision.model_dump_json() if decision else None, run.error,
                run.created_at.isoformat(), run.updated_at.isoformat(), run.trigger_type,
                run.scheduled_for.isoformat() if run.scheduled_for is not None else None,
                run.state.model_dump_json() if run.state else None,
            ))
        connection.execute("INSERT INTO persistent_task_generator_imports VALUES (?,?,?)", (
            request.task_id, request.source_hash, json.dumps(files, ensure_ascii=False),
        ))
