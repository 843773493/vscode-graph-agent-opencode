"""真实消息与 pending reminder 经派发后产生轻量标记，详情仍读原 Item。"""

import asyncio
import base64
import json

import httpx
import pytest

from app.api.deps import get_llm_request_log_reader
from app.core.path_utils import workspace_context
from app.domain.itemized.hashing import contribution_content_hash_and_length
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.runtime_reminder import submit_runtime_reminder
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    recording_runtime_case as recording_runtime_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.support.api_helpers import with_message_run_operation_id


@pytest.fixture
async def notice_client(history_client: httpx.AsyncClient, recording_runtime_case: RuntimeCase):
    application = history_client._transport.app
    container = recording_runtime_case.runner.container
    application.dependency_overrides[get_llm_request_log_reader] = lambda: container.llm_request_log_reader
    yield history_client


@pytest.mark.parametrize("mode", ["last_completed_turn_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_fork_retains_context_metadata_and_frozen_body_after_source_deletion(
    production_fork_container, fork_execution_message, mode,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    guidance = container.workspace_root / "AGENTS.md"
    guidance.write_text("# 冻结来源\n\nFORK_CONTEXT_SOURCE：历史应保留此版本。\n", encoding="utf-8")
    source = await container.session_service.create(SessionCreateRequest(title="上下文历史来源"))
    await fork_execution_message(container, source.session_id, content="生成可独立继承的上下文历史")
    source_thread = container.session_service.path_resolver.main_thread_id(source.session_id)
    page_request = TurnHistoryLoadRequest(direction="tail")
    original = container.checkpointer.load_history(source.session_id, source_thread, page_request)
    markers = [part for turn in original.items for part in turn.response_parts if part.kind == "context_notice"]
    assert markers
    frozen = {}
    for marker in markers:
        reference = marker.context_content_ref
        assert reference.kind == "sealed_contribution"
        frozen[marker.part_id] = container.checkpointer.read_context_contribution(
            source.session_id, source_thread, historical_owner=(reference.session_id, reference.thread_id),
            assembly_id=reference.assembly_id, contribution_id=reference.contribution_id,
        )
    assert any("FORK_CONTEXT_SOURCE" in body for body in frozen.values() if isinstance(body, str))
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode)
    result = await container.session_context_fork_service.fork(request)
    guidance.write_text("当前版本不应替代冻结来源。\n", encoding="utf-8")
    await container.session_service.path_resolver.delete_subtree(
        idempotency_key=f"delete-marker-source:{source.session_id}", root_node_id=source.session_id,
    )
    with RolloutCheckpointSaver(container.session_service.path_resolver.sessions_root) as cold:
        restored = cold.load_history(result.session.session_id, result.thread_id, page_request)
        inherited = [part for turn in restored.items for part in turn.response_parts if part.kind == "context_notice"]
        assert inherited == markers
        for marker in inherited:
            reference = marker.context_content_ref
            assert reference.kind == "sealed_contribution"
            assert cold.read_context_contribution(
                result.session.session_id, result.thread_id,
                historical_owner=(reference.session_id, reference.thread_id),
                assembly_id=reference.assembly_id, contribution_id=reference.contribution_id,
            ) == frozen[marker.part_id]


async def test_inspect_frozen_contribution_from_real_dispatched_request(
    recording_runtime_case: RuntimeCase, notice_client: httpx.AsyncClient,
) -> None:
    case = recording_runtime_case
    container = case.runner.container
    guidance = case.runner.workspace / "AGENTS.md"
    guidance.write_text("# 真实冻结来源\n\nCONTEXT_SOURCE_BEFORE_REMOVAL\n", encoding="utf-8")
    session_id, thread_id = await _session(case.runner)
    base = f"/api/v1/sessions/{session_id}/threads/{thread_id}"
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        response = await notice_client.post(base + "/messages", json=with_message_run_operation_id({
            "message": {"content": "检查真实来源正文"}, "run": {},
        }))
        assert response.status_code == 200, response.text
        job = container.job_service._jobs[response.json()["data"]["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        snapshot = case.model_dispatches[-1].snapshot
        history = await notice_client.post(base + "/history", json={"direction": "tail"})
        assert history.status_code == 200, history.text
        parts = [part for turn in history.json()["data"]["items"] for part in turn["response_parts"]
                 if part["kind"] == "context_notice"]
        assert parts
        assert all(part["context_model_call_id"] == snapshot.model_call_id for part in parts)
        assert all(part["context_anchor"] == "before_user_message" for part in parts)
        turn = history.json()["data"]["items"][0]
        assert all(part["context_anchor_ordinal"] == turn["root_logical_item_ordinal"] for part in parts)
        marker = next(part for part in parts if part["context_source_id"] == "agents:workspace")
        assert marker["source"]["item_id"] is None
        assert marker["text"] == ""
        selected = next(entry for entry in snapshot.selection
                        if entry.contribution_id == marker["context_content_ref"]["contribution_id"])
        source = next(value for value in snapshot.contributions if value.contribution_id == selected.contribution_id)
        reference = {"kind": "sealed_contribution", "session_id": snapshot.session_id,
                     "thread_id": snapshot.thread_id, "assembly_id": snapshot.assembly_id,
                     "contribution_id": source.contribution_id}
        assert marker["context_content_ref"] == reference
        assert marker["context_source_revision"] == source.source_revision
        assert marker["context_content_length"] == source.content_length
        response = await notice_client.post(base + "/items/inspect", json={"operation": "content", "content_ref": reference})
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["location"] is None
        assert result["content_ref"] == reference
        assert len(result["contents"]) == 1
        body = result["contents"][0]["content"]
        assert result["contents"][0]["language"] == "plaintext"
        assert contribution_content_hash_and_length(source.contribution_kind, body) == (source.content_hash, source.content_length)
        # 同时核对真正发送的 wire，不仅验证读取 helper 消费了手工 manifest。
        envelopes = [json.loads(message["content"]) for message in snapshot.request_hash_preimage["messages"]
                     if isinstance(message["content"], str) and message["content"].startswith('{"protocol":"boxteam-context-sources:v2"')]
        assert any(value["body"] == body for envelope in envelopes for value in envelope["sources"])
        assert "CONTEXT_SOURCE_BEFORE_REMOVAL" in body
        guidance.write_text("当前来源已更改，不得替代历史正文。\n", encoding="utf-8")
        guidance.unlink()
        after_removal = await notice_client.post(base + "/items/inspect", json={"operation": "content", "content_ref": reference})
        assert after_removal.status_code == 200, after_removal.text
        assert after_removal.json()["data"] == result
        for payload in (
            {"operation": "content", "content_ref": reference, "item_id": "not-an-item"},
            {"operation": "location", "content_ref": reference},
            {"operation": "content", "content_ref": {**reference, "item_id": "not-an-item"}},
        ):
            rejected = await notice_client.post(base + "/items/inspect", json=payload)
            assert rejected.status_code == 422, rejected.text


async def test_pending_reminders_are_displayed_once_at_the_consuming_request(
    recording_runtime_case: RuntimeCase, notice_client: httpx.AsyncClient,
) -> None:
    case = recording_runtime_case
    container = case.runner.container
    session_id, thread_id = await _session(case.runner)
    base = f"/api/v1/sessions/{session_id}/threads/{thread_id}"

    async def send(content: str):
        response = await notice_client.post(base + "/messages", json=with_message_run_operation_id({
            "message": {"content": content}, "run": {},
        }))
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        job = container.job_service._jobs[result["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        return result

    async def history():
        response = await notice_client.post(base + "/history", json={"direction": "tail"})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def markers(page):
        return [(turn, part) for turn in page["items"] for part in turn["response_parts"]
                if part["kind"] == "context_notice" and "notice-ui-" in part["context_source_id"]]

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        await send("先建立真实执行")
        admission = case.model_admissions[-1]
        for index in range(2):
            reminder = f"保留 <system_reminder> 原文 notice-ui-{index}"
            if index == 1:
                # 长提示用于区分聊天有限预览与 Item 全文读取，不截断持久化原文。
                reminder += "".join(f"\n上下文第 {line} 行：保留这条说明的完整原文。" for line in range(1, 81))
                reminder += "\nCOMPLETE_CONTEXT_TAIL"
            submit_runtime_reminder(checkpointer=container.checkpointer, session_id=session_id, thread_id=thread_id,
                reminder=reminder,
                response_metadata={"source": "interrupt"}, checkpoint_source="interrupt",
                event_identity=f"notice-ui-{index}", execution_id=admission.execution_id)
        assert markers(await history()) == []
        second = await send("继续处理")
        second_call = case.model_dispatches[-1].snapshot.model_call_id
        page = await history()
        notices = markers(page)
        assert len(notices) == 2
        assert {turn["turn_id"] for turn, _ in notices} == {second["turn_id"]}
        assert {part["context_model_call_id"] for _, part in notices} == {second_call}
        assert all(part["context_anchor"] == "before_user_message" for _, part in notices)
        assert all(part["context_anchor_ordinal"] == turn["root_logical_item_ordinal"] for turn, part in notices)
        for _, part in notices:
            assert part["text"] == "" and part["projection"] == "summary"
            response = await notice_client.post(base + "/items/inspect", json={
                "operation": "content", "item_id": part["source"]["item_id"],
            })
            assert response.status_code == 200, response.text
            inspected = response.json()["data"]
            assert inspected["location"]["model_call_id"] == second_call
            assert inspected["location"]["turn_id"] == second["turn_id"]
            assert inspected["contents"][0]["language"] == "plaintext"
            assert "<system_reminder>" in inspected["contents"][0]["content"]
            if "notice-ui-1" in part["context_source_id"]:
                assert "COMPLETE_CONTEXT_TAIL" in inspected["contents"][0]["content"]
        item_id = notices[-1][1]["source"]["item_id"]
        for operation, field in (("request", "request_log"), ("trace", "trace")):
            response = await notice_client.post(base + "/items/inspect", json={
                "operation": operation, "item_id": item_id,
            })
            assert response.status_code == 200, response.text
            inspected = response.json()["data"]
            assert inspected["location"]["model_call_id"] == second_call
            assert inspected[field] is not None
        await send("再次继续，不重复标记旧提示")
        assert len(markers(await history())) == 2


async def test_context_metadata_continuation_keeps_turn_page_and_does_not_read_bodies(
    recording_runtime_case: RuntimeCase, notice_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = recording_runtime_case
    container = case.runner.container
    session_id, thread_id = await _session(case.runner)
    base = f"/api/v1/sessions/{session_id}/threads/{thread_id}"

    async def send(content: str):
        response = await notice_client.post(base + "/messages", json=with_message_run_operation_id({
            "message": {"content": content}, "run": {},
        }))
        assert response.status_code == 200, response.text
        job = container.job_service._jobs[response.json()["data"]["job_id"]]
        await asyncio.wait_for(asyncio.shield(job.task), 20)
        assert job.status.value == "completed", job.error_message
        return response.json()["data"]

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        await send("建立提醒来源执行")
        for index in range(133):
            submit_runtime_reminder(checkpointer=container.checkpointer, session_id=session_id, thread_id=thread_id,
                reminder=f"元数据续页来源 {index}", response_metadata={"source": "interrupt"},
                checkpoint_source="interrupt", event_identity=f"continuation-{index}",
                execution_id=case.model_admissions[-1].execution_id)
        consumed = await send("消费全部提醒")
        response = await notice_client.post(base + "/history", json={"direction": "tail", "turns": 1})
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        assert [turn["turn_id"] for turn in page["items"]] == [consumed["turn_id"]]
        markers = [part for turn in page["items"] for part in turn["response_parts"] if part["kind"] == "context_notice"]
        assert len(markers) == 128
        cursor = markers[-1]["context_cursor"]
        assert cursor and not any(part["truncated"] for part in markers)

        def unexpected_body_read(*args, **kwargs):
            pytest.fail("上下文元数据续页不应重读消息或来源正文")

        reader = container.checkpointer._history_reader._context_reader
        with monkeypatch.context() as guard:
            guard.setattr(reader, "read_projection_records_batch", unexpected_body_read)
            guard.setattr(reader, "read_canonical_items", unexpected_body_read)
            response = await notice_client.post(base + "/history", json={"context_cursor": cursor})
        assert response.status_code == 200, response.text
        more = response.json()["data"]
        assert more["items"] == more["summaries"] == more["internal_executions"] == []
        assert more["next_cursor"] is None and not more["has_more"]
        assert more["source_view_revision"] == page["source_view_revision"]
        assert more["context_notices"][0]["owner_id"] == consumed["turn_id"]
        assert more["context_notices"][0]["owner_kind"] == "turn"
        remaining = [part for batch in more["context_notices"] for part in batch["parts"]]
        assert len(remaining) == 5
        assert all(part["context_cursor"] is None for part in remaining)
        assert len({part["part_id"] for part in [*markers, *remaining]}) == 133
        assert [part["context_plan_ordinal"] for part in markers] + [part["context_plan_ordinal"] for part in remaining] == sorted(
            part["context_plan_ordinal"] for part in [*markers, *remaining])
        assert page["before_cursor"] and page["has_before"]

        invalid = await notice_client.post(base + "/history", json={"context_cursor": cursor, "turns": 1})
        assert invalid.status_code == 422
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for field, value, status in (("thread_id", "other-thread", 400),
                                     ("history_view_revision", decoded["history_view_revision"] + 1, 409)):
            changed = base64.urlsafe_b64encode(json.dumps({**decoded, field: value}).encode()).decode().rstrip("=")
            invalid = await notice_client.post(base + "/history", json={"context_cursor": changed})
            assert invalid.status_code == status, invalid.text
