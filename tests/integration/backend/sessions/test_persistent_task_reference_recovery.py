"""产品入口生成判断回执，损坏引用必须在重新读取和普通响应前明确拒绝。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from tests.integration.backend.sessions.test_persistent_task_models import (
    _answer,
    _begin,
    _model_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _create,
    _release_match,
    _run,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 当前文件独立工作区和模型回放。
)


async def test_corrupt_evaluation_reference_is_rejected_without_rewriting_history(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    judge_id, judge_thread, judge_uri = await _target(case)
    _work_id, _work_thread, work_uri = await _target(case)
    task = await _create(case, _model_configuration(work_uri, judge_uri))
    run = await _begin(case, task, "reference-recovery")
    await case.plan(judge_id, [{"content": json.dumps(_answer(run, "wait"))}])
    _release_match(case, "reference-recovery")
    actual = await _run(case, task, run, "checked")
    assert actual["evaluation"]["thread_uri"] == judge_uri
    detail_url = f"{ROOT}/{task['task_id']}/runs/{run['run_id']}"
    original_detail = (await case.client.get(detail_url)).json()["data"]
    assemblies = case.sql(judge_id, judge_thread, "SELECT assembly_id,request_hash FROM context_assemblies")
    assert len(assemblies) == 1
    database = case.workspace / ".boxteam/state/workspace.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        original = connection.execute(
            "SELECT evaluation_json FROM persistent_task_runs WHERE run_id=?", (run["run_id"],),
        ).fetchone()[0]
    requests = case.requests()
    # 关键状态由产品生成；只在此注入损坏，不替产品执行判断或建立准入。
    for uri in ["/private/task-reference", r"C:\private\task-reference", r"\\target\private\task-reference",
                "file:///private/task-reference", judge_uri + "?revision=1",
                judge_uri.rsplit("/threads/", 1)[0]]:
        payload = {**json.loads(original), "thread_uri": uri}
        damaged = json.dumps(payload, ensure_ascii=False)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE persistent_task_runs SET evaluation_json=? WHERE run_id=?", (damaged, run["run_id"]))
        try:
            # 同一数据库只有一个原生 owner；新 repository 通过其新连接重读，不能另抢运行锁。
            reader = PersistentTaskStore(case.container.workspace_activity_service.store,
                                         workspace_id=case.container.workspace_id)
            with pytest.raises(ValidationError) as rejected:
                reader.run(task["task_id"], run["run_id"])
            assert uri not in str(rejected.value)
            for url in [f"{ROOT}/{task['task_id']}/runs", detail_url]:
                response = await case.client.get(url)
                assert response.status_code == 400, response.text
                detail = response.json()["detail"]
                assert "thread_uri" in detail and uri not in detail
                assert "task-reference" not in detail
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("SELECT evaluation_json FROM persistent_task_runs WHERE run_id=?", (run["run_id"],)).fetchone() == (damaged,)
            assert case.requests() == requests
        finally:
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE persistent_task_runs SET evaluation_json=? WHERE run_id=?", (original, run["run_id"]))
        response = await case.client.get(detail_url)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == original_detail
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert case.sql(judge_id, judge_thread, "SELECT assembly_id,request_hash FROM context_assemblies") == assemblies
    assert case.call_count(judge_id, judge_thread) == 1
