"""完整 Web 页面经真实 Gateway 保留未注册目录的浏览、编辑与快捷入口。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import workspace_identity_path
from app.services.infrastructure.workspace_files.paths import (
    file_uri_child,
    file_uri_root,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - fixture 以当前测试路径隔离
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - 独立 Gateway 与真实 SSH 路由
)
from tests.support.api_helpers import create_session
from tests.support.gateway_processes import (
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def local_model_metadata(monkeypatch):
    # 本场景不使用外部模型；避免 LiteLLM 首次导入联网更新价格表阻塞页面初始化。
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")


async def _browse(case, fixture: dict, artifacts: Path) -> dict:
    artifacts.mkdir(parents=True)
    result_path = artifacts / "native-directory-browser.json"
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert executable, "未注册目录 Web 验证需要 Chromium"
    script = "gateway_directory_selection.mjs" if fixture.get("directory_selection") else "native_directory_browser.mjs"
    process = await asyncio.to_thread(subprocess.run, ["bun",
        "tests/integration/clients/web/workspace_files/" + script],
        env={**os.environ, "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url),
             "BOXTEAM_BROWSER_FIXTURE": json.dumps({**fixture, "cookies": [
                 {"name": cookie.name, "value": cookie.value, "url": str(case.client.base_url)}
                 for cookie in case.client.cookies.jar]}, ensure_ascii=False),
             "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
             "BOXTEAM_BROWSER_RUNTIME_PATH": str(case.workspace.parent / "runtime/browser"),
             "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable},
        cwd=Path.cwd(), capture_output=True, text=True, timeout=240, check=False)
    (artifacts / "playwright.log").write_text(process.stdout + process.stderr)
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(result_path.read_text())


def _registry(workspace: Path) -> list[tuple]:
    database = workspace.parent / "boxteam-home/state/gateway/gateway.sqlite"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT workspace_id, position, active, payload_json FROM gateway_workspace_registry ORDER BY workspace_id",
        ).fetchall()


def _workspace_state(response) -> dict:
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # checked_at 是每次健康检查的采样时间，不是注册表事实；其它 DTO 字段完整比较。
    return {**data, "items": [{key: value for key, value in item.items() if key != "checked_at"}
                            for item in data["items"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"gateway_environment": {
    "BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist"),
}}], indirect=True)
async def test_unregistered_directory_browser_edit_and_shortcut_reopen(communication_case):
    case = communication_case
    user_id = "file-preference-user"
    created = await case.client.post("/api/gateway/users", json={"user_id": user_id, "display_name": "文件偏好验收"})
    assert created.status_code == 200, created.text
    access = await case.client.post(f"/api/gateway/users/{user_id}/access", json={"client_label": "文件偏好验收"})
    assert access.status_code == 200, access.text
    external = case.workspace.parent / "未注册 目录%"
    external.mkdir(exist_ok=True)
    original, edited = "原生目录正文 Ω\n", "浏览器实际编辑 Ω\n"
    file = external / "原生 文件%.txt"
    file.write_text(original, encoding="utf-8")
    title = "未注册目录浏览与编辑"
    session_id = await create_session(case.client, title)
    before = await case.client.get("/api/gateway/workspaces")
    initial_state = _workspace_state(before)
    initial_registry = _registry(case.workspace)
    fixture = {"session_id": session_id, "session_title": title, "directory": str(external),
               "file_name": file.name, "original": original, "edited": edited, "persist_files": True}
    artifacts = case.artifacts / f"run-{os.getpid()}"
    result = await _browse(case, fixture, artifacts / "edit-and-reload")
    uri = result["shortcut_uri"]
    parsed = parse_resource_uri(uri)
    assert parsed.scope == "gateway" and parsed.kind == "files"
    assert file.read_text(encoding="utf-8") == edited
    assert result["reopened_content"] == edited
    assert result["page_errors"] == []
    assert not (external / ".boxteam").exists()
    after = await case.client.get("/api/gateway/workspaces")
    case.record("workspace-list-before-and-after", {"before": before.json(), "after": after.json()})
    assert initial_state == _workspace_state(after)
    assert initial_registry == _registry(case.workspace)
    node = await case.client.get("/api/v1/session-catalog/export")
    assert node.status_code == 200, node.text
    row = next(item for item in node.json()["data"]["items"] if item["node_id"] == session_id)
    persisted = case.workspace / ".boxteam/sessions" / row["storage_relative_path"] / "ui/file-tree-shortcuts.json"
    saved = json.loads(persisted.read_text())
    assert saved["shortcuts"] == [{"path": uri, "label": external.name}]
    assert str(external) not in persisted.read_text()
    profile_path = case.workspace.parent / "boxteam-home/state/gateway/users" / user_id / "profile.jsonc"
    profile = json.loads(profile_path.read_text())
    preview_uri = result["file_preferences"]["layout"]["workspace_preview_active_file_path"]
    assert profile["layout"]["workspace_preview_file_paths"] == [preview_uri]
    assert profile["layout"]["workspace_preview_active_file_path"] == preview_uri
    expanded = profile["preferences"]["workspace_file_tree"]["expanded_paths_by_workspace"]
    assert uri in next(iter(expanded.values()))
    assert str(external) not in profile_path.read_text()
    for field, value in (("workspace_preview_file_paths", [str(file)]), ("workspace_preview_active_file_path", str(file))):
        original_profile = profile_path.read_bytes()
        denied = await case.client.put("/api/gateway/ui-settings", json={"layout": {field: value}})
        assert denied.status_code == 422, denied.text
        assert str(external) not in denied.text
        assert denied.json()["request_id"] == denied.headers["X-Request-ID"]
        assert denied.json()["detail"] == [{"loc": ["body", "layout", field], "type": "value_error", "msg": "UI 设置字段校验失败"}]
        assert profile_path.read_bytes() == original_profile
    denied = await case.client.put("/api/gateway/ui-settings", json={"workspace_file_tree": {
        "expanded_paths_by_workspace": {"target": [str(external)]}}})
    assert denied.status_code == 422, denied.text
    assert str(external) not in denied.text
    assert denied.json()["request_id"] == denied.headers["X-Request-ID"]
    assert denied.json()["detail"] == [{"loc": ["body", "workspace_file_tree", "expanded_paths_by_workspace"],
                                        "type": "value_error", "msg": "UI 设置字段校验失败"}]
    assert profile_path.read_bytes() == original_profile
    # 损坏的旧落盘值不是本次请求输入；冷读与更新失败均不得把它回显到普通响应。
    invalid_profile = {**profile, "layout": {**profile["layout"], "workspace_preview_file_paths": [str(file)]}}
    invalid_bytes = json.dumps(invalid_profile).encode()
    profile_path.write_bytes(invalid_bytes)
    try:
        for method, options in (("GET", {}), ("PUT", {"json": {"layout": {"workbench_view": "sessions"}}})):
            rejected = await case.client.request(method, "/api/gateway/ui-settings", **options)
            assert rejected.status_code == 409, rejected.text
            assert "workspace_preview_file_paths" in rejected.text
            assert str(external) not in rejected.text
            assert profile_path.read_bytes() == invalid_bytes
    finally:
        profile_path.write_bytes(original_profile)
    # 两个 owner 均重启，新 Chromium context 没有本地快捷目录或文件内容缓存。
    await case.hard_restart()
    previous_gateway = case.gateway
    await asyncio.to_thread(close_gateway_process, previous_gateway)
    case.gateway = await asyncio.to_thread(
        start_gateway_process, workspace_root=case.workspace, port=previous_gateway.port,
        default_backend_url=f"http://127.0.0.1:{case.backend.port}",
        extra_env={"BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist")},
    )
    assert case.gateway.process.pid != previous_gateway.process.pid
    access = await case.client.post(f"/api/gateway/users/{user_id}/takeover", json={"client_label": "冷读文件偏好验收"})
    assert access.status_code == 200, access.text
    health = await case.client.get("/api/gateway/health")
    assert health.status_code == 200 and health.json()["data"]["gateway_id"] == case.gateway_id
    reopened = await _browse(case, {**fixture, "shortcut_uri": uri}, artifacts / "cold-reopen")
    assert reopened["shortcut_uri"] == uri and reopened["reopened_content"] == edited
    assert reopened["file_preferences"]["layout"]["workspace_preview_active_file_path"] == preview_uri
    assert file.read_text(encoding="utf-8") == edited
    after_restart = await case.client.get("/api/gateway/workspaces")
    assert after_restart.status_code == 200, after_restart.text
    assert initial_state == _workspace_state(after_restart)
    assert initial_registry == _registry(case.workspace)
    assert not (external / ".boxteam").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"gateway_environment": {
    "BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist"),
}}], indirect=True)
async def test_chat_file_references_preserve_native_and_uri_targets(communication_case):
    case = communication_case
    local = case.workspace / "reference-target.ts"
    local.write_text("本地原件\n定位第二行\n", encoding="utf-8")
    external = case.workspace.parent / "引用 外部%2F"
    external.mkdir(exist_ok=True)
    native = external / local.name
    native.write_text("外部原件\n独立第二行\n", encoding="utf-8")
    decoy = case.workspace / str(native).lstrip("/")
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("不得打开的旧回退对照\n", encoding="utf-8")
    local_response = await case.client.get("/api/v1/workspace/files/content", params={"path": str(local)})
    native_response = await case.client.get("/api/v1/workspace/files/content", params={"path": str(native)})
    assert local_response.status_code == 200, local_response.text
    assert native_response.status_code == 200, native_response.text
    local_uri, native_uri = local_response.json()["data"]["path"], native_response.json()["data"]["path"]
    private = case.workspace / ".boxteam/cache/private-boundary.txt"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("private boundary original", encoding="utf-8")
    private_uri = file_uri_child(file_uri_root(local_uri), ".boxteam", "cache", private.name)
    for target in (str(private), private_uri):
        for suffix in ("content", "raw", "download"):
            denied = await case.client.get(f"/api/v1/workspace/files/{suffix}", params={"path": target})
            assert denied.status_code == 403, denied.text
            assert "private boundary original" not in denied.text
            assert str(case.workspace) not in denied.text
    public_tree = await case.client.get("/api/v1/workspace/files")
    assert public_tree.status_code == 200, public_tree.text
    assert ".boxteam" not in {item["name"] for item in public_tree.json()["data"]["items"]}
    references = [
        {"label": "相对文件", "target": local.name, "uri": local_uri, "content": local.read_text()},
        {"label": "原生绝对", "target": str(local), "uri": local_uri, "content": local.read_text()},
        {"label": "资源地址", "target": local_uri, "uri": local_uri, "content": local.read_text()},
        {"label": "外部原生", "target": str(native), "uri": native_uri, "content": native.read_text()},
        {"label": "外部资源", "target": native_uri, "uri": native_uri, "content": native.read_text()},
        {"label": "文件URL", "target": native.as_uri(), "uri": native_uri, "content": native.read_text()},
    ]
    missing = str(external / "不存在" / local.name)
    markdown = "\n\n".join(f'[{item["label"]}](<{item["target"]}#L2>)' for item in references)
    markdown += f"\n\n[缺失绝对](<{missing}>)\n\n`{local_uri}`"
    title = "文件链接统一目标"
    session_id = await create_session(case.client, title)
    # 受控 Provider 回显用户给定目标以验证 Markdown 文件入口；不代表系统引用生成策略。
    case.model.response_callback = lambda _payload: {"content": markdown}
    response = await case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": markdown}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    await _completed(case.client, response.json()["data"]["job_id"])
    result = await _browse(case, {"session_id": session_id, "session_title": title,
        "references": references, "missing": missing, "directory": str(external)},
        case.artifacts / f"run-{os.getpid()}/references")
    assert result["references"] == [item["uri"] for item in references]
    assert result["page_errors"] == []
    assert native.read_text() == "外部原件\n独立第二行\n"
    assert decoy.read_text() == "不得打开的旧回退对照\n"
    assert local.read_text() == "本地原件\n定位第二行\n"
    assert private.read_text(encoding="utf-8") == "private boundary original"


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_case", [{"gateway_environment": {
    "BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist"),
}}], indirect=True)
async def test_gateway_directory_selector_registers_uri_after_native_input_and_reload(communication_case):
    case = communication_case
    directory = case.workspace.parent / "选择 目录% Ω"
    target = prepare_default_test_workspace(
        workspace_root=directory / "alpha 工程%",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    # 新工作区不能沿用模板固定 UUID；由实际注册 owner 创建独立身份。
    workspace_identity_path(target).unlink()
    install_test_workspace_config(
        workspace_root=target, config_path=case.workspace / ".boxteam/workspace.jsonc",
        schema_path=case.workspace / ".boxteam/workspace_schema.jsonc",
    )
    for path in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
        response = await case.client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
    before = await case.client.get("/api/gateway/workspaces")
    result = await _browse(case, {"directory_selection": True, "directory": str(directory),
        "directory_name": directory.name, "target_name": target.name},
        case.artifacts / f"run-{os.getpid()}/directory-selection")
    assert result["request"]["root_path"] == result["target_uri"]
    after = await case.client.get("/api/gateway/workspaces")
    assert after.status_code == 200, after.text
    previous = {item["workspace_id"] for item in before.json()["data"]["items"]}
    created = [item for item in after.json()["data"]["items"] if item["workspace_id"] not in previous]
    assert len(created) == 1 and created[0]["name"] == target.name
    assert created[0]["managed"] is True and created[0]["status"] == "ready"
    assert str(directory) not in after.text
    # 真实控制面根绑定必须精确指向所选目录；不允许按 URI 文本拼出另一个目录。
    rows = _registry(case.workspace)
    payload = json.loads(next(row[3] for row in rows if row[0] == created[0]["workspace_id"]))
    assert payload["root_path"] == str(target)
    assert result["page_errors"] == []


@pytest.mark.asyncio
async def test_remote_directory_selection_resolves_only_on_fixed_gateway(communication_triad):
    nodes, _, artifacts = communication_triad
    source, target = nodes["a"], nodes["b"]
    directory = target.workspace / "remote 目录% Ω"
    directory.mkdir()
    (directory / "child").mkdir()
    before = _registry(source.workspace)
    with closing(sqlite3.connect(source.gateway_root / "gateway.sqlite")) as connection:
        native_before = connection.execute("SELECT * FROM native_directory_roots ORDER BY directory_id").fetchall()
    response = await source.client.get("/api/gateway/local-directories", params={
        "path": str(directory), "gateway_connection_id": "rgw_c3_b",
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert str(directory) not in response.text
    assert data["name"] == directory.name and [entry["name"] for entry in data["entries"]] == ["child"]
    for uri in (data["path"], data["parent_path"], data["home_path"], data["entries"][0]["path"]):
        parsed = parse_resource_uri(uri)
        assert parsed.scope == "gateway" and parsed.scope_id == target.gateway_id
    cold = await source.client.get("/api/gateway/local-directories", params={
        "path": data["path"], "gateway_connection_id": "rgw_c3_b",
    })
    assert cold.status_code == 200 and cold.json()["data"] == data
    child = await source.client.get("/api/gateway/local-directories", params={
        "path": "child", "base_path": data["path"], "gateway_connection_id": "rgw_c3_b",
    })
    assert child.status_code == 200 and child.json()["data"]["name"] == "child"
    for connection in (None, "rgw_c3_c"):
        wrong = await source.client.get("/api/gateway/local-directories", params={
            "path": data["path"], **({"gateway_connection_id": connection} if connection else {}),
        })
        assert wrong.status_code == 400, wrong.text
        assert str(directory) not in wrong.text
        assert wrong.json()["request_id"] == wrong.headers["X-Request-ID"]
    assert _registry(source.workspace) == before
    with closing(sqlite3.connect(source.gateway_root / "gateway.sqlite")) as connection:
        assert connection.execute("SELECT * FROM native_directory_roots ORDER BY directory_id").fetchall() == native_before
    with closing(sqlite3.connect(target.gateway_root / "gateway.sqlite")) as connection:
        assert connection.execute("SELECT native_root FROM native_directory_roots WHERE gateway_id=? AND directory_id=?",
            (target.gateway_id, parse_resource_uri(data["path"]).tail_segments[0])).fetchone() == (str(directory),)
    assert not (directory / ".boxteam").exists()
    (artifacts / "directory-owner.json").write_text(json.dumps({
        "listing": response.json(), "reopened": cold.json(), "relative": child.json(),
        "source_gateway": source.gateway_id, "target_gateway": target.gateway_id,
    }, ensure_ascii=False, indent=2))
