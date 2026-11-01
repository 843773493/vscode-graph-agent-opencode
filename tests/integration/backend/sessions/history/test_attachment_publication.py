"""产品输入生成混合附件历史，再核消息和Turn响应拒绝损坏引用。"""

from __future__ import annotations

import json

import pytest

from app.core.resource_uri import resource_display_uri
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 当前测试独立工作区和端口。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_attachment_publication_rejects_paths_and_keeps_original_history(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    regular = case.workspace / "native-input.txt"
    regular.write_text("普通文件正文", encoding="utf-8")
    uri = resource_display_uri(scope="workspace", scope_id=case.container.workspace_id,
                               kind="files", tail_segments=("root", regular.name),
                               gateway_authority=case.gateway_id)
    session_id = await create_session(case.client, "附件发布边界")
    call, = await case.plan(session_id, [{"content": "混合附件已接受"}])
    native_paths = ["/private/attachment-publication", r"C:\private\attachment-publication",
                    r"\\target\private\attachment-publication", "file:///private/attachment-publication"]
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": "正文允许原生路径：" + " | ".join(native_paths), "attachments": [
            *({"file_id": value} for value in (regular.name, str(regular), uri)),
            upload(b"immutable attachment original").model_dump(mode="json"),
        ]}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    accepted = response.json()["data"]
    await _job(case, accepted["job_id"])
    assert isinstance(case.container.job_service, JobService)
    await case.container.job_service.drain_finalizations()
    thread_id, message_id = accepted["thread_id"], accepted["message_id"]
    root = case.container.session_service.path_resolver.resolve_thread_node(session_id, thread_id)
    canonical = root / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    logs = tuple(case.container.session_service.path_resolver.resolve_session_node(session_id).joinpath("logs/llm_requests").glob("*.json"))
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text(encoding="utf-8"))}
    audit = audit_sealed_requests(root / "rollout/index.sqlite", logs, captures)
    history_url = f"/api/v1/sessions/{session_id}/threads/{thread_id}/history"
    messages_url = f"/api/v1/sessions/{session_id}/messages"
    message_url = f"/api/v1/sessions/{session_id}/messages/{message_id}"
    history = await case.client.post(history_url, json={"direction": "tail"})
    message = await case.client.get(message_url)
    messages = await case.client.get(messages_url)
    assert history.status_code == message.status_code == messages.status_code == 200
    refs = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    assert [ref["file_id"] for ref in refs[:3]] == [uri] * 3
    assert all(ref.get("directory_uri") is None for ref in refs[:3])
    assert refs[3]["directory_uri"] and refs[3]["file_id"].startswith(refs[3]["directory_uri"] + "/")
    assert message.json()["data"]["attachments"] == [
        {key: value for key, value in ref.items() if key != "availability"} | {"data_url": None}
        for ref in refs
    ]
    assert all(native in message.json()["data"]["content"] for native in native_paths)
    history_service, message_service = case.container.session_turn_history_service, case.container.message_service
    original_history, original_message, original_list = history_service.load_history, message_service.get, message_service.list
    failures: list[dict[str, object]] = []

    def break_attachment(result: MessageDTO, field: str, native: str, validate: bool) -> MessageDTO:
        result = result.model_copy(deep=True)
        if validate:
            payload = result.model_dump()
            payload["attachments"][0]["file_id"] = native
            return type(result).model_validate(payload)
        at = 0 if field == "file_id" else 3
        result.attachments[at] = result.attachments[at].model_copy(update={field: native})
        return result

    for boundary, field in (("history", "file_id"), ("history", "directory_uri"),
                            ("message", "file_id"), ("message", "directory_uri"),
                            ("message-validation", "file_id"),
                            ("message-list", "file_id"), ("message-list", "directory_uri"),
                            ("message-list-validation", "file_id")):
        for native in native_paths:
            # 原owner完成真实读取后才损坏DTO，不手填或改写canonical与封存请求。
            async def broken_history(session_id: str, request: TurnHistoryLoadRequest, *, field: str = field, native: str = native, **kwargs):
                result = (await original_history(session_id, request, **kwargs)).model_copy(deep=True)
                attachments = result.items[0].user_messages[0].attachments
                at = 0 if field == "file_id" else 3
                attachments[at] = attachments[at].model_copy(update={field: native})
                return result

            async def broken_message(session_id: str, message_id: str, field: str = field, native: str = native, boundary: str = boundary):
                return break_attachment(await original_message(session_id, message_id), field, native, boundary.endswith("validation"))

            async def broken_list(session_id: str, limit: int = 50, cursor: str | None = None, *, field: str = field, native: str = native, boundary: str = boundary):
                result = (await original_list(session_id, limit, cursor)).model_copy(deep=True)
                index = next(index for index, item in enumerate(result.items) if item.message_id == message_id)
                result.items[index] = break_attachment(result.items[index], field, native, boundary.endswith("validation"))
                return result

            with monkeypatch.context() as fault:
                if boundary == "history":
                    fault.setattr(history_service, "load_history", broken_history)
                    rejected = await case.client.post(history_url, json={"direction": "tail"})
                elif boundary.startswith("message-list"):
                    fault.setattr(message_service, "list", broken_list)
                    rejected = await case.client.get(messages_url)
                else:
                    fault.setattr(message_service, "get", broken_message)
                    rejected = await case.client.get(message_url)
                failures.append({"boundary": boundary, "field": field, "status": rejected.status_code, "body": rejected.json()})
                case.write_actual("attachment-publication-progress", {"failures": failures})
                assert rejected.status_code == 500, rejected.text
                assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
                assert native not in rejected.text and json.dumps(native)[1:-1] not in rejected.text
                assert canonical.read_bytes() == original
    restored_history = await case.client.post(history_url, json={"direction": "tail"})
    restored_message = await case.client.get(message_url)
    restored_messages = await case.client.get(messages_url)
    assert restored_history.status_code == restored_message.status_code == restored_messages.status_code == 200
    assert restored_history.json()["data"] == history.json()["data"]
    assert restored_message.json()["data"] == message.json()["data"]
    assert restored_messages.json()["data"] == messages.json()["data"]
    assert len(case.requests()) == 1 and canonical.read_bytes() == original
    assert audit_sealed_requests(root / "rollout/index.sqlite", logs, captures) == audit
    case.write_actual("attachment-publication", {"history": history.json(), "message": message.json(),
                                               "failures": failures, "request_audit": audit})
