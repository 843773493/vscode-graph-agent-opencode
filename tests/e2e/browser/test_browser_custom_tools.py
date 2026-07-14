from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

import httpx
import pytest
import websockets

from app.agents.tools.browser import (
    create_click_element_tool,
    create_drag_element_tool,
    create_handle_dialog_tool,
    create_hover_element_tool,
    create_navigate_page_tool,
    create_open_browser_page_tool,
    create_read_page_tool,
    create_run_playwright_code_tool,
    create_screenshot_page_tool,
    create_type_in_page_tool,
)
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.e2e.processes import (
    kill_process_on_port,
    terminate_process,
    wait_for_http_ok,
)


CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS = (
    "AGENTS.md",
    ".boxteam/boxteam.json",
    ".boxteam/skills",
)


def _browser_ports(e2e_backend_port: int) -> tuple[int, int]:
    return e2e_backend_port + 60, e2e_backend_port + 61


def _tool_context(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        browser_manager_client=BrowserManagerClient(),
    )


def _json_tool_result(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise AssertionError(f"浏览器扩展工具应返回 JSON 字符串，实际: {type(raw).__name__}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise AssertionError(f"浏览器扩展工具返回值不是对象: {raw}")
    return parsed


def _browser_test_data_url() -> str:
    html = """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>BoxTeam Browser Tool Test</title>
    <style>
      body { font-family: sans-serif; padding: 24px; }
      #drag-source, #drop-target {
        display: inline-flex;
        width: 120px;
        height: 48px;
        align-items: center;
        justify-content: center;
        border: 1px solid #777;
        margin-right: 16px;
      }
      #drop-target { background: #f2f2f2; }
      #hover-area { margin-top: 16px; padding: 12px; border: 1px dashed #999; }
    </style>
  </head>
  <body>
    <h1>BoxTeam Browser Tool Test</h1>
    <input id="name" aria-label="name input" value="" />
    <button id="apply">Apply</button>
    <button id="alert-button">Alert</button>
    <p id="result">empty</p>
    <div id="hover-area">hover me</div>
    <div id="drag-source" draggable="true">drag source</div>
    <div id="drop-target">drop target</div>
    <script>
      document.querySelector("#apply").addEventListener("click", () => {
        document.querySelector("#result").textContent =
          "Hello " + document.querySelector("#name").value;
      });
      document.querySelector("#alert-button").addEventListener("click", () => {
        alert("BOXTEAM_DIALOG_CLICK");
      });
      document.querySelector("#hover-area").addEventListener("mouseenter", () => {
        document.body.dataset.hovered = "yes";
      });
      document.querySelector("#drag-source").addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("text/plain", "BOXTEAM_DRAG_OK");
      });
      document.querySelector("#drop-target").addEventListener("dragover", (event) => {
        event.preventDefault();
      });
      document.querySelector("#drop-target").addEventListener("drop", (event) => {
        event.preventDefault();
        event.currentTarget.textContent = event.dataTransfer.getData("text/plain");
      });
    </script>
  </body>
</html>
"""
    return "data:text/html;charset=utf-8," + quote(html)


async def _recv_ws_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(websocket.recv(), timeout=max(deadline - time.monotonic(), 0.1))
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到浏览器 WebSocket 消息: {expected_type}")


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest, e2e_session_marker: str) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = project_root / "out" / "tests" / "e2e" / relative_test_path
    template_root = project_root / "asset" / "custom_tool_test_workspace"
    lock_file = workspace_root / ".e2e_session_lock"

    same_session = lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == e2e_session_marker
    if workspace_root.exists() and not same_session:
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    for item in workspace_root.iterdir():
        if item.resolve() == lock_file.resolve():
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for relative_item in CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS:
        item = template_root / relative_item
        if not item.exists():
            raise FileNotFoundError(f"custom tool e2e 模板缺少必要文件: {item}")
        target = workspace_root / item.name
        if relative_item.startswith(".boxteam/"):
            target = workspace_root / relative_item
            target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

    lock_file.write_text(e2e_session_marker, encoding="utf-8")
    return str(workspace_root)


@pytest.fixture(scope="module", autouse=True)
def browser_manager_env(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = _browser_ports(e2e_backend_port)
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    updates = {
        "WORKSPACE_ROOT": workspace_root,
        "BOXTEAM_BROWSER_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
        "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{frontend_port}",
        "BOXTEAM_BROWSER_WORKSPACE_ROOT": workspace_root,
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield backend_port, frontend_port
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def browser_manager_processes(
    browser_manager_env: tuple[int, int],
    e2e_workspace_root_path: str,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = browser_manager_env
    kill_process_on_port(backend_port)
    kill_process_on_port(frontend_port)

    project_root = Path.cwd().resolve()
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_BROWSER_WORKSPACE_ROOT"] = workspace_root

    backend_process = subprocess.Popen(
        [
            node_bin,
            "backend.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--frontend-url",
            f"http://127.0.0.1:{frontend_port}",
            "--workspace-root",
            workspace_root,
        ],
        cwd=project_root / "src" / "browser" / "server",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    frontend_process = subprocess.Popen(
        [
            node_bin,
            "server.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            "--backend-url",
            f"http://127.0.0.1:{backend_port}",
            "--workspace-root",
            workspace_root,
            "--asset-root",
            str(project_root),
        ],
        cwd=project_root / "src" / "browser" / "client",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
        yield backend_port, frontend_port
    finally:
        terminate_process(frontend_process)
        terminate_process(backend_process)
        kill_process_on_port(frontend_port)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_browser_custom_tools_are_invokable_and_exposed_as_resource(
    browser_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    backend_port, frontend_port = browser_manager_processes
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Browser Custom Tools E2E"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    context = _tool_context(session_id)

    open_page = create_open_browser_page_tool(context)
    read_page = create_read_page_tool(context)
    type_in_page = create_type_in_page_tool(context)
    click_element = create_click_element_tool(context)
    hover_element = create_hover_element_tool(context)
    drag_element = create_drag_element_tool(context)
    handle_dialog = create_handle_dialog_tool(context)
    navigate_page = create_navigate_page_tool(context)
    screenshot_page = create_screenshot_page_tool(context)
    run_playwright_code = create_run_playwright_code_tool(context)

    opened = _json_tool_result(
        await open_page.ainvoke({"url": _browser_test_data_url(), "forceNew": False})
    )
    page_id = str(opened["pageId"])
    assert page_id.startswith("browser_")
    assert opened["attach_url"] == f"http://127.0.0.1:{frontend_port}/?browserId={page_id}"

    summary = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    assert "BoxTeam Browser Tool Test" in str(summary["summary"])
    assert "Apply" in str(summary["summary"])

    await type_in_page.ainvoke(
        {
            "pageId": page_id,
            "selector": "#name",
            "text": "Box Browser",
        }
    )
    await click_element.ainvoke({"pageId": page_id, "selector": "#apply"})
    result_text = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "return await page.locator('#result').innerText();",
                "timeoutMs": 5000,
            }
        )
    )
    assert result_text["result"] == "Hello Box Browser"

    await hover_element.ainvoke({"pageId": page_id, "selector": "#hover-area"})
    hovered = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "return await page.evaluate(() => document.body.dataset.hovered);",
                "timeoutMs": 5000,
            }
        )
    )
    assert hovered["result"] == "yes"

    await drag_element.ainvoke(
        {
            "pageId": page_id,
            "fromSelector": "#drag-source",
            "toSelector": "#drop-target",
        }
    )
    dropped = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "return await page.locator('#drop-target').innerText();",
                "timeoutMs": 5000,
            }
        )
    )
    assert dropped["result"] == "BOXTEAM_DRAG_OK"

    click_dialog_state = _json_tool_result(
        await click_element.ainvoke({"pageId": page_id, "selector": "#alert-button"})
    )
    pending_dialog = click_dialog_state.get("pending_dialog")
    assert isinstance(pending_dialog, dict)
    assert pending_dialog["message"] == "BOXTEAM_DIALOG_CLICK"
    dialog_click_result = _json_tool_result(
        await handle_dialog.ainvoke({"pageId": page_id, "acceptModal": True})
    )
    assert "BOXTEAM_DIALOG_CLICK" in str(dialog_click_result["summary"])

    await run_playwright_code.ainvoke(
        {
            "pageId": page_id,
            "code": "await page.evaluate(() => setTimeout(() => alert('BOXTEAM_DIALOG_OK'), 100)); return 'scheduled';",
            "timeoutMs": 5000,
        }
    )
    await asyncio.sleep(0.2)
    dialog_result = _json_tool_result(
        await handle_dialog.ainvoke({"pageId": page_id, "acceptModal": True})
    )
    assert "BOXTEAM_DIALOG_OK" in str(dialog_result["summary"])

    screenshot = _json_tool_result(
        await screenshot_page.ainvoke({"pageId": page_id, "selector": "#result"})
    )
    image_path = Path(str(screenshot["image_path"]))
    assert image_path.exists()
    assert image_path.is_relative_to(Path(e2e_workspace_root_path).resolve())

    reloaded = _json_tool_result(
        await navigate_page.ainvoke({"pageId": page_id, "type": "reload"})
    )
    assert reloaded["browser_id"] == page_id

    bare_local_url = f"127.0.0.1:{frontend_port}/health"
    bare_navigation = _json_tool_result(
        await navigate_page.ainvoke({"pageId": page_id, "type": "url", "url": bare_local_url})
    )
    assert bare_navigation["url"] == f"http://{bare_local_url}"

    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    browser_resource = next(
        resource
        for resource in resources
        if resource["kind"] == "browser" and resource["resource_id"] == page_id
    )
    assert browser_resource["status"] == "running"
    assert "cancel" in browser_resource["available_actions"]
    assert "delete" in browser_resource["available_actions"]
    assert browser_resource["metadata"]["attach_url"] == opened["attach_url"]

    with urlopen(str(opened["attach_url"]), timeout=5) as response:
        html = response.read().decode("utf-8")
    assert response.status == 200
    assert "可附加浏览器" in html

    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": page_id}))
        attached = await _recv_ws_type(websocket, "attached")
        assert attached["browserId"] == page_id
        await websocket.send(json.dumps({"type": "detach"}))
        detached = await _recv_ws_type(websocket, "detached")
        assert detached["browserId"] == page_id

    skill_path = Path(e2e_workspace_root_path) / ".boxteam" / "skills" / "browser-control" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    for tool_name in [
        "openBrowserPage",
        "readPage",
        "clickElement",
        "typeInPage",
        "hoverElement",
        "dragElement",
        "handleDialog",
        "navigatePage",
        "screenshotPage",
        "runPlaywrightCode",
    ]:
        assert tool_name in skill_text

    delete_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/browser/{page_id}/control",
        json={"action": "delete"},
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["status"] == "deleted"

    resources_after_delete = await client.get(
        f"/api/v1/sessions/{session_id}/resources"
    )
    assert resources_after_delete.status_code == 200
    assert all(
        resource["resource_id"] != page_id
        for resource in resources_after_delete.json()["data"]["items"]
    )


@pytest.mark.asyncio
async def test_browser_open_failure_is_exposed_as_failed_resource(
    browser_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
):
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Browser Open Failure E2E"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    open_page = create_open_browser_page_tool(_tool_context(session_id))

    with pytest.raises(RuntimeError) as exc_info:
        await open_page.ainvoke(
            {"url": "http://127.0.0.1:9/boxteam-browser-open-failure"}
        )

    assert "浏览器管理器请求失败" in str(exc_info.value)
    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    failed_browser = next(resource for resource in resources if resource["kind"] == "browser")
    assert failed_browser["status"] == "failed"
    assert failed_browser["available_actions"] == ["delete"]
    assert "page.goto" in failed_browser["metadata"]["error_message"]


@pytest.mark.asyncio
async def test_browser_delete_while_attached_keeps_manager_alive(
    browser_manager_processes: tuple[int, int],
):
    backend_port, _frontend_port = browser_manager_processes
    context = _tool_context("delete_attached_browser")
    open_page = create_open_browser_page_tool(context)
    opened = _json_tool_result(
        await open_page.ainvoke({"url": _browser_test_data_url(), "forceNew": True})
    )
    page_id = str(opened["pageId"])

    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": page_id}))
        attached = await _recv_ws_type(websocket, "attached")
        assert attached["browserId"] == page_id
        delete_request = Request(
            f"http://127.0.0.1:{backend_port}/api/browsers/{page_id}",
            method="DELETE",
        )
        with urlopen(delete_request, timeout=5) as response:
            assert response.status == 200

    with urlopen(f"http://127.0.0.1:{backend_port}/health", timeout=5) as response:
        assert response.status == 200
