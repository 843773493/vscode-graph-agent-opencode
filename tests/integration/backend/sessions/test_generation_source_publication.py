"""真实任务入口生成配置，来源引用损坏不得进入查询或原任务事务。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    SessionGenerationActionDTO,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from tests.integration.backend.sessions.test_persistent_task_generations import (
    generation_configuration,
)
from tests.integration.backend.sessions.test_persistent_tasks import (
    ROOT,
    _create,
    _target,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 当前文件独立工作区、端口和产物。
)


async def test_generation_source_cold_read_and_publication_share_original_contract(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    _session, _thread, uri = await _target(case)
    configuration = generation_configuration(case.container.workspace_id)
    configuration["action"]["generation"]["created_from_uri"] = uri
    task = await _create(case, configuration)
    # 本场景只测损坏后的冷读；先真实排空 worker，避免它与故障注入并发扫描同一行。
    await case.container.persistent_task_runtime.stop()
    endpoint = f"{ROOT}/{task['task_id']}"
    database = case.workspace / ".boxteam/state/workspace.sqlite"
    reader = PersistentTaskStore(case.container.workspace_activity_service.store, workspace_id=case.container.workspace_id)
    with closing(sqlite3.connect(database)) as connection:
        original = connection.execute(
            "SELECT configuration_json FROM persistent_task_definitions WHERE task_id=?", (task["task_id"],),
        ).fetchone()[0]
    valid = PersistentTaskConfigurationDTO.model_validate_json(original)
    assert isinstance(valid.action, SessionGenerationActionDTO)
    for source in ("/private/generation-source", r"C:\private\generation-source",
                   r"\\target\private\generation-source", "file:///private/generation-source"):
        damaged = json.loads(original)
        damaged["action"]["generation"]["created_from_uri"] = source
        damaged_json = json.dumps(damaged)
        rejected_input = await case.client.post(ROOT, json={"idempotency_key": "invalid-source", "configuration": damaged})
        assert rejected_input.status_code == 422, rejected_input.text
        assert "generation-source" not in rejected_input.text
        assert rejected_input.json()["request_id"] == rejected_input.headers["X-Request-ID"]
        # 配置由产品创建；手工修改只用于验证持久事实损坏时的冷读拒绝。
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE persistent_task_definitions SET configuration_json=? WHERE task_id=?",
                               (damaged_json, task["task_id"]))
        try:
            with pytest.raises(ValidationError) as rejected:
                reader.get(task["task_id"])
            assert rejected.value.errors()[0]["input"] == source
            for url in (endpoint, ROOT):
                response = await case.client.get(url)
                assert response.status_code == 500, response.text
                assert "generation-source" not in response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("SELECT configuration_json FROM persistent_task_definitions WHERE task_id=?",
                                          (task["task_id"],)).fetchone()[0] == damaged_json
        finally:
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE persistent_task_definitions SET configuration_json=? WHERE task_id=?",
                                   (original, task["task_id"]))
        action = valid.action.model_copy(update={"generation": valid.action.generation.model_copy(update={"created_from_uri": source})})
        copied = valid.model_copy(update={"action": action})
        with pytest.raises(PydanticSerializationError):
            reader.update(task["task_id"], expected_revision=task["revision"], configuration=copied, now=datetime.now(UTC))
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT configuration_json FROM persistent_task_definitions WHERE task_id=?",
                                      (task["task_id"],)).fetchall() == [(original,)]
        repaired = await case.client.get(endpoint)
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["data"] == task
    assert case.requests() == []
