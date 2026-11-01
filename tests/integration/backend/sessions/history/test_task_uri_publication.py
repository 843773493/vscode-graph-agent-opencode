"""产品入口生成任务和历史，发布失败不得持久化复制后损坏的目标引用。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime

import pytest

from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    PersistentTaskDTO,
    RuntimeSourceActionDTO,
)
from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前测试文件隔离工作区和产物。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_task_uri_publication_rolls_back_and_same_revision_recovers(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "任务引用发布")
    main = await case.client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert main.status_code == 200, main.text
    thread_id = main.json()["data"]["thread_id"]
    call, = await case.plan(session_id, [{"content": "保留原历史。"}])
    accepted = await _send(case, session_id, "建立待保护的执行历史。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    assert isinstance(case.container.job_service, JobService)
    await case.container.job_service.drain_finalizations()
    paths = case.container.session_service.path_resolver
    thread_root = paths.resolve_thread_node(session_id, thread_id)
    canonical = thread_root / "rollout/rollout.jsonl"
    original_history = canonical.read_bytes()
    index = thread_root / "rollout/index.sqlite"
    logs = tuple(paths.resolve_session_node(session_id).joinpath("logs/llm_requests").glob("*.json"))
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text(encoding="utf-8"))}
    audit = audit_sealed_requests(index, logs, captures)
    native_paths = ["/private/task-publication", r"C:\private\task-publication",
                    r"\\target\private\task-publication", "file:///private/task-publication"]
    assert case.gateway_id is not None
    target = thread_resource_uri(case.container.session_service.workspace_id, session_id, thread_id,
                                 gateway_authority=case.gateway_id)
    configuration = {
        "name": "保护目标引用",
        "trigger": {"type_id": "builtin.manual", "version": "1"},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": "正文允许路径：" + " | ".join(native_paths)},
        "action": {"type_id": "builtin.runtime_source", "version": "1", "target_uri": target, "delivery_mode": "context_only"},
    }
    created = await case.client.post("/api/v1/persistent-tasks", json={
        "idempotency_key": "publication-create", "enabled": False, "configuration": configuration,
    })
    assert created.status_code == 200, created.text
    original_task = created.json()["data"]
    assert original_task["configuration"]["action"]["target_uri"] == target
    task_id = original_task["task_id"]
    state = case.container.workspace_activity_service.store
    store = case.container.persistent_task_runtime.store

    def stored_facts() -> dict[str, list[list[object]]]:
        with closing(state.connection()) as connection:
            return {table: [list(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE task_id=? ORDER BY rowid", (task_id,),
            )] for table in ("persistent_tasks", "persistent_task_definitions", "persistent_task_schedules",
                            "persistent_task_execution_sources", "persistent_task_runs")}

    original_facts = stored_facts()
    original_update = store.update
    failures: list[dict[str, object]] = []
    payload = {"expected_revision": original_task["revision"], "configuration": {**configuration, "name": "更新目标引用"}}
    for native in native_paths:
        def broken_update(
            task_id: str, *, expected_revision: int, configuration: PersistentTaskConfigurationDTO,
            now: datetime, source_cursor: int | None = None, native: str = native,
        ) -> PersistentTaskDTO:
            assert isinstance(configuration.action, RuntimeSourceActionDTO)
            broken = configuration.model_copy(update={"action": configuration.action.model_copy(update={"target_uri": native})})
            return original_update(task_id, expected_revision=expected_revision, configuration=broken,
                                   now=now, source_cursor=source_cursor)

        # 只在原 owner 的最终发布入口损坏引用，所有事实仍由产品入口生成。
        with monkeypatch.context() as fault:
            fault.setattr(store, "update", broken_update)
            response = await case.client.put(f"/api/v1/persistent-tasks/{task_id}", json=payload)
            failures.append({"native": native, "status": response.status_code, "body": response.json()})
            case.write_actual("task-publication-progress", {"failures": failures})
            assert response.status_code == 500, response.text
            assert response.json()["message"] == "请求处理失败: stage=http_request, error=PydanticSerializationError"
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert "task-publication" not in response.text
        assert stored_facts() == original_facts
        restored = await case.client.get(f"/api/v1/persistent-tasks/{task_id}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["data"] == original_task

    retried = await case.client.put(f"/api/v1/persistent-tasks/{task_id}", json=payload)
    assert retried.status_code == 200, retried.text
    task = retried.json()["data"]
    assert task["revision"] == original_task["revision"] + 1
    assert task["definition_revision"] == original_task["definition_revision"] + 1
    assert task["configuration"]["action"]["target_uri"] == target
    assert task["configuration"]["context"]["content"] == configuration["context"]["content"]
    facts = stored_facts()
    assert len(facts["persistent_task_definitions"]) == 2 and facts["persistent_task_runs"] == []
    assert canonical.read_bytes() == original_history
    assert audit_sealed_requests(index, logs, captures) == audit
    assert len(case.requests()) == 1
    case.write_actual("task-publication", {"session_id": session_id, "thread_id": thread_id,
                      "task_id": task_id, "failures": failures, "task": task,
                      "before": original_facts, "after": facts, "sealed_request_audit": audit})
