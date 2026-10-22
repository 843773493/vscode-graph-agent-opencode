"""真实 Gateway、受管 Workspace 与文件 owner 的只读导航闭包。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import prepare_default_test_workspace


def _tracking_counts(root: Path) -> dict[str, int]:
    """核真实持久化来源/activation表，而不是仅检查响应没有身份字段。"""
    counts: dict[str, int] = {}
    for database in root.rglob("*.sqlite"):
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table,) in tables:
                if table.startswith("resource_activation") or table == "context_source_control_states":
                    # 表名来自sqlite自身schema，并非用户输入。
                    counts[f"{database.relative_to(root)}:{table}"] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return counts


@pytest.fixture
def navigation_files(integration_workspace_root_path: str):
    workspace = Path(integration_workspace_root_path)
    artifacts = workspace.parent / "artifacts"
    artifacts.mkdir(exist_ok=True)
    home = workspace.parent / "boxteam-home"
    for root in (workspace / ".boxteam/skills/demo", home / "skills/demo"):
        (root / "references/deep").mkdir(parents=True, exist_ok=True)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text("---\nname: demo\ndescription: 导航验证\n---\n正文\n")
        (root / "references/deep/中文 文档.v2.md").write_text("多层正文 Ω\n")
        (root / "scripts/run.py").write_text("print('导航')\n")
        (root / "%2e%2e.txt").write_text("字面百分号\n")
    (workspace / "src/中文 文件.v2.md").write_text("工作区源码 Ω\n")
    (workspace / "AGENTS.md").write_text("工作区规范\n")
    untracked = workspace / "src/navigation-untracked"
    untracked.mkdir()
    for index in range(257):
        (untracked / f"bulk-needle-{index:03}.txt").write_text("未追踪源码\n")
    (workspace / ".github").mkdir(exist_ok=True)
    (workspace / ".github/100%.txt").write_text("点号目录可读\n")
    outside = workspace.parent / "outside-secret.txt"
    outside.write_text("不能读取的越界正文\n")
    try:
        yield workspace, home, artifacts, outside
    finally:
        outside.unlink(missing_ok=True)


async def test_real_workspace_gateway_navigation_is_fixed_lightweight_and_contained(
    request: pytest.FixtureRequest, integration_workspace_config_path: str, navigation_files,
) -> None:
    assert Path(integration_workspace_config_path).is_file()
    workspace, home, artifacts, outside = navigation_files
    port = integration_port_block_for_file(Path(request.node.path)).port(11)
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=home, artifacts_dir=artifacts / "gateway",
    )
    receipts: list[dict[str, object]] = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=60, trust_env=False,
        ) as client:
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            gateway_id = health.json()["data"]["gateway_id"]
            workspace_info = await client.get("/api/v1/workspace")
            assert workspace_info.status_code == 200, workspace_info.text
            workspace_id = json.loads((workspace / ".boxteam/workspace-identity.json").read_text())["workspace_id"]
            path = f"/api/v1/workspaces/{workspace_id}/resources/navigation"
            raw_path = f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}"
            gateway_path = raw_path + "/navigation"
            before = _tracking_counts(workspace / ".boxteam")
            publication = (await client.get(raw_path)).json()["data"]
            for headers in ({}, {"X-Local-Token": "wrong"}):
                denied = await client.post(gateway_path, headers={"X-Local-Token": headers.get("X-Local-Token", "")}, json={
                    "uri": resource_display_uri(scope="gateway", scope_id=gateway_id, kind="skills", tail_segments=("demo",)),
                    "operation": "list",
                })
                assert denied.status_code == 401, denied.text
            denied = await client.post(gateway_path.replace(workspace_id, str(uuid4())), json={
                "uri": resource_display_uri(scope="gateway", scope_id=gateway_id, kind="skills", tail_segments=("demo",)),
                "operation": "list",
            })
            assert denied.status_code == 403 and "audience" in denied.text

            async def navigate(uri: str, operation: str, **extra: object) -> dict:
                response = await client.post(path, json={"uri": uri, "operation": operation, **extra})
                receipts.append({"uri": uri, "operation": operation, "status": response.status_code, "body": response.json()})
                assert response.status_code == 200, response.text
                assert len(response.content) <= extra.get("max_response_bytes", 1048576)
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                for forbidden in (str(workspace), str(home), "local-dev-token"):
                    assert forbidden not in response.text
                assert set(response.json()["data"]) == {"node", "entries", "content", "truncated", "next_cursor", "truncation_reason", "view",
                                                        "content_encoding", "media_type", "mime_type", "line_offset", "matches", "content_hash"}
                assert response.json()["data"]["view"] == "live"
                assert response.json()["data"]["node"]["uri"] == parse_resource_uri(uri, local_gateway_id=gateway_id).display_uri
                for node in (response.json()["data"]["node"], *response.json()["data"]["entries"]):
                    assert set(node) == {"uri", "resource_shape", "operations", "child_path"}
                return response.json()["data"]

            files_root = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",))
            root = await navigate(files_root, "describe")
            assert root["node"] == {"uri": files_root, "resource_shape": "directory", "operations": ["describe", "list", "search"], "child_path": "file_segments"}
            tree = await navigate(files_root, "list")
            assert all(not node["uri"].endswith("/.boxteam") for node in tree["entries"])
            dot = next(node["uri"] for node in tree["entries"] if node["uri"].endswith("/.github"))
            dot_children = await navigate(dot, "list")
            assert (await navigate(dot_children["entries"][0]["uri"], "read"))["content"] == "点号目录可读\n"
            found = await navigate(files_root, "search", query="中文 文件")
            assert len(found["entries"]) == 1
            assert (await navigate(found["entries"][0]["uri"], "read"))["content"] == "工作区源码 Ω\n"
            bounded = await navigate(found["entries"][0]["uri"], "read", max_bytes=4)
            assert bounded["content"] == "工" and bounded["truncated"]
            assert bounded["content_hash"] is None
            full = await navigate(found["entries"][0]["uri"], "read")
            assert full["content_hash"] == "sha256:" + hashlib.sha256((workspace / "src/中文 文件.v2.md").read_bytes()).hexdigest()
            file_uri = found["entries"][0]["uri"]
            preview = await client.get("/api/v1/workspace/files/content", params={"path": file_uri})
            assert preview.status_code == 200, preview.text
            original_revision = preview.json()["data"]["revision"]
            assert "sha256:" + original_revision == full["content_hash"]
            saved = await client.put("/api/v1/workspace/files/content", params={"path": file_uri}, json={
                "content": "HTTP保存的正文 Ω\r\n", "expected_revision": original_revision,
            })
            assert saved.status_code == 200, saved.text
            saved_raw = "HTTP保存的正文 Ω\r\n".encode()
            assert (workspace / "src/中文 文件.v2.md").read_bytes() == saved_raw
            assert saved.json()["data"]["content"] == saved_raw.decode()
            assert saved.json()["data"]["revision"] == hashlib.sha256(saved_raw).hexdigest()
            stale = await client.put("/api/v1/workspace/files/content", params={"path": file_uri}, json={
                "content": "过时草稿不得覆盖", "expected_revision": original_revision,
            })
            assert stale.status_code == 409, stale.text
            for response in (preview, saved, stale):
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                assert str(workspace) not in response.text
            assert (await navigate(file_uri, "read"))["content_hash"] == "sha256:" + hashlib.sha256(saved_raw).hexdigest()
            assert (await navigate(files_root, "list", limit=1))["truncated"]
            bulk = await navigate(files_root, "search", query="bulk-needle", limit=7)
            assert len(bulk["entries"]) == 7 and bulk["truncated"]
            assert all(node["resource_shape"] == "file" for node in bulk["entries"])
            assert (await navigate(bulk["entries"][0]["uri"], "read"))["content"] == "未追踪源码\n"
            budget_root = files_root + "/src/navigation-untracked"
            budget_page = await navigate(budget_root, "list", max_response_bytes=1400)
            assert budget_page["truncated"] and budget_page["truncation_reason"] == "response_byte_limit"
            budget_next = await navigate(budget_root, "list", max_response_bytes=1400, cursor=budget_page["next_cursor"])
            assert {entry["uri"] for entry in budget_page["entries"]}.isdisjoint(entry["uri"] for entry in budget_next["entries"])
            cursor, seen = None, []
            while True:
                page = await navigate(files_root + "/src/navigation-untracked", "search", query="bulk-needle", limit=60, cursor=cursor)
                assert len(page["entries"]) <= 60
                seen.extend(entry["uri"] for entry in page["entries"])
                if not page["truncated"]:
                    assert page["next_cursor"] is None
                    break
                assert page["truncation_reason"] == "entry_limit" and page["next_cursor"]
                cursor = page["next_cursor"]
            assert len(seen) == len(set(seen)) == 257
            rejected = await client.post(path, json={"uri": files_root, "operation": "search", "query": "bulk-needle", "cursor": cursor or bulk["next_cursor"]})
            assert rejected.status_code == 400
            assert rejected.json()["detail"]["owner"] == "resource_file_operations"
            limited = await client.post(path, json={"uri": files_root, "operation": "list", "scan_limit": 1})
            assert limited.status_code == 400
            assert limited.json()["detail"]["code"] == "output_limit_exceeded"
            assert limited.json()["detail"]["reason"] == "scan_limit"
            agents_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="agent-spec", tail_segments=("root", "AGENTS.md"))
            agents = await navigate(agents_uri, "read")
            assert agents["content"] == "工作区规范\n"
            assert agents["node"]["operations"] == ["describe", "read"] and agents["node"]["child_path"] is None

            for scope, scope_id in (("workspace", workspace_id), ("gateway", gateway_id), ("inline", "source-development-0_0_2")):
                name = "demo" if scope != "inline" else "coding"
                if scope == "inline":
                    name = next(item.name for item in (Path.cwd() / "resources/skills").iterdir() if (item / "SKILL.md").is_file())
                uri = resource_display_uri(scope=scope, scope_id=scope_id, kind="skills", tail_segments=(name,))
                listed = await navigate(uri, "list")
                entry = next(node for node in listed["entries"] if node["uri"].endswith("/SKILL.md"))
                assert entry["resource_shape"] == "file" and entry["child_path"] is None
                assert (await navigate(entry["uri"], "read"))["content"]
                if scope != "inline":
                    paged = await navigate(uri, "list", limit=1)
                    assert paged["truncated"] and paged["next_cursor"]
                    following = await navigate(uri, "list", limit=1, cursor=paged["next_cursor"])
                    assert following["entries"][0]["uri"] != paged["entries"][0]["uri"]
                    limited = await client.post(path, json={"uri": uri, "operation": "list", "scan_limit": 1})
                    assert limited.status_code == 400, limited.text
                    assert limited.json()["detail"]["owner"] == "resource_file_operations"
                    assert limited.json()["detail"]["reason"] == "scan_limit"
                if scope == "inline":
                    continue
                found = await navigate(uri, "search", query="中文 文档")
                child = found["entries"][0]["uri"]
                assert child.endswith("/references/deep/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.v2.md")
                assert (await navigate(child, "read"))["content"] == "多层正文 Ω\n"
                literal = resource_display_uri(scope=scope, scope_id=scope_id, kind="skills", tail_segments=(name, "%2e%2e.txt"))
                assert (await navigate(literal, "read"))["content"] == "字面百分号\n"
                explicit = resource_display_uri(scope=scope, scope_id=scope_id, kind="skills", tail_segments=(name, "SKILL.md"), gateway_authority=gateway_id)
                assert (await navigate(explicit, "read"))["node"]["uri"] == entry["uri"]

            # 同一入口按URI拒绝目标错位、私有树、非法路径与领域对象目录猜测。
            for uri, operation, code in (
                (files_root.replace(workspace_id, str(uuid4())), "list", "scope_mismatch"),
                (files_root.replace("boxteam://", "boxteam://gateway_other/"), "list", "scope_mismatch"),
                (files_root + "/.boxteam", "list", "capability_denied"),
                (files_root, "read", "capability_denied"),
                (agents_uri, "list", "capability_denied"),
                (agents_uri + "/child", "read", "malformed_path"),
                (files_root + "/src/%2e%2e", "list", "dot_segment"),
                (files_root + "/missing.txt", "read", "unknown_resource"),
                (files_root + "/src/%E4%B8%AD%E6%96%87%20%E6%96%87%E4%BB%B6.v2.md/child", "read", "capability_denied"),
                (files_root + "/src/中文.md", "read", "non_ascii_rejected"),
                (resource_display_uri(scope="workspace", scope_id=workspace_id, kind="session", tail_segments=("sess-test", "checkpoints")), "list", "capability_denied"),
            ):
                response = await client.post(path, json={"uri": uri, "operation": operation})
                assert response.status_code in {400, 403, 404}, response.text
                assert response.json()["detail"]["code"] == code
            # 精确产物经原领域 owner 读取；合法悬空 URI 明确未找到，不变成私有目录能力。
            session_id = "ses_019c3567da377a6f855e1bddac8f1f76"
            for collection, artifact_id in (
                ("attachments", "att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                ("tool-results", "tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt"),
            ):
                uri = resource_display_uri(
                    scope="workspace", scope_id=workspace_id, kind="session",
                    tail_segments=(session_id, collection, artifact_id),
                )
                response = await client.post(path, json={"uri": uri, "operation": "read"})
                body = response.json()
                receipts.append({"uri": uri, "operation": "read", "status": response.status_code, "body": body})
                assert response.status_code == 404, response.text
                assert body["detail"]["owner"] == "resource_uri_resolver"
                assert body["detail"]["code"] == "unknown_resource"
                assert response.headers["X-Request-ID"] == body["request_id"]
            invalid_operation = await client.post(path, json={"uri": files_root, "operation": "write"})
            assert invalid_operation.status_code == 422
            # 激活另一个真实工作区后，原canonical Workspace路由仍命中原根。
            second = workspace / "navigation-second-workspace"
            prepare_default_test_workspace(workspace_root=second, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
            (second / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
            registered = await client.post("/api/gateway/workspaces/local", json={"root_path": str(second), "name": "导航第二工作区"})
            assert registered.status_code == 200, registered.text
            second_id = json.loads((second / ".boxteam/workspace-identity.json").read_text())["workspace_id"]
            target = next(item for item in registered.json()["data"]["items"] if item["backend_workspace_id"] == second_id)
            activated = await client.post(f"/api/gateway/workspaces/{target['workspace_id']}/activate")
            assert activated.status_code == 200, activated.text
            assert activated.json()["data"]["active_workspace_id"] == target["workspace_id"]
            active = await client.get("/api/v1/workspace")
            assert active.status_code == 200 and active.json()["data"]["workspace_id"] == second_id
            source_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "src", "中文 文件.v2.md"))
            assert (await navigate(source_uri, "read"))["content"] == saved_raw.decode()
            unknown = await client.post(path.replace(workspace_id, str(uuid4())), json={"uri": source_uri, "operation": "read"})
            assert unknown.status_code == 404, unknown.text
            for base, uri in (
                (workspace / "src", files_root + "/src/escape"),
                (workspace / ".boxteam/skills/demo/references", resource_display_uri(scope="workspace", scope_id=workspace_id, kind="skills", tail_segments=("demo", "references", "escape"))),
                (home / "skills/demo/references", resource_display_uri(scope="gateway", scope_id=gateway_id, kind="skills", tail_segments=("demo", "references", "escape"))),
            ):
                link = base / "escape"
                link.symlink_to(outside)
                response = await client.post(path, json={"uri": uri, "operation": "read"})
                assert response.status_code == 403, response.text
                assert response.json()["detail"]["code"] == "capability_denied"
                assert str(outside) not in response.text
                link.unlink()
            private_alias = workspace / "private-alias"
            private_alias.symlink_to(workspace / ".boxteam", target_is_directory=True)
            response = await client.post(path, json={"uri": files_root + "/private-alias", "operation": "list"})
            assert response.status_code == 403 and response.json()["detail"]["code"] == "capability_denied"
            private_alias.unlink()
            for base, uri in (
                (workspace / "src", files_root + "/src/loop"),
                (workspace / ".boxteam/skills/demo/references", resource_display_uri(scope="workspace", scope_id=workspace_id, kind="skills", tail_segments=("demo", "references", "loop"))),
                (home / "skills/demo/references", resource_display_uri(scope="gateway", scope_id=gateway_id, kind="skills", tail_segments=("demo", "references", "loop"))),
            ):
                link = base / "loop"
                link.symlink_to(link)
                response = await client.post(path, json={"uri": uri, "operation": "read"})
                assert response.status_code == 403, response.text
                assert response.json()["detail"] == {
                    "owner": "resource_uri_resolver", "code": "capability_denied", "message": "资源路径包含符号链接循环",
                }
                for real_path in (str(workspace), str(home), str(link)):
                    assert real_path not in response.text and real_path not in str(response.headers)
                link.unlink()
            after = _tracking_counts(workspace / ".boxteam")
            assert after == before
            assert (await client.get(raw_path)).json()["data"] == publication
            (artifacts / "navigation-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")
    finally:
        close_gateway_process(gateway)


async def test_real_gateway_navigation_grammar_errors_do_not_echo_native_input(
    request: pytest.FixtureRequest, integration_workspace_config_path: str, navigation_files,
) -> None:
    assert Path(integration_workspace_config_path).is_file()
    workspace, home, artifacts, outside = navigation_files
    original = outside.read_bytes()
    port = integration_port_block_for_file(Path(request.node.path)).port(12)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=home, artifacts_dir=artifacts / "grammar-errors-gateway")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=60, trust_env=False) as client:
            await acquire_gateway_guest(client)
            workspace_id = (await client.get("/api/v1/workspace")).json()["data"]["workspace_id"]
            registered = (await client.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            registry = home / "state/gateway/gateway.sqlite"
            with closing(sqlite3.connect(f"{registry.as_uri()}?mode=ro", uri=True)) as connection:
                registry_before = connection.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()
            endpoint = f"/api/v1/workspaces/{workspace_id}/resources/navigation"
            cases = [
                (str(outside), "unknown_scheme"), (outside.as_uri(), "unknown_scheme"),
                (r"C:\native-uri-marker\secret", "backslash_rejected"),
                (r"\\native-uri-marker\share\secret", "backslash_rejected"),
                ("file:///private/native-uri-marker/secret", "unknown_scheme"),
                ("boxteam://workspace/root/resources/files/root/x?path=/native-uri-marker", "query_rejected"),
                ("boxteam://workspace/root/resources/files/root/native-uri-marker%2Fsecret", "invalid_character"),
            ]
            receipts = []
            for uri, code in cases:
                response = await client.post(endpoint, json={"uri": uri, "operation": "read"})
                receipts.append({"status": response.status_code, "body": response.json()})
                assert response.status_code == 400, response.text
                body = response.json()
                assert body["request_id"] == response.headers["X-Request-ID"]
                assert body["detail"]["owner"] == "resource_uri_grammar" and body["detail"]["code"] == code
                assert body["detail"]["message"]
                assert all(value not in response.text for value in (str(workspace), str(home), str(outside), "native-uri-marker"))
            after = (await client.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            # checked_at 是每次列表投影生成的观察时间，不是注册表状态。
            for before_item, after_item in zip(registered["items"], after["items"], strict=True):
                assert datetime.fromisoformat(after_item.pop("checked_at")) >= datetime.fromisoformat(before_item.pop("checked_at"))
            assert after == registered
            with closing(sqlite3.connect(f"{registry.as_uri()}?mode=ro", uri=True)) as connection:
                assert connection.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall() == registry_before
            assert outside.read_bytes() == original
            file_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "src", "中文 文件.v2.md"))
            success = await client.post(endpoint, json={"uri": file_uri, "operation": "read"})
            assert success.status_code == 200 and success.json()["data"]["content"] == "工作区源码 Ω\n"
            (artifacts / "grammar-error-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")
    finally:
        close_gateway_process(gateway)
