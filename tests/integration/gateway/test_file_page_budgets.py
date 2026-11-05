"""真实联邦文件查询从双根继续单根，重新分配的页预算不废弃原续点。"""

import asyncio
import json

import httpx

from app.schemas.search_federation import (
    MAX_SEARCH_OUTPUT_BYTES,
    FederatedSearchEventDTO,
)
from tests.integration.gateway.test_federated_search import Network
from tests.integration.gateway.test_federated_search import (
    network as network,  # noqa: PLC0414 - 沿原双后端、辅助服务及端口租约 fixture
)
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
)
from tests.support.processes.workspace import workspace_backend_id_for_root


async def test_two_roots_resume_subset_with_new_page_budget(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    observations = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=25, trust_env=False) as client:
            await acquire_gateway_guest(client)
            workspace_id = workspace_backend_id_for_root(network.workspace)
            client.headers.update({"X-BoxTeam-Target-Gateway-Id": network.gateway_id,
                                   "X-BoxTeam-Workspace-Id": workspace_id})
            roots = await client.get("/api/v1/workspace/files/roots")
            roots.raise_for_status()
            root = roots.json()["data"]["workspace_uri"]
            owner = {"gateway_id": network.gateway_id, "workspace_id": workspace_id}

            async def search(payload: dict[str, object], request_id: str) -> list[FederatedSearchEventDTO]:
                async with client.stream("POST", "/api/gateway/search", json=payload,
                                         headers={"X-Request-ID": request_id}) as response:
                    assert response.status_code == 200, (await response.aread()).decode()
                    assert response.headers["X-Request-ID"] == request_id
                    events = [FederatedSearchEventDTO.model_validate_json(line.removeprefix("data: "))
                              async for line in response.aiter_lines() if line.startswith("data: ")]
                assert events[0].type == "scope" and events[-1].type == "end"
                assert [event.sequence for event in events] == list(range(1, len(events) + 1))
                assert {event.request_id for event in events} == {request_id}
                assert response.num_bytes_downloaded <= MAX_SEARCH_OUTPUT_BYTES
                observations.append({"request": payload, "events": [event.model_dump(mode="json") for event in events],
                                     "output_bytes": response.num_bytes_downloaded})
                return events

            for operation in ("name", "content"):
                targets = [{"owner": owner, "target": {"uri": root + "/" + directory,
                    "search_options": {"all_matches": True, "context_lines": 1,
                        "include_globs": ["*.txt"], "excluded_resource_uris": [root + "/" + directory + "/covered.txt"]}
                    if operation == "content" else {}}} for directory in ("search-one", "search-two")]
                query = {"category": "file", "operation": operation, "query": "needle",
                         "match_mode": "literal" if operation == "content" else "fuzzy", "limit": 50, "file_targets": targets}
                first = await search({"scope": {"mode": "selected", "owners": [owner]}, "queries": [query],
                                      "max_results": 3}, f"req_budget_{operation}_first")
                assert first[0].scope is not None and first[-1].summary is not None
                frozen = first[0].scope.scope.model_dump(mode="json")
                branches = first[-1].summary.branches
                assert [branch.allocated_results for branch in branches] == [2, 1]
                assert [branch.returned_results for branch in branches] == [2, 1]
                assert all(branch.state == "limited" and branch.cursor for branch in branches)
                pages = {event.branch.branch.resource_uri: event.page for event in first
                         if event.type == "page" and event.branch is not None}
                for branch in reversed(branches):
                    assert branch.branch.resource_uri is not None
                    page = pages[branch.branch.resource_uri]
                    assert page is not None and page.files is not None
                    files = page.files
                    positions = [(hit.uri, hit.line, hit.utf16_column) for hit in files.matches]
                    resources = [entry.uri for entry in files.entries]
                    zero = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                        "continuations": [{"branch": branch.branch.model_dump(mode="json"), "cursor": branch.cursor}],
                        "max_results": 0}, f"req_budget_{operation}_zero_{branch.allocated_results}")
                    assert zero[-1].summary is not None
                    zero_branch, = zero[-1].summary.branches
                    assert (zero_branch.code, zero_branch.allocated_results, zero_branch.returned_results) == ("output_limit", 0, 0)
                    assert zero_branch.cursor == branch.cursor and not any(event.type == "page" for event in zero)

                    cursor = branch.cursor
                    # 原1条页变3条，原2条页变1条；只继续该根，不能借旧页尾跳过尾项。
                    for maximum in ((3,) if branch.allocated_results == 1 else (1, 2)):
                        continued = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                            "continuations": [{"branch": branch.branch.model_dump(mode="json"), "cursor": cursor}],
                            "max_results": maximum}, f"req_budget_{operation}_next_{branch.allocated_results}_{maximum}")
                        assert continued[0].scope is not None and continued[0].scope.scope.model_dump(mode="json") == frozen
                        assert continued[-1].summary is not None
                        following, = continued[-1].summary.branches
                        assert following.branch == branch.branch and following.allocated_results == maximum
                        assert following.state in {"complete", "limited"} and following.returned_results <= maximum
                        event, = [event for event in continued if event.type == "page"]
                        assert event.page is not None and event.page.files is not None
                        next_files = event.page.files
                        positions.extend((hit.uri, hit.line, hit.utf16_column) for hit in next_files.matches)
                        resources.extend(entry.uri for entry in next_files.entries)
                        if operation == "content":
                            assert next_files.contexts and following.returned_results == len(next_files.matches)
                        cursor = following.cursor
                        if following.state == "complete":
                            assert cursor is None
                            break
                        assert cursor
                    else:
                        raise AssertionError("原文件分支未在预期有界页数内完成")
                    if operation == "content":
                        assert positions == [(branch.branch.resource_uri + "/" + name, line, column)
                                             for name in ("needle-a.txt", "needle-b.txt") for line, column in ((1, 4), (3, 1))]
                    else:
                        assert resources == [branch.branch.resource_uri + "/" + name
                                             for name in ("needle-a.txt", "needle-b.txt", "needle-c.md")]
    finally:
        (network.artifacts / "file-page-budgets.json").write_text(
            json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        await asyncio.to_thread(close_gateway_process, gateway)
