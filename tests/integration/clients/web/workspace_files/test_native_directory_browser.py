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
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - fixture 以当前测试路径隔离
)
from tests.support.api_helpers import create_session
from tests.support.gateway_processes import (
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
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
    process = await asyncio.to_thread(subprocess.run, ["bun",
        "tests/integration/clients/web/workspace_files/native_directory_browser.mjs"],
        env={**os.environ, "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url),
             "BOXTEAM_BROWSER_FIXTURE": json.dumps(fixture, ensure_ascii=False),
             "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
             "BOXTEAM_BROWSER_RUNTIME_PATH": str(case.workspace.parent / "runtime/browser"),
             "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable},
        cwd=Path.cwd(), capture_output=True, text=True, timeout=150, check=False)
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
               "file_name": file.name, "original": original, "edited": edited}
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
    await acquire_gateway_guest(case.client)
    health = await case.client.get("/api/gateway/health")
    assert health.status_code == 200 and health.json()["data"]["gateway_id"] == case.gateway_id
    reopened = await _browse(case, {**fixture, "shortcut_uri": uri}, artifacts / "cold-reopen")
    assert reopened["shortcut_uri"] == uri and reopened["reopened_content"] == edited
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
