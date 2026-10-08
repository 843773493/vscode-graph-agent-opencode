from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import httpx
import pytest
import websockets
from fastapi import FastAPI

from app.abstractions.browser_resources import BrowserRequestError
from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tools.browser import (
    create_click_element_tool,
    create_drag_element_tool,
    create_handle_dialog_tool,
    create_hover_element_tool,
    create_list_browser_page_tool,
    create_navigate_page_tool,
    create_open_browser_page_tool,
    create_read_page_tool,
    create_run_playwright_code_tool,
    create_screenshot_page_tool,
    create_type_in_page_tool,
)
from app.core.mounted_workspaces import MountedWorkspace
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.main import workspace_api
from app.services.business.workspace_resources.records import ThreadRef
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    start_terminal_backend,
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.processes import (
    kill_process_on_port,
    terminate_process,
    wait_for_http_ok,
)
from tests.support.trace import get_trace_payload
from tests.support.workspace_tool_runtime import (
    WorkspaceToolRuntime,
    workspace_tool_runtime,
)
from tests.support.workspaces import prepare_test_workspace

CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS = (
    "AGENTS.md",
    ".boxteam/skills",
)


def _browser_ports(integration_backend_port: int) -> tuple[int, int]:
    return integration_backend_port + 60, integration_backend_port + 61


def _json_tool_result(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise TypeError(f"浏览器扩展工具应返回 JSON 字符串，实际: {type(raw).__name__}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError(f"浏览器扩展工具返回值不是对象: {raw}")
    return parsed


def _read_http(target: str | Request) -> tuple[int, bytes]:
    with urlopen(target, timeout=5) as response:
        return response.status, response.read()


def _read_http_error_status_and_body(target: str | Request) -> tuple[int, bytes]:
    try:
        _read_http(target)
    except HTTPError as error:
        return error.code, error.read()
    raise AssertionError(f"预期 HTTP 错误但请求成功: {target}")


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
        if not isinstance(raw, str):
            continue
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到浏览器 WebSocket 消息: {expected_type}")


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "integration"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = (
        project_root / "out" / "tests" / "integration" / relative_test_path / "workspace"
    )
    template_root = project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace"
    prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS,
        shared_skill_root=project_root / "resources" / "skills",
    )
    return str(workspace_root)


@pytest.fixture(scope="module", autouse=True)
def browser_manager_env(
    integration_workspace_root_path: str,
    integration_backend_port: int,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = _browser_ports(integration_backend_port)
    workspace_root = str(Path(integration_workspace_root_path).resolve())
    updates = {
        "WORKSPACE_ROOT": workspace_root,
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
def integration_startup_mount(
    browser_manager_processes: tuple[int, int], integration_workspace_root_path: str,
) -> MountedWorkspace:
    root = Path(integration_workspace_root_path).resolve()
    return MountedWorkspace(
        load_or_create_workspace_id(root), root,
        browser_backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
        terminal_backend_url=f"http://127.0.0.1:{browser_manager_processes[0] + 2}",
    )


@pytest.fixture(scope="module")
def browser_manager_processes(
    browser_manager_env: tuple[int, int],
    integration_workspace_root_path: str,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = browser_manager_env
    kill_process_on_port(backend_port)
    kill_process_on_port(frontend_port)

    project_root = Path.cwd().resolve()
    workspace_root = str(Path(integration_workspace_root_path).resolve())
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_BROWSER_WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_WORKSPACE_ID"] = load_or_create_workspace_id(Path(workspace_root))

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
        cwd=project_root / "src" / "workspace-services" / "browser" / "server",
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
        cwd=project_root / "src" / "workspace-services" / "browser" / "client",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    terminal_process = None
    try:
        terminal_process = start_terminal_backend(backend_port=backend_port + 2, workspace_root=workspace_root)
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
        yield backend_port, frontend_port
    finally:
        terminate_process(frontend_process)
        terminate_process(backend_process)
        kill_process_on_port(frontend_port)
        kill_process_on_port(backend_port)
        if terminal_process is not None:
            terminate_process(terminal_process)
        kill_process_on_port(backend_port + 2)


@pytest.fixture
def browser_application() -> FastAPI:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(workspace_api, prefix="/api/v1")
    return application


@pytest.fixture
async def browser_runtime(
    browser_manager_processes: tuple[int, int],
    integration_workspace_root_path: str,
    browser_application: FastAPI,
) -> AsyncIterator[WorkspaceToolRuntime]:
    del browser_manager_processes
    async with workspace_tool_runtime(Path(integration_workspace_root_path), application=browser_application) as runtime:
        yield runtime


@pytest.fixture
def browser_context(browser_runtime: WorkspaceToolRuntime) -> CustomToolFactoryContext:
    container, session_id, invocation = browser_runtime
    return CustomToolFactoryContext(
        session_id=session_id, agent_id="default", sender_agent_id="default",
        workspace_root=container.workspace_root,
        background_task_registry=container.background_task_registry,
        background_message_bus=container.background_message_bus, job_event_bus=container.job_event_bus,
        job_service=container.job_service, session_context_query_service=container.session_context_query_service,
        workspace_session_context_client=container.agent_execution_service._dependency_provider.get_workspace_session_context_client(),
        session_orchestrator=container.session_orchestrator, config_service=container.config_service,
        terminal_resource_owner=container.terminal_resource_owner,
        browser_resource_owner=container.browser_resource_owner, invocation_context=invocation,
    )


@pytest.fixture
async def integration_client(
    browser_runtime: WorkspaceToolRuntime, browser_application: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    container, _session, _context = browser_runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_application), base_url="http://workspace.test",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": container.workspace_id},
    ) as client:
        yield client


async def _grant(client: httpx.AsyncClient, resource_id: str, context: CustomToolFactoryContext) -> dict[str, object]:
    detail = await client.get(f"/api/v1/resources/{resource_id}")
    assert detail.status_code == 200, detail.text
    resource = detail.json()["data"]
    granted = await client.post(f"/api/v1/resources/{resource_id}/transport-grant", json={
        "expected_revision": resource["owner_revision"],
        "thread": {"session_id": context.session_id, "thread_id": context.invocation_context.thread_binding.thread_id},
    })
    assert granted.status_code == 200, granted.text
    return granted.json()["data"]


async def _detach_delete(client: httpx.AsyncClient, resource_id: str, context: CustomToolFactoryContext) -> httpx.Response:
    detail = (await client.get(f"/api/v1/resources/{resource_id}")).json()["data"]
    detached = await client.post(f"/api/v1/resources/{resource_id}/detach", json={
        "expected_revision": detail["owner_revision"],
        "thread": {"session_id": context.session_id, "thread_id": context.invocation_context.thread_binding.thread_id},
    })
    assert detached.status_code == 200, detached.text
    return await client.post(f"/api/v1/resources/{resource_id}/delete", json={"expected_revision": detached.json()["data"]["owner_revision"]})


@pytest.mark.asyncio
async def test_browser_custom_tools_are_invokable_and_exposed_as_resource(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
    integration_workspace_root_path: str,
):
    backend_port, frontend_port = browser_manager_processes
    session_id = browser_context.session_id
    context = browser_context

    list_pages = create_list_browser_page_tool(context)
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
    assert "attach_url" not in opened

    listed = _json_tool_result(await list_pages.ainvoke({}))
    listed_page = next(
        page for page in listed["pages"] if page["pageId"] == page_id
    )
    assert listed_page["url"] == _browser_test_data_url()
    assert listed_page["status"] == "running"
    assert "attach_url" not in listed_page
    assert "checkpoint" not in listed_page

    summary = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    assert "BoxTeam Browser Tool Test" in str(summary["summary"])
    assert "Apply" in str(summary["summary"])

    await run_playwright_code.ainvoke(
        {
            "pageId": page_id,
            "code": (
                "const popup = await context.newPage();"
                "await popup.setContent('<title>Popup Tab</title><p>popup</p>');"
                "return popup.url();"
            ),
            "timeoutMs": 5000,
        }
    )
    popup_state = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    assert len(popup_state["pages"]) == 2
    popup_tab_id = str(popup_state["activePageId"])
    assert popup_tab_id != page_id
    assert "Popup Tab" in {str(page["title"]) for page in popup_state["pages"]}

    activated = _json_tool_result(
        await navigate_page.ainvoke(
            {"pageId": page_id, "type": "activate_tab", "tabId": page_id}
        )
    )
    assert activated["activePageId"] == page_id
    closed_popup = _json_tool_result(
        await navigate_page.ainvoke(
            {"pageId": page_id, "type": "close_tab", "tabId": popup_tab_id}
        )
    )
    assert len(closed_popup["pages"]) == 1

    new_tab = _json_tool_result(
        await navigate_page.ainvoke({"pageId": page_id, "type": "new_tab"})
    )
    assert str(new_tab["created_page_id"]).startswith("page_")
    await navigate_page.ainvoke(
        {
            "pageId": page_id,
            "type": "close_tab",
            "tabId": str(new_tab["created_page_id"]),
        }
    )
    await navigate_page.ainvoke(
        {"pageId": page_id, "type": "activate_tab", "tabId": page_id}
    )

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
    # 负向断言（6.3）：模型可见载荷 MUST NOT 出现文件系统路径。
    assert "image_path" not in screenshot
    screenshot_id = str(screenshot["screenshot_id"])
    assert screenshot_id.startswith("screenshot_")
    screenshot_url = str(screenshot["screenshot_url"])
    assert screenshot_url.endswith(f"/api/browsers/{page_id}/screenshots/{screenshot_id}")
    assert str(integration_workspace_root_path) not in screenshot_url

    # 截图的对外可寻址改由浏览器管理器只读端点承载（裁定修正 2）。
    shot_status, shot_body = await asyncio.to_thread(
        _read_http,
        f"http://127.0.0.1:{backend_port}{screenshot_url}?workspace_id={browser_context.browser_resource_owner.workspace_id}",
    )
    assert shot_status == 200
    assert shot_body[:8] == b"\x89PNG\r\n\x1a\n"

    # 截图端点负向断言：id 不存在、browser 不存在、文件缺失都必须回稳定结构化
    # 错误（明确 code），且响应体 MUST NOT 泄漏真实路径（6.3）。
    missing_shot_status, missing_shot_body = await asyncio.to_thread(
        _read_http_error_status_and_body,
        f"http://127.0.0.1:{backend_port}"
        f"/api/browsers/{page_id}/screenshots/screenshot_doesnotexist?workspace_id={browser_context.browser_resource_owner.workspace_id}",
    )
    assert missing_shot_status == 404
    missing_shot_payload = json.loads(missing_shot_body)
    assert missing_shot_payload["code"] == "browser_artifact_not_found"
    assert str(integration_workspace_root_path) not in missing_shot_body.decode("utf-8", "replace")
    assert ".png" not in missing_shot_payload["error"]

    missing_browser_status, missing_browser_body = await asyncio.to_thread(
        _read_http_error_status_and_body,
        f"http://127.0.0.1:{backend_port}"
        f"/api/browsers/browser_doesnotexist/screenshots/screenshot_x?workspace_id={browser_context.browser_resource_owner.workspace_id}",
    )
    assert missing_browser_status == 404
    missing_browser_payload = json.loads(missing_browser_body)
    assert missing_browser_payload["code"] == "browser_page_not_found"
    assert str(integration_workspace_root_path) not in missing_browser_body.decode("utf-8", "replace")


    before_reload = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    stale_apply_ref = next(
        str(item["ref"])
        for item in before_reload["refs"]
        if item.get("text") == "Apply"
    )
    reloaded = _json_tool_result(
        await navigate_page.ainvoke({"pageId": page_id, "type": "reload"})
    )
    assert reloaded["browser_id"] == page_id
    stale_started_at = time.monotonic()
    with pytest.raises(RuntimeError) as stale_error:
        await click_element.ainvoke({"pageId": page_id, "ref": stale_apply_ref})
    assert time.monotonic() - stale_started_at < 1
    assert "请重新调用 readPage" in str(stale_error.value)

    bare_local_url = f"127.0.0.1:{frontend_port}/health"
    bare_navigation = _json_tool_result(
        await navigate_page.ainvoke({"pageId": page_id, "type": "url", "url": bare_local_url})
    )
    assert bare_navigation["url"] == f"http://{bare_local_url}"

    resources_response = await integration_client.get(
        f"/api/v1/sessions/{session_id}/resources"
    )
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    browser_resource = next(
        resource
        for resource in resources
        if resource["kind"] == "browser" and resource["resource_id"] == page_id
    )
    assert browser_resource["status"] == "running"
    assert "cancel" in browser_resource["available_actions"]
    assert "delete" not in browser_resource["available_actions"]
    assert "attach_url" not in browser_resource["metadata"]

    frontend_status, html_body = await asyncio.to_thread(
        _read_http,
        f"http://127.0.0.1:{frontend_port}/?browserId={page_id}",
    )
    html = html_body.decode("utf-8")
    assert frontend_status == 200
    assert "可附加浏览器" in html

    grant = await _grant(integration_client, page_id, browser_context)
    workspace_id = grant["resource"]["workspace_id"]
    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser?workspace_id={workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": page_id, "grantToken": grant["grant_token"]}))
        attached = await _recv_ws_type(websocket, "attached")
        assert attached["browserId"] == page_id
        await websocket.send(json.dumps({"type": "detach"}))
        detached = await _recv_ws_type(websocket, "detached")
        assert detached["browserId"] == page_id

    skill_path = Path(integration_workspace_root_path) / ".boxteam" / "skills" / "browser-control" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    for tool_name in [
        "listBrowserPage",
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

    delete_response = await _detach_delete(integration_client, page_id, browser_context)
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["status"] == "deleted"

    resources_after_delete = await integration_client.get(
        f"/api/v1/sessions/{session_id}/resources"
    )
    assert resources_after_delete.status_code == 200
    assert all(
        resource["resource_id"] != page_id
        for resource in resources_after_delete.json()["data"]["items"]
    )


@pytest.mark.asyncio
async def test_agent_lists_current_browser_website_through_documented_tool(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
):
    session_id = browser_context.session_id
    context = browser_context
    open_page = create_open_browser_page_tool(context)
    opened = _json_tool_result(
        await open_page.ainvoke(
            {
                "url": (
                    "data:text/html;charset=utf-8,"
                    "%3Ctitle%3EBOXTEAM_BROWSER_DISCOVERY_E2E%3C/title%3E"
                    "%3Cmain%3Ehttps%3A%2F%2Fbrowser-discovery.example%2F%3C/main%3E"
                ),
                "forceNew": True,
            }
        )
    )
    page_id = str(opened["pageId"])

    message_response = await integration_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "你看看当前后台连接的浏览器网站是什么。"
            },
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]
    job_data = await wait_for_job_done(
        integration_client, job_id, max_attempts=120
    )
    assert job_data["status"] in {"completed", "succeeded"}

    traces_response = await integration_client.get(
        f"/api/v1/sessions/{session_id}/traces"
    )
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]["items"]
    tool_starts = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_start"
    ]
    tool_names = [get_trace_payload(trace).get("tool_name") for trace in tool_starts]
    assert "listBrowserPage" in tool_names
    assert all(
        not (
            get_trace_payload(trace).get("tool_name") in {"grep", "read_file"}
            and "browser-manager/browsers.json"
            in json.dumps(get_trace_payload(trace).get("args", {}), ensure_ascii=False)
        )
        for trace in tool_starts
    )

    messages_response = await integration_client.get(
        f"/api/v1/sessions/{session_id}/messages"
    )
    assert messages_response.status_code == 200
    answer = last_assistant_message(messages_response.json()["data"]["items"])
    assert (
        "BOXTEAM_BROWSER_DISCOVERY_E2E" in answer
        or "browser-discovery.example" in answer
    )

    listed = _json_tool_result(
        await create_list_browser_page_tool(context).ainvoke({})
    )
    assert any(page["pageId"] == page_id for page in listed["pages"])


@pytest.mark.asyncio
async def test_playwright_timeout_recovery_allows_immediate_page_actions(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
):
    context = browser_context
    open_page = create_open_browser_page_tool(context)
    run_playwright_code = create_run_playwright_code_tool(context)
    read_page = create_read_page_tool(context)
    screenshot_page = create_screenshot_page_tool(context)

    opened = _json_tool_result(
        await open_page.ainvoke({"url": _browser_test_data_url(), "forceNew": True})
    )
    page_id = str(opened["pageId"])

    timeout_result = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "await new Promise(() => undefined);",
                "timeoutMs": 25,
            }
        )
    )
    assert timeout_result["status"] == "error"
    assert timeout_result["code"] == "browser_tool_timeout"
    assert timeout_result["retryable"] is True
    assert timeout_result["recovery"] == "page_reset"

    keyboard_result = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "await page.keyboard.press('r'); return await page.title();",
                "timeoutMs": 3000,
            }
        )
    )
    assert keyboard_result["result"] == "BoxTeam Browser Tool Test"

    summary = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    assert "BoxTeam Browser Tool Test" in str(summary["summary"])

    screenshot = _json_tool_result(
        await screenshot_page.ainvoke({"pageId": page_id})
    )
    assert str(screenshot["mime_type"]) == "image/png"


@pytest.mark.asyncio
async def test_new_browser_page_allows_minimal_actions_without_explicit_timeout(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
):
    context = browser_context
    open_page = create_open_browser_page_tool(context)
    navigate_page = create_navigate_page_tool(context)
    run_playwright_code = create_run_playwright_code_tool(context)
    read_page = create_read_page_tool(context)
    screenshot_page = create_screenshot_page_tool(context)

    opened = _json_tool_result(
        await open_page.ainvoke({"url": _browser_test_data_url(), "forceNew": True})
    )
    page_id = str(opened["pageId"])

    navigated = _json_tool_result(
        await navigate_page.ainvoke(
            {
                "pageId": page_id,
                "type": "url",
                "url": _browser_test_data_url(),
            }
        )
    )
    assert navigated["pageId"] == page_id
    assert isinstance(navigated["activePageId"], str)
    assert "page_id" not in navigated

    title = _json_tool_result(
        await run_playwright_code.ainvoke(
            {"pageId": page_id, "code": "return await page.title();"}
        )
    )
    assert title["result"] == "BoxTeam Browser Tool Test"

    keyboard = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "await page.keyboard.press('r'); return 'keyboard-ok';",
            }
        )
    )
    assert keyboard["result"] == "keyboard-ok"

    summary = _json_tool_result(await read_page.ainvoke({"pageId": page_id}))
    assert "BoxTeam Browser Tool Test" in str(summary["summary"])

    screenshot = _json_tool_result(
        await screenshot_page.ainvoke({"pageId": page_id})
    )
    assert screenshot["mime_type"] == "image/png"

    with pytest.raises(BrowserRequestError) as selector_info:
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "await page.click('#focus');",
                "timeoutMs": 1000,
            }
        )
    assert selector_info.value.status == 500
    assert selector_info.value.code is None
    assert "Timeout 750ms exceeded" in str(selector_info.value)

    recovered_keyboard = _json_tool_result(
        await run_playwright_code.ainvoke(
            {
                "pageId": page_id,
                "code": "await page.keyboard.press('r'); return 'recovered';",
            }
        )
    )
    assert recovered_keyboard["result"] == "recovered"
    recovered_summary = _json_tool_result(
        await read_page.ainvoke({"pageId": page_id})
    )
    assert "BoxTeam Browser Tool Test" in str(recovered_summary["summary"])
    recovered_screenshot = _json_tool_result(
        await screenshot_page.ainvoke({"pageId": page_id})
    )
    assert recovered_screenshot["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_browser_open_failure_is_exposed_as_failed_resource(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
):
    del browser_manager_processes
    session_id = browser_context.session_id
    open_page = create_open_browser_page_tool(browser_context)

    with pytest.raises(RuntimeError) as exc_info:
        await open_page.ainvoke(
            {"url": "http://127.0.0.1:9/boxteam-browser-open-failure"}
        )

    assert "page.goto" in str(exc_info.value)
    assert "ERR_UNSAFE_PORT" in str(exc_info.value)
    resources_response = await integration_client.get(
        f"/api/v1/sessions/{session_id}/resources"
    )
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    failed_browser = next(resource for resource in resources if resource["kind"] == "browser")
    assert failed_browser["status"] == "failed"
    assert failed_browser["available_actions"] == []
    assert "page.goto" in failed_browser["metadata"]["error_message"]

    deleted = await _detach_delete(integration_client, failed_browser["resource_id"], browser_context)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_browser_delete_while_attached_keeps_manager_alive(
    browser_manager_processes: tuple[int, int],
    integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext,
):
    backend_port, _frontend_port = browser_manager_processes
    context = browser_context
    open_page = create_open_browser_page_tool(context)
    opened = _json_tool_result(
        await open_page.ainvoke({"url": _browser_test_data_url(), "forceNew": True})
    )
    page_id = str(opened["pageId"])

    grant = await _grant(integration_client, page_id, browser_context)
    workspace_id = grant["resource"]["workspace_id"]
    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser?workspace_id={workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": page_id, "grantToken": grant["grant_token"]}))
        attached = await _recv_ws_type(websocket, "attached")
        assert attached["browserId"] == page_id
        deleted = await _detach_delete(integration_client, page_id, browser_context)
        assert deleted.status_code == 200, deleted.text

    health_status, _ = await asyncio.to_thread(
        _read_http,
        f"http://127.0.0.1:{backend_port}/health",
    )
    assert health_status == 200


@pytest.mark.asyncio
async def test_real_node_agent_lock_rejection_preserves_owner_and_never_executes_callback(
    browser_context: CustomToolFactoryContext,
) -> None:
    opened = _json_tool_result(await create_open_browser_page_tool(browser_context).ainvoke({
        "url": _browser_test_data_url(), "forceNew": True,
    }))
    resource_id = opened["pageId"]
    owner = browser_context.browser_resource_owner
    resource = await owner.get_resource(resource_id)
    thread = ThreadRef(browser_context.session_id, browser_context.invocation_context.thread_binding.thread_id)
    arguments = {
        "expected_revision": resource["owner_revision"], "thread": thread,
        "holder_id": "request:lock-real", "operation_id": "lock-real",
    }
    with pytest.raises(BrowserRequestError) as invalid:
        await owner.perform(resource_id, action="agent-lock", payload={"locked": True, "owner_id": "user-real"}, **arguments)
    assert invalid.value.payload["operation_outcome"] == "failed"
    unchanged = await owner.get_resource(resource_id)
    assert unchanged["reconciliation_status"] == "verified"
    assert unchanged["agent_access_locked"] is False
    assert owner._leases.leases_for_resource(resource_id) == []
    await owner.perform(resource_id, action="agent-lock", payload={"locked": True, "owner_id": "user_real_lock_owner"}, **arguments)
    run = create_run_playwright_code_tool(browser_context)
    with pytest.raises(BrowserRequestError) as rejected:
        await run.ainvoke({
            "pageId": resource_id,
            "code": "return await page.evaluate(() => window.rejectedCallbackCount = (window.rejectedCallbackCount || 0) + 1);",
        })
    assert rejected.value.status == 423
    assert rejected.value.payload["operation_outcome"] == "failed"
    assert (await owner.get_resource(resource_id))["reconciliation_status"] == "verified"
    assert owner._leases.leases_for_resource(resource_id) == []
    await owner.perform(resource_id, action="agent-lock", payload={"locked": False, "owner_id": "user_real_lock_owner"}, **arguments)
    executed = _json_tool_result(await run.ainvoke({
        "pageId": resource_id,
        "code": "return await page.evaluate(() => window.rejectedCallbackCount = (window.rejectedCallbackCount || 0) + 1);",
    }))
    assert executed["result"] == 1
    promoted = await owner.command(
        resource_id, action="promote", expected_revision=resource["owner_revision"],
        holder_id="request:promote-real", operation_id="promote-real",
    )
    assert promoted["retention_scope"] == "workspace"
    assert promoted["reconciliation_status"] == "verified"
    await owner.command(resource_id, action="stop", expected_revision=promoted["owner_revision"],
                        holder_id="request:stop-real", operation_id="stop-real")
