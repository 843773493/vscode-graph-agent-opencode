"""真实执行后的上下文投影损坏必须拒绝发布，原历史和封存请求保持。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import pytest
from pydantic_core import PydanticSerializationError

from app.agents.tools.session_history import _result_json
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.job.service import JobService
from app.services.business.session_context_projection import SessionContextReadSnapshot
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前文件隔离工作区、端口及产物。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests

Field = Literal["read", "item", "resource", "search", "match"]


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_context_publication_rejects_copied_paths_without_rewriting_history(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    session_id = await create_session(case.client, "上下文发布校验")
    session = await case.container.session_service.get(session_id)
    thread = await case.client.get(f"/api/v1/sessions/{session_id}/main-thread")
    assert thread.status_code == 200, thread.text
    thread_id = thread.json()["data"]["thread_id"]
    marker = "context-publication-marker"
    native_paths = ["/private/context-publication", r"C:\private\context-publication",
                    r"\\target\private\context-publication", "file:///private/context-publication"]
    call, = await case.plan(session_id, [{"content": marker + " 保持原回答。"}])
    accepted = await _send(case, session_id, marker + " 正文允许这些原生路径：" + " | ".join(native_paths))
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    assert isinstance(case.container.job_service, JobService)
    await case.container.job_service.drain_finalizations()
    root = case.container.session_service.path_resolver.resolve_thread_node(session_id, thread_id)
    canonical = root / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    index = root / "rollout/index.sqlite"
    logs = tuple(case.container.session_service.path_resolver.resolve_session_node(session_id).joinpath("logs/llm_requests").glob("*.json"))
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text(encoding="utf-8"))}
    audit = audit_sealed_requests(index, logs, captures)
    resource: dict[str, object] = {"resource_identity": session.resource_identity, "uri": session.uri, "scope": session.scope}
    assemblies = await case.client.post("/api/v1/context/read", json={**resource, "view": "assemblies"})
    assert assemblies.status_code == 200, assemblies.text
    assembly_ref = assemblies.json()["data"]["items"][0]["assembly_ref"]
    read_payload = {**resource, "view": "assembly", "assembly_ref": assembly_ref, "max_chars": 65536, "limit": 200}
    search_payload = {**resource, "query": marker}
    service = case.container.session_context_query_service
    original_read, original_search = service.read_context, service.search_context
    read_before = await case.client.post("/api/v1/context/read", json=read_payload)
    search_before = await case.client.post("/api/v1/context/search", json=search_payload)
    history_url = f"/api/v1/sessions/{session_id}/threads/{thread_id}/history"
    history_before = await case.client.post(history_url, json={"direction": "tail", "turns": 10})
    assert all(response.status_code == 200 for response in (read_before, search_before, history_before))
    read_data, search_data = read_before.json()["data"], search_before.json()["data"]
    assert search_data["matches"] and any(marker in match["preview"] for match in search_data["matches"])
    assert read_data["items"][0]["kind"] == "assembly"
    assert any(ref["display_uri"] is not None for ref in read_data["items"][0]["resource_refs"])
    assert [turn["turn_id"] for turn in history_before.json()["data"]["items"]] == [accepted["turn_id"]]
    # 首屏可能只容纳 selection 与部分历史；按真实 cursor 读完，不能把首屏当完整 assembly。
    pages = [read_data]
    cursors: set[str] = set()
    while pages[-1]["has_more"]:
        cursor = pages[-1]["next_cursor"]
        assert isinstance(cursor, str) and cursor not in cursors and len(pages) < 100
        cursors.add(cursor)
        page = await case.client.post("/api/v1/context/read", json={**read_payload, "cursor": cursor})
        assert page.status_code == 200, page.text
        pages.append(page.json()["data"])
    case.write_actual("context-publication-baseline", {"pages": pages, "search": search_data})
    visible_text = "".join(item["text"] or "" for page in pages for item in page["items"])
    assert all(path in visible_text for path in native_paths)
    failures: list[dict[str, object]] = []

    fields: tuple[Field, ...] = ("read", "item", "resource", "search", "match")
    for field in fields:
        for native in native_paths:
            # 真实 owner 完成查询后才复制损坏展示引用，不补造历史或改动封存来源。
            async def broken_read(
                request: SessionContextReadRequest, field: Field = field, native: str = native,
            ) -> SessionContextReadResultDTO:
                result = await original_read(request)
                if field == "read":
                    return result.model_copy(update={"uri": native})
                item = result.items[0]
                if field == "item":
                    item = item.model_copy(update={"uri": native})
                else:
                    refs = list(item.resource_refs)
                    at = next(i for i, ref in enumerate(refs) if ref.display_uri is not None)
                    refs[at] = refs[at].model_copy(update={"display_uri": native})
                    item = item.model_copy(update={"resource_refs": refs})
                return result.model_copy(update={"items": [item, *result.items[1:]]})

            async def broken_search(
                request: SessionContextSearchRequest, field: Field = field, native: str = native,
            ) -> SessionContextSearchResultDTO:
                result = await original_search(request)
                if field == "search":
                    return result.model_copy(update={"uri": native})
                match = result.matches[0].model_copy(update={"uri": native})
                return result.model_copy(update={"matches": [match, *result.matches[1:]]})

            with monkeypatch.context() as fault:
                if field in {"search", "match"}:
                    fault.setattr(service, "search_context", broken_search)
                    response = await case.client.post("/api/v1/context/search", json=search_payload)
                else:
                    fault.setattr(service, "read_context", broken_read)
                    response = await case.client.post("/api/v1/context/read", json=read_payload)
                failures.append({"field": field, "native": native, "status": response.status_code, "body": response.json()})
                case.write_actual("context-publication-progress", {"responses": failures})
                assert response.status_code == 500, response.text
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                assert native not in response.text and json.dumps(native)[1:-1] not in response.text
                # 模型工具的既有出口也必须拒绝同一损坏投影，不能只依赖 FastAPI。
                with pytest.raises(PydanticSerializationError):
                    if field in {"search", "match"}:
                        await _result_json(service.search_context(SessionContextSearchRequest.model_validate(search_payload)))
                    else:
                        await _result_json(service.read_context(SessionContextReadRequest.model_validate(read_payload)))

    original_freeze = service.freeze_read_context
    for native in native_paths:
        async def broken_freeze(
            request: SessionContextReadRequest, native: str = native,
        ) -> SessionContextReadSnapshot:
            snapshot = await original_freeze(request)
            item = snapshot.items[0].model_copy(update={"uri": native})
            return snapshot.model_copy(update={"items": [item, *snapshot.items[1:]]})

        # 分页计算自身会序列化 item；服务内部发现的损坏也属于服务器错误。
        with monkeypatch.context() as fault:
            fault.setattr(service, "freeze_read_context", broken_freeze)
            response = await case.client.post("/api/v1/context/read", json=read_payload)
            failures.append({"field": "page.item", "native": native, "status": response.status_code, "body": response.json()})
            case.write_actual("context-publication-progress", {"responses": failures})
            assert response.status_code == 500, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert response.json()["message"] == "请求处理失败: stage=http_request, error=PydanticSerializationError"
            assert native not in response.text and json.dumps(native)[1:-1] not in response.text
            with pytest.raises(PydanticSerializationError):
                await _result_json(service.read_context(SessionContextReadRequest.model_validate(read_payload)))

    read_after = await case.client.post("/api/v1/context/read", json=read_payload)
    search_after = await case.client.post("/api/v1/context/search", json=search_payload)
    history_after = await case.client.post(history_url, json={"direction": "tail", "turns": 10})
    assert all(response.status_code == 200 for response in (read_after, search_after, history_after))
    assert read_after.json()["data"] == read_data
    assert search_after.json()["data"] == search_data
    assert history_after.json()["data"] == history_before.json()["data"]
    assert canonical.read_bytes() == original
    assert audit_sealed_requests(index, logs, captures) == audit
    assert len(case.requests()) == 1
    case.write_actual("context-publication", {
        "session_id": session_id, "thread_id": thread_id, "turn_id": accepted["turn_id"],
        "canonical_sha256": hashlib.sha256(original).hexdigest(), "failures": failures,
        "read": read_data, "search": search_data, "sealed_request_audit": audit,
    })
