"""真实 Gateway 和两个 Backend 的名称及文件联邦；目录历史由产品入口生成。"""

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.session_catalog_store import SessionCatalogStore
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.search_federation import MAX_SEARCH_OUTPUT_BYTES
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    terminate_process,
    workspace_backend_id_for_root,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass(frozen=True)
class Network:
    workspace: Path
    secondary: Path
    stopped: Path
    gateway_home: Path
    gateway_port: int
    backend_port: int
    artifacts: Path
    backends: list[BackendProcess]

    def start(self):
        return start_gateway_process(
            workspace_root=self.workspace, default_backend_url=f"http://127.0.0.1:{self.backend_port}",
            port=self.gateway_port, boxteam_home=self.gateway_home, artifacts_dir=self.artifacts,
        )


@pytest.fixture
def network(request: pytest.FixtureRequest, integration_workspace_root_path: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Network]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    workspace = Path(integration_workspace_root_path)
    secondary, stopped = workspace / "secondary-b", workspace / "stopped-c"
    for root in (secondary, stopped):
        prepare_default_test_workspace(workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
        install_test_workspace_config(workspace_root=root, config_path=workspace / ".boxteam/workspace.jsonc",
                                      schema_path=workspace / ".boxteam/workspace_schema.jsonc")
    for root in (workspace, secondary):
        for directory in ("search-one", "search-two"):
            target = root / directory
            (target / "needle-directory").mkdir(parents=True)
            (target / "empty").mkdir()
            for name in ("needle-a.txt", "needle-b.txt", "needle-c.md"):
                (target / name).write_text("🧪 needle\ncontext\nneedle\n", encoding="utf-8")
            (target / "covered.txt").write_bytes(b"\xffmodel body must not be read")
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    lease = E2EProcessLease(runtime / "service-ports.json", tuple(ports.port(offset) for offset in (0, 1, 20, 40, 42, 43)))
    fixture = Network(workspace, secondary, stopped, runtime / "gateway-home", ports.port(20), ports.port(0), context.artifacts_dir, [])
    # 本机目录控制链和 Gateway 使用同一原生身份；独立 HOME 会产生互不相认的本机令牌。
    credential = fixture.gateway_home / "state/gateway/credentials/local-token"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(LOCAL_TOKEN_HEADERS["X-Local-Token"] + "\n", encoding="utf-8")
    credential.chmod(0o600)
    with ExitStack() as cleanup:
        lease.acquire()
        cleanup.callback(lease.release)
        # fork 来源会读取真实资源状态；辅助服务使用本装具独占的端口和生命周期。
        terminal = start_terminal_backend(backend_port=ports.port(40), workspace_root=str(workspace), artifacts_dir=fixture.artifacts)
        cleanup.callback(terminate_process, terminal)
        browser = start_browser_manager_processes(
            workspace_root=workspace, backend_port=ports.port(42), frontend_port=ports.port(43),
            backend_host="127.0.0.1", artifacts_dir=fixture.artifacts,
        )
        cleanup.callback(close_browser_manager_processes, browser)
        mount = MountedWorkspace(workspace_backend_id_for_root(workspace), workspace,
                                 f"http://127.0.0.1:{ports.port(40)}", f"http://127.0.0.1:{ports.port(42)}")
        for index, root in enumerate((workspace, secondary)):
            backend = start_backend_process(workspace_root=str(root), port=ports.port(index), log_name=f"backend-{index}",
                                            boxteam_home=fixture.gateway_home, artifacts_dir=fixture.artifacts,
                                            startup_mount=mount if index == 0 else None)
            cleanup.callback(close_backend_process, backend)
            fixture.backends.append(backend)
        # 外部后端登记是查询输入，目录历史不通过磁盘写入或 mock 准备。
        gateway_root = fixture.gateway_home / "state/gateway"
        state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
        registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json", state_store=state)
        try:
            for route, root, url in (("route_b", secondary, f"http://127.0.0.1:{ports.port(1)}"), ("route_c", stopped, "")):
                registry.upsert(WorkspaceTarget(
                    workspace_id=route, backend_workspace_id=workspace_backend_id_for_root(root), name="Unrelated workspace" if route == "route_b" else "Stopped",
                    root_path=str(root), backend_url=url, connection_kind="local", desired_running=False,
                ), activate=False)
        finally:
            registry.close()
            state.close()
        yield fixture


@pytest.mark.asyncio
async def test_exact_scopes_partial_failure_and_restart_continuation(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    observations = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=25, trust_env=False) as client:
            await acquire_gateway_guest(client)
            health = (await client.get("/api/gateway/health")).json()["data"]
            gateway_id = health["gateway_id"]
            listing = (await client.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            route_a = next(item["workspace_id"] for item in listing["items"]
                           if item["backend_workspace_id"] == workspace_backend_id_for_root(network.workspace))
            owners = {item["workspace_id"]: {"gateway_id": gateway_id, "workspace_id": item["backend_workspace_id"]} for item in listing["items"]}
            assert route_a != "route_b" and owners[route_a] != owners["route_b"]
            sessions = {}
            for route in (route_a, "route_b"):
                client.headers["X-BoxTeam-Workspace-Id"] = route
                sessions[route] = [await create_session(client, f"Shared Search {index}") for index in range(3)]

            async def navigation() -> dict[str, object]:
                snapshots = {}
                for route in (route_a, "route_b"):
                    response = await client.get("/api/v1/session-catalog/snapshot", headers={"X-BoxTeam-Workspace-Id": route})
                    response.raise_for_status()
                    snapshots[route] = response.json()["data"]
                return snapshots

            original_navigation = await navigation()

            async def search(payload: dict, request_id: str) -> list[dict]:
                async with client.stream("POST", "/api/gateway/search", json=payload, headers={"X-Request-ID": request_id}) as response:
                    assert response.status_code == 200, (await response.aread()).decode()
                    assert response.headers["X-Request-ID"] == request_id
                    events = []
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line.removeprefix("data: "))
                            assert event["request_id"] == request_id
                            events.append(event)
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
                assert response.num_bytes_downloaded <= MAX_SEARCH_OUTPUT_BYTES
                observations.append({"request": payload, "events": events, "output_bytes": response.num_bytes_downloaded})
                return events

            query = {"category": "catalog", "query": "ShSe", "limit": 1}
            first = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [query]}, "req_federation_a")
            assert first[0]["scope"]["scope"]["owners"] == [owners[route_a]]
            a_page, = [event for event in first if event["type"] == "page"]
            assert a_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[route_a][0]

            both = await search({"scope": {"mode": "selected", "owners": [owners[route_a], owners["route_b"]]}, "queries": [query]}, "req_federation_ab")
            pages = [event for event in both if event["type"] == "page"]
            assert len(pages) == 2
            assert {event["page"]["workspace_id"] for event in pages} == {owners[route_a]["workspace_id"], owners["route_b"]["workspace_id"]}
            assert {event["page"]["catalog"]["items"][0]["node"]["session_id"] for event in pages} == {
                sessions[route_a][0], sessions["route_b"][0],
            }
            b_page = next(event for event in pages if event["page"]["workspace_id"] == owners["route_b"]["workspace_id"])
            frozen = both[0]["scope"]["scope"]

            for request_id, change, code in (
                ("req_federation_unsupported_mode", {"match_mode": "literal"}, "unsupported_option"),
                ("req_federation_unsupported_version", {"contract_version": 2}, "unsupported_version"),
            ):
                unsupported = await search({"scope": {"mode": "selected", "owners": frozen["owners"]},
                                            "queries": [{**query, **change}]}, request_id)
                summary = unsupported[-1]["summary"]
                assert summary["state"] == "incomplete" and summary["returned_results"] == 0
                assert len(summary["branches"]) == 2
                for failure in summary["branches"]:
                    assert failure["state"] == "failed" and failure["code"] == code and failure["http_status"] == 400
                    assert failure["cursor"] is None and failure["returned_results"] == 0
                assert not any(event["type"] == "page" for event in unsupported)
            current_navigation = await navigation()
            assert current_navigation == original_navigation
            observations.append({"navigation_before": original_navigation, "navigation_after": current_navigation})

            for suffix in ("search", "children", "roots"):
                response = await client.get(f"/api/v1/session-catalog/{suffix}",
                    params={"query": "ShSe", "cursor": "MTIz"}, headers={"X-BoxTeam-Workspace-Id": route_a})
                assert response.status_code == 409, response.text
                detail = response.json()["detail"]
                assert (detail["owner"], detail["code"], detail["reason"]) == (
                    "session_catalog", "invalid_catalog_cursor", "payload")
            for request_id, invalid_cursor, reason in (
                ("req_federation_catalog_format", "%%%", "format"),
                ("req_federation_catalog_scope", a_page["branch"]["cursor"], "scope"),
            ):
                invalid = await search({"scope": {"mode": "resume", "scope": frozen},
                    "queries": [{**query, "query": "Shared"}],
                    "continuations": [{"branch": a_page["branch"]["branch"], "cursor": invalid_cursor}]}, request_id)
                rejected, = invalid[-1]["summary"]["branches"]
                assert (rejected["code"], rejected["reason"], rejected["error_owner"], rejected["http_status"]) == (
                    "invalid_catalog_cursor", reason, "session_catalog", 409)
                assert rejected["cursor"] == invalid_cursor and rejected["returned_results"] == 0 and rejected["retryable"]
                assert not any(event["type"] == "page" for event in invalid)

            roots = {}
            for route in (route_a, "route_b"):
                response = await client.get("/api/v1/workspace/files/roots", headers={"X-BoxTeam-Workspace-Id": route})
                response.raise_for_status()
                roots[route] = response.json()["data"]["workspace_uri"]
            file_targets = [
                {"owner": owners[route], "target": {"uri": roots[route] + "/" + directory,
                    "search_options": {"include_globs": [pattern]}}}
                for route, directory, pattern in ((route_a, "search-one", "*.txt"), (route_a, "search-two", "*.md"), ("route_b", "search-one", "*.txt"))
            ]
            file_query = {"category": "file", "operation": "name", "query": "needle", "match_mode": "fuzzy", "limit": 1,
                          "file_targets": file_targets}
            file_first = await search({"scope": {"mode": "selected", "owners": [owners[route_a], owners["route_b"]]},
                                       "queries": [file_query]}, "req_federation_files")
            file_pages = [event for event in file_first if event["type"] == "page"]
            assert len(file_pages) == 3 and file_first[-1]["summary"]["returned_results"] == 3
            assert [item["branch"]["resource_uri"] for item in file_first[-1]["summary"]["branches"]] == [item["target"]["uri"] for item in file_targets]
            for event in file_pages:
                page = event["page"]["files"]
                assert event["branch"]["returned_results"] == 1
                assert len(page["entries"]) == 1 and page["entries"][0]["resource_shape"] == "file"
                assert page["node"]["uri"] == event["branch"]["branch"]["resource_uri"]
            markdown = next(event for event in file_pages if event["branch"]["branch"]["resource_uri"].endswith("/search-two"))
            assert markdown["page"]["files"]["entries"][0]["uri"].endswith("/needle-c.md")
            file_resume = next(event for event in file_pages if event["branch"]["branch"]["owner"] == owners["route_b"])
            assert file_resume["branch"]["cursor"]

            shared_content = await search({"scope": {"mode": "selected", "owners": frozen["owners"]}, "queries": [{
                "category": "file", "operation": "content", "query": "needle", "match_mode": "literal", "limit": 1,
                "file_targets": [{"owner": owners[route], "target": {"uri": roots[route] + "/search-one",
                    "search_options": {"include_globs": ["*.txt"],
                        "excluded_resource_uris": [roots[route] + "/search-one/covered.txt"]}}} for route in (route_a, "route_b")],
            }]}, "req_federation_same_content")
            shared_pages = [event["page"]["files"] for event in shared_content if event["type"] == "page"]
            assert len(shared_pages) == 2 and shared_content[-1]["summary"]["returned_results"] == 2
            shared_hits = [page["matches"][0] for page in shared_pages]
            assert len({hit["uri"] for hit in shared_hits}) == 2
            assert {hit["line"] for hit in shared_hits} == {1}
            assert {hit["text"] for hit in shared_hits} == {"🧪 needle"}

            # 同一文件的两处正文分别计量；上下文不消耗配额，覆盖的非法 UTF-8 正文不读取。
            content_root = roots[route_a] + "/search-one"
            content_query = {"category": "file", "operation": "content", "query": "needle", "match_mode": "literal", "limit": 1,
                "file_targets": [{"owner": owners[route_a], "target": {"uri": content_root,
                    "search_options": {"include_globs": ["*.txt"], "all_matches": True, "context_lines": 1,
                                       "excluded_resource_uris": [content_root + "/covered.txt", content_root + "/needle-a.txt"]}}}]}
            content = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [content_query]}, "req_federation_file_content")
            content_page, = [event for event in content if event["type"] == "page"]
            assert content_page["branch"]["returned_results"] == 1 and content[-1]["summary"]["returned_results"] == 1
            hit, = content_page["page"]["files"]["matches"]
            assert hit["uri"] == content_root + "/needle-b.txt" and hit["utf16_column"] == 4
            assert content_page["page"]["files"]["contexts"]
            following = await search({"scope": {"mode": "resume", "scope": content[0]["scope"]["scope"]}, "queries": [content_query],
                "continuations": [{"branch": content_page["branch"]["branch"], "cursor": content_page["branch"]["cursor"]}]}, "req_federation_file_content_next")
            next_hit, = next(event for event in following if event["type"] == "page")["page"]["files"]["matches"]
            assert next_hit["uri"] == hit["uri"] and next_hit["line"] == 3
            invalid_query = {**content_query, "file_targets": [{"owner": owners[route_a], "target": {"uri": content_root,
                "search_options": {"include_globs": ["*.txt"]}}}]}
            rejected = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [invalid_query]}, "req_federation_file_encoding")
            rejected_branch, = rejected[-1]["summary"]["branches"]
            assert rejected_branch["state"] == "failed" and rejected_branch["code"] == "invalid_encoding"
            assert rejected_branch["error_owner"] == "resource_file_operations" and rejected_branch["reason"] == "utf8"
            assert rejected_branch["http_status"] == 400
            assert not any(event["type"] == "page" for event in rejected)
            for request_id, query_error, cursor in (
                ("req_federation_invalid_regex", {**content_query, "query": "[", "match_mode": "regex", "file_targets": [
                    {"owner": owners[route_a], "target": {"uri": content_root + "/empty", "search_options": {"is_regex": True}}},
                ]}, None),
                ("req_federation_invalid_cursor", content_query, "not-a-cursor"),
            ):
                payload: dict[str, object] = {"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [query_error]}
                if cursor is not None:
                    payload["scope"] = {"mode": "resume", "scope": content[0]["scope"]["scope"]}
                    payload["continuations"] = [{"branch": content_page["branch"]["branch"], "cursor": cursor}]
                invalid = await search(payload, request_id)
                failure, = invalid[-1]["summary"]["branches"]
                assert failure["state"] == "failed" and failure["code"] == "execution_failed"
                assert failure["error_owner"] == "resource_file_operations" and failure["http_status"] == 400
                assert failure["reason"] == ("invalid_cursor" if cursor else "invalid_search_pattern")
                assert failure["cursor"] == cursor and failure["returned_results"] == 0
                assert not any(event["type"] == "page" for event in invalid)
            missing_query = {**file_query, "file_targets": [{"owner": owners[route_a], "target": {"uri": roots[route_a] + "/missing-directory"}}]}
            missing = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [missing_query]}, "req_federation_file_missing")
            missing_branch, = missing[-1]["summary"]["branches"]
            assert missing_branch["state"] == "failed" and missing_branch["code"] == "unknown_resource"

            await asyncio.to_thread(close_gateway_process, gateway)
            gateway = None
            gateway = await asyncio.to_thread(network.start)
            file_continued = await search({"scope": {"mode": "resume", "scope": file_first[0]["scope"]["scope"]}, "queries": [file_query],
                "continuations": [{"branch": file_resume["branch"]["branch"], "cursor": file_resume["branch"]["cursor"]}]}, "req_federation_files_restart")
            file_next, = [event for event in file_continued if event["type"] == "page"]
            assert file_next["page"]["files"]["entries"][0]["uri"] == roots["route_b"] + "/search-one/needle-b.txt"
            assert len(file_continued[-1]["summary"]["branches"]) == 1
            content_continuation = {"scope": {"mode": "resume", "scope": content[0]["scope"]["scope"]}, "queries": [content_query],
                "continuations": [{"branch": content_page["branch"]["branch"], "cursor": content_page["branch"]["cursor"]}]}
            content_restarted = await search(content_continuation, "req_federation_content_restart")
            restored_hit, = next(event for event in content_restarted if event["type"] == "page")["page"]["files"]["matches"]
            assert restored_hit == next_hit
            # 通过原保存入口更新已消费的同一文件，旧的文件内续点必须明确失效。
            selection = {"X-BoxTeam-Workspace-Id": route_a}
            current = await client.get("/api/v1/workspace/files/content", headers=selection, params={"path": hit["uri"]})
            current.raise_for_status()
            original = current.json()["data"]
            saved = await client.put("/api/v1/workspace/files/content", headers=selection, params={"path": hit["uri"]},
                json={"content": original["content"] + "updated", "expected_revision": original["revision"]})
            saved.raise_for_status()
            stale_content = await search(content_continuation, "req_federation_content_stale")
            stale_branch, = stale_content[-1]["summary"]["branches"]
            assert (stale_branch["state"], stale_branch["error_owner"], stale_branch["reason"], stale_branch["http_status"]) == (
                "failed", "resource_file_operations", "stale_cursor", 400)
            assert stale_branch["cursor"] == content_page["branch"]["cursor"] and stale_branch["returned_results"] == 0
            assert not any(event["type"] == "page" for event in stale_content)
            resumed = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                    "continuations": [{"branch": b_page["branch"]["branch"], "cursor": b_page["branch"]["cursor"]}]}, "req_federation_resume")
            resumed_page, = [event for event in resumed if event["type"] == "page"]
            assert resumed[0]["scope"]["scope"] == frozen
            assert resumed_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions["route_b"][1]
            assert len(resumed[-1]["summary"]["branches"]) == 1

            # 通过产品修改 B 的目录版本，A 的旧页仍可继续；不替换 owner 响应制造冲突。
            client.headers["X-BoxTeam-Workspace-Id"] = "route_b"
            await create_session(client, "Revision bump")
            invalidated = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                        "continuations": [{"branch": item["branch"]["branch"], "cursor": item["branch"]["cursor"]} for item in pages]}, "req_federation_conflict")
            a_continued, = [event for event in invalidated if event["type"] == "page"]
            assert a_continued["branch"]["branch"]["owner"] == owners[route_a]
            assert a_continued["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[route_a][1]
            failed = next(item for item in invalidated[-1]["summary"]["branches"] if item["branch"]["owner"] == owners["route_b"])
            assert failed["state"] == "failed" and failed["code"] == "stale_catalog_cursor"
            assert failed["error_owner"] == "session_catalog" and failed["http_status"] == 409
            assert failed["reason"] == "revision" and failed["retryable"]
            assert failed["cursor"] == b_page["branch"]["cursor"]
            retried = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                    "continuations": [{"branch": b_page["branch"]["branch"]}]}, "req_federation_retry")
            retry_page, = [event for event in retried if event["type"] == "page"]
            assert retry_page["branch"]["branch"]["owner"] == owners["route_b"]
            assert retry_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions["route_b"][0]
            assert len(retried[-1]["summary"]["branches"]) == 1

            full = await search({"scope": {"mode": "all"}, "queries": [{**query, "limit": 50}]}, "req_federation_all")
            assert full[-1]["summary"]["state"] == "incomplete"
            assert len(full[-1]["summary"]["branches"]) == 3
            stopped = next(item for item in full[-1]["summary"]["branches"] if item["branch"]["owner"] == owners["route_c"])
            assert stopped["code"] == "target_unavailable" and stopped["state"] == "failed"
            assert sum(len(event["page"]["catalog"]["items"]) for event in full if event["type"] == "page") == 6
            assert full[-1]["summary"]["returned_results"] == 6
            assert not (network.stopped / ".boxteam/sessions").exists()

            # 产品入口发布真实 child，使名称续点停留在同一 Session 的 Thread 版本内。
            session_a = sessions[route_a][0]
            selection = {"X-BoxTeam-Workspace-Id": route_a}
            point_response = await client.get(f"/api/v1/sessions/{session_a}/fork-source-point", headers=selection,
                                              params={"mode": "full_rollout_copy"})
            assert point_response.status_code == 200, point_response.text
            point = point_response.json()["data"]

            async def publish_child(key: str) -> dict:
                response = await client.post(f"/api/v1/sessions/{session_a}/fork-context", headers=selection, json={
                    "idempotency_key": key, "source_uri": point["source_uri"], "source_view_id": point["source_view_id"],
                    "source_view_revision": point["source_view_revision"], "mode": "full_rollout_copy", "anchor_mode": "inclusive",
                    "target_container_uri": point["child_container_uri"], "creation_kind": "new_child_thread",
                    "title": "Shared Search child", "relationship": "detached", "resources": [],
                })
                assert response.status_code == 200, response.text
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                return response.json()["data"]

            await publish_child("search-thread-first-child")
            thread_query = {"category": "thread", "query": "ShSe", "limit": 1}
            thread_first = await search({"scope": {"mode": "selected", "owners": frozen["owners"]},
                                         "queries": [thread_query]}, "req_federation_thread_first")
            thread_pages = [event for event in thread_first if event["type"] == "page"]
            thread_a = next(page for page in thread_pages if page["branch"]["branch"]["owner"] == owners[route_a])
            for cursor, changed_query, reason in (("MTIz", "ShSe", "malformed"),
                                                  (thread_a["branch"]["cursor"], "Shared", "query_mismatch")):
                direct = await client.get("/api/v1/session-threads/search", headers=selection,
                                          params={"query": changed_query, "cursor": cursor})
                assert direct.status_code == 409, direct.text
                assert direct.headers["X-Request-ID"] == direct.json()["request_id"]
                assert (direct.json()["detail"]["code"], direct.json()["detail"]["owner"], direct.json()["detail"]["reason"]) == (
                    "invalid_thread_cursor", "session_thread_search", reason)
                rejected_events = await search({"scope": {"mode": "resume", "scope": thread_first[0]["scope"]["scope"]},
                    "queries": [{**thread_query, "query": changed_query}],
                    "continuations": [{"branch": thread_a["branch"]["branch"], "cursor": cursor}]}, "req_thread_" + reason)
                rejection, = rejected_events[-1]["summary"]["branches"]
                assert (rejection["code"], rejection["error_owner"], rejection["reason"], rejection["http_status"]) == (
                    "invalid_thread_cursor", "session_thread_search", reason, 409)
                assert rejection["cursor"] == cursor and rejection["returned_results"] == 0 and rejection["retryable"]
                assert not any(event["type"] == "page" for event in rejected_events)
            await publish_child("search-thread-second-child")
            thread_stale = await search({"scope": {"mode": "resume", "scope": thread_first[0]["scope"]["scope"]},
                "queries": [thread_query], "continuations": [{"branch": page["branch"]["branch"], "cursor": page["branch"]["cursor"]}
                                                           for page in thread_pages]}, "req_federation_thread_changed")
            failed_thread, = [branch for branch in thread_stale[-1]["summary"]["branches"] if branch["state"] == "failed"]
            assert failed_thread["branch"]["owner"] == owners[route_a]
            assert (failed_thread["code"], failed_thread["error_owner"], failed_thread["reason"], failed_thread["http_status"]) == (
                "stale_thread_catalog", "session_thread_catalog", "thread_revision", 409)
            assert failed_thread["cursor"] == thread_a["branch"]["cursor"] and failed_thread["returned_results"] == 0 and failed_thread["retryable"]
            assert [page["branch"]["branch"]["owner"] for page in thread_stale if page["type"] == "page"] == [owners["route_b"]]
            thread_retry = await search({"scope": {"mode": "resume", "scope": thread_first[0]["scope"]["scope"]},
                "queries": [thread_query], "continuations": [{"branch": failed_thread["branch"]}]}, "req_federation_thread_retry")
            assert len(thread_retry[-1]["summary"]["branches"]) == 1
            assert all(branch["state"] != "failed" for branch in thread_retry[-1]["summary"]["branches"])

            # 只改变产品已创建 Session 的目录权限，不伪造日志或搜索回执。
            sessions_root = network.workspace / ".boxteam/sessions"
            with closing(SessionCatalogStore(network.workspace / ".boxteam/navigation/session-catalog.sqlite", sessions_root)) as catalog:
                node = catalog.get_node(sessions[route_a][0])
                assert node.storage_relative_locator is not None
                directory = catalog.resolve_session_locator(node.storage_relative_locator) / "logs/llm_requests"
            directory.mkdir(parents=True, exist_ok=True)
            mode = directory.stat().st_mode & 0o7777
            log_query = {"category": "llm_request", "operation": "content", "match_mode": "literal", "query": "query-only"}
            directory.chmod(0)
            try:
                response = await client.post("/api/v1/search", json={"workspace_id": owners[route_a]["workspace_id"], **log_query},
                    headers={"X-BoxTeam-Workspace-Id": route_a, "X-Request-ID": "req_provider_native_io"})
                assert response.status_code == 500, response.text
                assert response.headers["X-Request-ID"] == "req_provider_native_io"
                detail = response.json()["detail"]
                assert detail["owner"] == "search_provider" and detail["provider_id"] == "llm-request-log"
                assert detail["code"] == "provider_read_failed" and str(directory) in detail["message"]
                failed_logs = await search({"scope": {"mode": "selected", "owners": [owners[route_a], owners["route_b"]]},
                                           "queries": [log_query]}, "req_federation_native_io")
                failed = next(branch for branch in failed_logs[-1]["summary"]["branches"] if branch["branch"]["owner"] == owners[route_a])
                assert failed["state"] == "failed" and failed["code"] == "provider_read_failed"
                assert failed["provider_id"] == "llm-request-log" and str(directory) in failed["message"]
                assert failed["error_owner"] == "search_provider" and failed["http_status"] == 500
                assert failed["cursor"] is None and failed["returned_results"] == 0 and failed["retryable"]
                assert [page["branch"]["branch"]["owner"] for page in failed_logs if page["type"] == "page"] == [owners["route_b"]]
            finally:
                directory.chmod(mode)
            repaired = await search({"scope": {"mode": "resume", "scope": failed_logs[0]["scope"]["scope"]},
                "queries": [log_query], "continuations": [{"branch": failed["branch"], "cursor": failed["cursor"]}]}, "req_federation_native_io_retry")
            assert repaired[-1]["summary"]["state"] == "complete"
            assert [branch["branch"]["owner"] for branch in repaired[-1]["summary"]["branches"]] == [owners[route_a]]

            # 关闭本装具自己的 B 后端，保留原登记 URL，验证真实连接拒绝而非离线状态替身。
            await asyncio.to_thread(close_backend_process, network.backends[1])
            disconnected = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                "continuations": [{"branch": page["branch"]["branch"], "cursor": page["branch"]["cursor"]
                                  if page is b_page else None} for page in pages]}, "req_federation_connection_refused")
            failure, = [branch for branch in disconnected[-1]["summary"]["branches"] if branch["state"] == "failed"]
            assert failure["branch"]["owner"] == owners["route_b"]
            assert failure["code"] == "target_unavailable" and failure["error_owner"] == "search_transport"
            assert failure["reason"] == "ConnectError" and failure["http_status"] is None and failure["retryable"]
            assert failure["cursor"] == b_page["branch"]["cursor"] and failure["returned_results"] == 0
            assert [page["branch"]["branch"]["owner"] for page in disconnected if page["type"] == "page"] == [owners[route_a]]
            assert [event["branch"] for event in disconnected if event["type"] == "branch" and event["branch"]["state"] == "failed"] == [failure]

            await asyncio.to_thread(close_backend_process, network.backends[0])
            unavailable = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                "continuations": [{"branch": page["branch"]["branch"], "cursor": page["branch"]["cursor"]} for page in pages]},
                "req_federation_all_connections_refused")
            summary = unavailable[-1]["summary"]
            assert summary["state"] == "incomplete" and summary["returned_results"] == 0
            assert len(summary["branches"]) == 2 and not any(event["type"] == "page" for event in unavailable)
            for branch in summary["branches"]:
                page = next(page for page in pages if page["branch"]["branch"] == branch["branch"])
                assert branch["state"] == "failed" and branch["code"] == "target_unavailable"
                assert branch["reason"] == "ConnectError" and branch["error_owner"] == "search_transport"
                assert branch["http_status"] is None and branch["retryable"]
                assert branch["returned_results"] == 0 and branch["cursor"] == page["branch"]["cursor"]
            assert [event["branch"] for event in unavailable if event["type"] == "branch" and event["branch"]["state"] == "failed"] == summary["branches"]
    finally:
        (network.artifacts / "federation-network.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
