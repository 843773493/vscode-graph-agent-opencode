"""生成定义和持久任务拒绝无副作用，修正后经原入口执行。"""

from __future__ import annotations

import errno
import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 工作区按当前测试路径隔离。
)
from tests.integration.backend.sessions.test_session_generation_strategies import (
    _accepted_generation,
    _catalog_nodes,
    _check_generation,
    _generation_configuration,
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 写入真实 ENOTDIR。
)


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_definition_rejection_preserves_task_then_executes_corrected_prompt(
    source_case: RuntimeSourceCase,
    diagnostic_sink: DiagnosticFailure,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    log_fails: bool,
) -> None:
    case = source_case
    session, thread, _ = await _target(case)
    workspace_id = case.container.session_catalog_service.workspace_id
    configuration = _generation_configuration(
        mode="continue_existing", workspace_id=workspace_id,
        target_session_id=session, target_thread_id=thread,
    )
    original = Draft202012Validator.validate
    causes: list[ValidationError] = []

    def validate(validator: Draft202012Validator, instance: object) -> None:
        try:
            original(validator, instance)
        except ValidationError as error:
            causes.append(error)
            if log_fails:
                diagnostic_sink.primary = error
            raise

    monkeypatch.setattr(Draft202012Validator, "validate", validate)
    before = await _catalog_nodes(case.client)
    native = str(case.workspace / "private-generator-input")
    invalid_configs: list[tuple[dict[str, object], str]] = [
        ({"prompt": {native: native}}, "parameter=prompt, depth=1, rule=type"),
        ({"prompt": "可用提示", native: native}, "parameter=None, depth=0, rule=additionalProperties"),
    ]
    rejected: list[dict[str, object]] = []
    for index, (invalid, coordinate) in enumerate(invalid_configs):
        changed = deepcopy(configuration)
        changed["action"]["generation"]["config"] = invalid
        payload = {
            "name": changed["name"], "execution_workspace_id": workspace_id,
            **changed["action"]["generation"],
        }
        responses = [
            await case.client.post("/api/v1/session-generations/validate-definition", json=payload),
            await case.client.post("/api/v1/persistent-tasks", json={
                "idempotency_key": f"invalid-{index}", "configuration": changed,
            }),
        ]
        for response in responses:
            assert response.status_code == 400, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert coordinate in response.json()["detail"]
            assert "private-generator-input" not in response.text
            assert "diagnostic-log-secret" not in response.text
            assert ("diagnostic_sink_error=NotADirectoryError" in response.text) == log_fails
            rejected.append(response.json())
    assert await _catalog_nodes(case.client) == before
    assert await case.container.job_service.list(session_id=session) == []
    tasks = await case.client.get("/api/v1/persistent-tasks")
    assert tasks.status_code == 200 and tasks.json()["data"]["items"] == []

    valid = await case.client.post("/api/v1/session-generations/validate-definition", json={
        "name": configuration["name"], "execution_workspace_id": workspace_id,
        **configuration["action"]["generation"],
    })
    assert valid.status_code == 200 and valid.json()["data"]["provider_available"] is True
    created = await case.client.post("/api/v1/persistent-tasks", json={
        "idempotency_key": "corrected", "configuration": configuration,
    })
    assert created.status_code == 200, created.text
    task = created.json()["data"]
    changed = deepcopy(configuration)
    changed["action"]["generation"]["config"] = invalid_configs[0][0]
    updated = await case.client.put(f"/api/v1/persistent-tasks/{task['task_id']}", json={
        "expected_revision": task["revision"], "configuration": changed,
    })
    assert updated.status_code == 400, updated.text
    assert updated.headers["X-Request-ID"] == updated.json()["request_id"]
    assert "private-generator-input" not in updated.text
    unchanged = await case.client.get(f"/api/v1/persistent-tasks/{task['task_id']}")
    assert unchanged.status_code == 200 and unchanged.json()["data"] == task
    assert len(causes) == 5
    assert all("private-generator-input" in str(error) for error in causes)
    assert all(any(record.exc_info and record.exc_info[1] is error for record in caplog.records) for error in causes)
    if log_fails:
        assert [error.errno for error in diagnostic_sink.errors] == [errno.ENOTDIR] * 5
        assert all(str(diagnostic_sink.destination) in "\n".join(error.__notes__) for error in causes)
    else:
        assert diagnostic_sink.errors == []

    await case.plan(session, [{"content": "修正定义后沿原任务完成"}], thread_id=thread)
    run = await _accepted_generation(
        case.client, task, await _check_generation(case.client, task, "execute-corrected"),
    )
    job = await _job(case, run["delivery"]["job_id"])
    assert job["session_id"] == session and job["thread_id"] == thread
    assert run["generation"]["outputs"][0]["session_id"] == session
    assert len(causes) == 5
    requests = case.requests()
    assert len(requests) == 1
    assert configuration["action"]["generation"]["config"]["prompt"] in json.dumps(requests, ensure_ascii=False)
    assert "private-generator-input" not in json.dumps(requests, ensure_ascii=False)
    assert await _catalog_nodes(case.client) == before
    case.write_actual("definition-rejection-and-execution", {
        "rejected": rejected, "invalid_update": updated.json(), "task": task, "run": run, "job": job,
    })
