"""持久任务的请求、冻结回执和公开目标共用精确 Thread URI 合同。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.persistent_task import (
    RuntimeSourceActionDTO,
    TaskEvaluationDTO,
    TaskModelCheckDTO,
    TaskThreadTargetDTO,
    TaskTriggerDTO,
)

SESSION = "ses_reference"
THREAD = "thr_reference"
URI = thread_resource_uri("ws_reference", SESSION, THREAD)
REFERENCES = [
    (TaskTriggerDTO, "source_uri", {"version": "1", "type_id": "builtin.execution_end"}),
    (TaskModelCheckDTO, "thread_uri", {"instruction": "/private/用户正文保持原样"}),
    (RuntimeSourceActionDTO, "target_uri", {
        "version": "1", "type_id": "builtin.runtime_source", "delivery_mode": "context_only",
    }),
    (TaskEvaluationDTO, "thread_uri", {
        "source_item_id": "item_reference", "job_id": "job_reference", "execution_id": "exec_reference",
    }),
    (TaskThreadTargetDTO, "uri", {"session_id": SESSION, "thread_id": THREAD}),
]


@pytest.mark.parametrize("model,field,fields", REFERENCES)
@pytest.mark.parametrize("uri", [
    "", "/private/task-reference", r"C:\private\task-reference", r"\\target\private\task-reference",
    "file:///private/task-reference", URI + "?revision=1", URI + "#snapshot",
    "boxteam://workspace/ws_reference/resources/session/ses_reference",
    "boxteam://workspace/ws_reference/resources/files/root/source.txt",
])
def test_task_references_reject_native_or_inexact_thread(
    model: type[BaseModel], field: str, fields: dict[str, object], uri: str,
) -> None:
    with pytest.raises(ValidationError) as rejected:
        model.model_validate({**fields, field: uri})
    if model in {TaskEvaluationDTO, TaskThreadTargetDTO} and uri:
        assert uri not in str(rejected.value), "冻结回执错误不能重新暴露被拒绝的位置"
        if "task-reference" in uri:
            assert "task-reference" not in str(rejected.value), "路径经过转义仍不能进入公开错误"


@pytest.mark.parametrize("model,field,fields", REFERENCES)
@pytest.mark.parametrize("authority", [None, "gateway_" + "a" * 32])
def test_task_references_preserve_dangling_target_and_user_content(
    model: type[BaseModel], field: str, fields: dict[str, object], authority: str | None,
) -> None:
    uri = thread_resource_uri("ws_reference", SESSION, THREAD, gateway_authority=authority)
    payload = {**fields, field: uri}
    value = model.model_validate(payload)
    restored = model.model_validate_json(value.model_dump_json())
    assert restored == value and restored.model_dump()[field] == uri
    assert all(restored.model_dump()[key] == item for key, item in fields.items())
