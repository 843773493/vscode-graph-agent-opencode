from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Generator
from contextlib import ExitStack
from pathlib import Path
from shutil import copytree
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import httpx
import pytest
import websockets
from fastapi import FastAPI, HTTPException

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
from app.main import workspace_api, workspace_http_exception_handler
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.api_waiters import wait_for_job_done
from tests.support.messages import last_assistant_message
from tests.support.processes.workspace import terminate_process
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
BROWSER_CONTROL_SKILL_NAME = "browser-control"


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
        if message.get("type") == "error":
            raise AssertionError(f"等待 {expected_type} 时 Browser 拒绝消息: {message}")
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
    workspace_root = prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS,
    )
    copytree(
        project_root / "resources" / "skills" / BROWSER_CONTROL_SKILL_NAME,
        workspace_root / ".boxteam" / "skills" / BROWSER_CONTROL_SKILL_NAME,
        dirs_exist_ok=True,
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
    workspace = Path(integration_workspace_root_path).resolve()
    with ExitStack() as lifetime:
        browser = start_browser_manager_processes(
            workspace_root=workspace, backend_port=backend_port,
            artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir,
            frontend_port=frontend_port, backend_host="127.0.0.1",
        )
        lifetime.callback(close_browser_manager_processes, browser)
        terminal = start_terminal_backend(backend_port=backend_port + 2, workspace_root=str(workspace), artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
        lifetime.callback(terminate_process, terminal)
        yield backend_port, frontend_port


@pytest.fixture
def browser_application() -> FastAPI:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(HTTPException, workspace_http_exception_handler)
    application.include_router(workspace_api, prefix="/api/v1")
    return application


@pytest.fixture
async def browser_runtime(
    browser_manager_processes: tuple[int, int],
    integration_workspace_root_path: str,
    integration_startup_mount: MountedWorkspace,
    browser_application: FastAPI,
) -> AsyncIterator[WorkspaceToolRuntime]:
    del browser_manager_processes
    async with workspace_tool_runtime(
        Path(integration_workspace_root_path), application=browser_application,
        startup_mount=integration_startup_mount,
    ) as runtime:
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
async def test_checkpoint_failure_reports_stage_without_publishing_native_path(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    response = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert response.status_code == 200, response.text
    resource = response.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    checkpoint = root / ".boxteam/browser-manager/checkpoints" / f"{browser_id}.json"
    checkpoint.mkdir(parents=True)
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    failures: list[BrowserRequestError] = []
    for mode in ("sync", "async"):
        with pytest.raises(BrowserRequestError) as caught:
            if mode == "sync":
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/freeze", {})
            else:
                await transport._json_request("POST", f"/api/browsers/{browser_id}/freeze", {})
        failure = caught.value
        assert failure.status == 500
        assert failure.code in {"EISDIR", "EPERM", "EEXIST"}
        assert failure.payload["error"] == f"写入浏览器检查点失败: browser_id={browser_id}, stage=文件发布, code={failure.code}"
        assert failure.payload["operation_outcome"] == "failed"
        assert str(root) not in json.dumps(failure.payload)
        failures.append(failure)
    command = {"action": "freeze", "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    failed = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
    assert failed.status_code == 500, failed.text
    assert failed.json()["detail"] == str(f"BrowserRequestError: {failures[0]}")
    assert failed.headers["X-Request-ID"]
    assert list(checkpoint.iterdir()) == []
    assert sorted(item.name for item in checkpoint.parent.iterdir()) == [checkpoint.name]
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert observed["metadata"]["resource_transition_error"] == failures[0].payload["error"]
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert record["resource_transition_error"] == failures[0].payload["error"]
    assert str(root) not in json.dumps(record)
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / f"checkpoint-failure-{time.time_ns()}.json").write_text(json.dumps({
        "transport_payloads": [failure.payload for failure in failures],
        "api_status": failed.status_code, "api_body": failed.json(),
        "request_id": failed.headers["X-Request-ID"],
        "owner_projection": observed, "node_record": record,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint.rmdir()
    frozen = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["data"]["browser"]["metadata"]["resource_state"] == "frozen"
    assert checkpoint.is_file()
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert "[browser-state-store] 写入浏览器检查点失败" in diagnostics
    assert str(checkpoint) in diagnostics
    assert "文件发布" in diagnostics
    assert failures[0].code in diagnostics
    awake = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "wake"})
    assert awake.status_code == 200, awake.text
    assert awake.json()["data"]["browser"]["metadata"]["resource_transition_error"] is None


@pytest.mark.asyncio
async def test_download_failure_publishes_stage_without_native_storage(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    browser_id = created.json()["data"]["resource_id"]
    root = Path(container.workspace_root)
    destination = root / ".boxteam/browser-manager/downloads" / browser_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"existing-directory-conflict")
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    code = """
const started = page.waitForEvent('download');
await page.evaluate(() => {
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(new Blob(['download-body'], {type: 'text/plain'}));
  anchor.download = 'report.txt';
  anchor.click();
});
await started;
return 'download-started';
"""
    try:
        await transport.perform(browser_id, "run", {"code": code}, actor="agent")
        deadline = time.monotonic() + 10
        while True:
            raw = await transport._json_request("GET", f"/api/browsers/{browser_id}")
            failure = raw["data"].get("download_error")
            if failure:
                break
            assert time.monotonic() < deadline, "真实下载事件未发布失败状态"
            await asyncio.sleep(0.05)
        assert f"保存浏览器下载失败: browser_id={browser_id}, download_id=download_" in failure
        assert "stage=目录创建" in failure
        assert any(f"code={code}" in failure for code in ("EEXIST", "ENOTDIR", "EPERM"))
        assert str(root) not in json.dumps(raw)
        assert raw["data"].get("downloads", []) == []
        assert destination.read_bytes() == b"existing-directory-conflict"
        observed = await integration_client.get(f"/api/v1/resources/{browser_id}")
        assert observed.status_code == 200, observed.text
        assert observed.json()["data"]["metadata"]["download_error"] == failure
        assert observed.headers["X-Request-ID"] == observed.json()["request_id"]
        records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
        assert record["download_error"] == failure
        assert str(root) not in json.dumps(record)
        artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
        diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
        assert "[browser-state-store] 保存浏览器下载失败" in diagnostics
        assert str(destination) in diagnostics
        (artifacts / f"download-failure-{time.time_ns()}.json").write_text(json.dumps({
            "raw_node_response": raw, "resource_response": observed.json(), "node_record": record,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        destination.unlink()
        await transport.perform(browser_id, "run", {"code": code}, actor="agent")
        deadline = time.monotonic() + 10
        while True:
            recovered = (await transport._json_request("GET", f"/api/browsers/{browser_id}"))["data"]
            if recovered.get("downloads"):
                break
            assert time.monotonic() < deadline, "目录修复后真实下载未完成"
            await asyncio.sleep(0.05)
        saved, = recovered["downloads"]
        assert saved["status"] == "completed" and saved["filename"] == "report.txt"
        assert (destination / f"{saved['download_id']}-{saved['filename']}").read_bytes() == b"download-body"
    finally:
        if destination.is_file():
            destination.unlink()
        deleted = await _detach_delete(integration_client, browser_id, browser_context)
        assert deleted.status_code == 200, deleted.text


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

    skill_root = Path(integration_workspace_root_path) / ".boxteam" / "skills"
    assert {entry.name for entry in skill_root.iterdir() if entry.is_dir()} == {
        BROWSER_CONTROL_SKILL_NAME,
        "large-test-output",
        "test-tool-2",
    }
    skill_path = skill_root / BROWSER_CONTROL_SKILL_NAME / "SKILL.md"
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
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_injected_page_recovery_failure_keeps_native_cause_in_diagnostics(
    mode: str, integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext, browser_runtime: WorkspaceToolRuntime,
    browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    native_path = str(root / ".boxteam/browser-manager" / f"injected-profile-{mode}")
    reason = f"injected EACCES: {native_path}"
    # 只替换第三方 Playwright 的建页边界；超时、队列、HTTP和持久化均走产品链路。
    code = (
        f"context.newPage = async () => {{ throw new Error({json.dumps(reason)}); }};"
        "await new Promise(() => undefined);"
    )
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    with pytest.raises(BrowserRequestError) as caught:
        payload = {"code": code, "timeoutMs": 25}
        if mode == "sync":
            await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/run", payload)
        else:
            await transport._json_request("POST", f"/api/browsers/{browser_id}/run", payload)
    failure = caught.value
    assert failure.status == 408
    assert failure.code == "browser_tool_timeout"
    assert failure.payload["retryable"] is True
    assert failure.payload["timeout_ms"] == 25
    assert failure.payload["recovery"] == "page_reset_failed"
    assert failure.payload["recovery_error"] == "浏览器页面重置失败: stage=reset_page"
    assert failure.payload["operation_outcome"] == "failed"
    assert str(root) not in json.dumps(failure.payload)

    # 恢复屏障失败应阻止新脚本，不把同一原生异常重新公开，也不伪报重试成功。
    command = {
        "action": "run", "expected_revision": resource["owner_revision"],
        "thread": resource["source_thread"],
        "payload": {"code": "return 'must-not-execute';", "timeoutMs": 100},
    }
    blocked = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
    assert blocked.status_code == 500, blocked.text
    assert "stage=reset_page" in blocked.text
    assert browser_id in blocked.text
    assert str(root) not in blocked.text
    assert blocked.headers["X-Request-ID"]
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert record["last_operation"]["status"] == "failed"
    assert str(root) not in json.dumps(record)
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert "[browser-runtime] 页面超时恢复失败" in diagnostics
    assert reason in diagnostics
    (artifacts / f"timeout-recovery-failure-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "transport_payload": failure.payload, "api_status": blocked.status_code,
        "api_body": blocked.json(), "request_id": blocked.headers["X-Request-ID"],
        "owner_projection": observed, "node_record": record,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_injected_registration_failure_shares_safe_http_state_and_record(
    mode: str, integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext, browser_runtime: WorkspaceToolRuntime,
    browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/native-profile'}"
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    # 通过脚本入口仅替换第三方CDP连接创建；真实注册、事件、HTTP与落盘保持产品实现。
    installed = await transport.perform(browser_id, "run", {"code": (
        "context.__testOriginalNewCDPSession = context.newCDPSession.bind(context);"
        f"context.newCDPSession = async () => {{ throw new Error({json.dumps(reason)}); }};"
        "return 'installed';"
    )}, actor="agent")
    assert installed["result"] == "installed"
    grant = await _grant(integration_client, browser_id, browser_context)
    states: list[dict[str, object]] = []
    async with websockets.connect(
        f"ws://127.0.0.1:{browser_manager_processes[0]}/browser?workspace_id={container.workspace_id}"
    ) as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        assert (await _recv_ws_type(websocket, "attached"))["browserId"] == browser_id
        with pytest.raises(BrowserRequestError) as caught:
            payload = {"type": "new_tab", "url": "about:blank"}
            if mode == "sync":
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/navigate", payload)
            else:
                await transport._json_request("POST", f"/api/browsers/{browser_id}/navigate", payload)
        failure = caught.value
        assert failure.status == 500
        assert "stage=register_page" in str(failure)
        assert browser_id in str(failure)
        assert failure.payload["operation_outcome"] == "failed"
        assert str(root) not in json.dumps(failure.payload)
        async with asyncio.timeout(8):
            while True:
                state = (await _recv_ws_type(websocket, "state"))["state"]
                states.append(state)
                if "stage=register_page" in str(state.get("error_message")):
                    break
        assert all(str(root) not in json.dumps(state) for state in states)
        assert state["status"] == "running"
        assert len(state["pages"]) == 1
        command = {
            "action": "navigate", "payload": payload,
            "expected_revision": resource["owner_revision"], "thread": resource["source_thread"],
        }
        api_failure = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
        assert api_failure.status_code == 500, api_failure.text
        assert "stage=register_page" in api_failure.text
        assert str(root) not in api_failure.text
        assert api_failure.headers["X-Request-ID"]
        await websocket.send(json.dumps({"type": "detach"}))
        assert (await _recv_ws_type(websocket, "detached"))["browserId"] == browser_id

    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert "stage=register_page" in record["error_message"]
    assert record["last_operation"]["status"] == "failed"
    assert str(root) not in json.dumps(record)
    restored = await transport.perform(browser_id, "run", {"code": (
        "context.newCDPSession = context.__testOriginalNewCDPSession;"
        "delete context.__testOriginalNewCDPSession; return 'restored';"
    )}, actor="agent")
    assert restored["result"] == "restored"
    retried = await transport.perform(browser_id, "navigate", payload, actor="agent")
    assert retried["created_page_id"] != resource["metadata"]["active_page_id"]
    assert len(retried["browser"]["pages"]) == 2
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert "[browser-session] 注册浏览器标签页失败" in diagnostics
    assert reason in diagnostics
    (artifacts / f"registration-failure-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "transport_payload": failure.payload, "states": states,
        "api_body": api_failure.json(), "request_id": api_failure.headers["X-Request-ID"],
        "owner_projection": observed, "node_record": record, "retried": retried,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "mode"), [
    ("create_page", "sync"), ("close_page", "api"),
    ("Page.stopLoading", "websocket"), ("Page.captureScreenshot", "websocket"),
])
async def test_page_control_failure_keeps_native_details_in_diagnostics(
    stage: str, mode: str, integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_runtime: WorkspaceToolRuntime, browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    prepared = await transport.perform(browser_id, "navigate", {"type": "new_tab", "url": "about:blank"}, actor="agent")
    page_id = prepared["created_page_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/private-page-control-profile'}"
    failure = f"throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}});"
    if stage == "create_page":
        code = "const original = context.newPage; context.newPage = async function (...args) { context.newPage = original; " + failure + " };"
    elif stage == "close_page":
        code = "const original = page.close; page.close = async function (...args) { page.close = original; " + failure + " };"
    else:
        code = (
            "const cdp = await context.newCDPSession(page); const proto = Object.getPrototypeOf(cdp);"
            " const original = proto.send; await cdp.detach(); proto.send = async function (method, params) {"
            f" if (method === '{stage}') {{ proto.send = original; {failure} }}"
            " return await original.call(this, method, params); };"
        )
    navigation = {"type": "new_tab", "url": "about:blank"} if stage == "create_page" else {"type": "close_tab", "tab_id": page_id}
    api_command = {"action": "navigate", "payload": navigation, "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    ws_command = {"type": "captureScreenshot", "browserId": browser_id} if stage == "Page.captureScreenshot" else {"type": "command", "name": "stop", "browserId": browser_id}
    grant = await _grant(integration_client, browser_id, browser_context)
    async with websockets.connect(f"ws://127.0.0.1:{browser_manager_processes[0]}/browser?workspace_id={container.workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        assert (await _recv_ws_type(websocket, "attached"))["browserId"] == browser_id
        # attach的首帧已完成，故障只在接下来的用户操作原生边界触发一次。
        await transport.perform(browser_id, "run", {"code": code}, actor="agent")
        if mode == "sync":
            with pytest.raises(BrowserRequestError) as caught:
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/navigate", navigation)
            assert caught.value.code == "EACCES"
            assert caught.value.status == 500
            receipt = caught.value.payload
        elif mode == "api":
            response = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=api_command)
            assert response.status_code == 500, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            receipt = response.json()
        else:
            await websocket.send(json.dumps(ws_command))
            receipt = await _recv_ws_type(websocket, "error")
            assert receipt["code"] == "EACCES"
        assert f"stage={stage}" in json.dumps(receipt)
        assert str(root) not in json.dumps(receipt)
        state = await transport.get_resource(browser_id)
        assert state["status"] == "running"
        assert state["reconciliation_status"] == "verified"
        assert state["active_page_id"] == page_id
        assert len(state["pages"]) == 2
        assert state["last_operation"]["status"] == "failed"
        if stage == "Page.captureScreenshot":
            # 并发截图不立即落盘；通过原控制入口发布状态，不替换失败操作事实。
            await transport.perform(browser_id, "resource-policy", {"policy": state["resource_policy"]}, actor="agent")
        records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
        assert record["last_operation"]["operation_id"] == state["last_operation"]["operation_id"]
        assert f"stage={stage}" in record["last_operation"]["error"]
        assert str(root) not in json.dumps(record)
        if mode == "websocket":
            await websocket.send(json.dumps(ws_command))
            retried = await _recv_ws_type(websocket, "screenshotResult" if stage == "Page.captureScreenshot" else "commandResult")
            if stage == "Page.captureScreenshot":
                assert retried["data"].startswith("iVBORw0KGgo")
        else:
            retried = await transport.perform(browser_id, "navigate", navigation, actor="agent")
            assert retried["operation"]["status"] == "completed"
            assert len(retried["browser"]["pages"]) == (3 if stage == "create_page" else 1)
        await websocket.send(json.dumps({"type": "detach"}))
        assert (await _recv_ws_type(websocket, "detached"))["browserId"] == browser_id
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    assert reason in "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    (artifacts / f"page-control-failure-{stage}-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": receipt, "node_record": record, "owner_projection": observed, "retried": retried,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "close", "stream_close"])
async def test_artifact_stream_and_close_failures_keep_server_available(
    mode: str, integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id, "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    browser_id = created.json()["data"]["resource_id"]
    saved = _json_tool_result(await create_screenshot_page_tool(browser_context).ainvoke({"pageId": browser_id}))
    artifact_id = saved["screenshot_id"]
    root = Path(container.workspace_root)
    file = root / ".boxteam/browser-manager/screenshots" / f"{browser_id}-{artifact_id}.png"
    expected = file.read_bytes()
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    # 一次性注入文件句柄/流故障；HTTP、pipeline、响应销毁及真实句柄关闭均走产品链路。
    await transport.perform(browser_id, "run", {"code": f"""
const fs = (await import('node:fs')).default;
const {{syncBuiltinESMExports}} = await import('node:module');
const {{Readable}} = await import('node:stream');
const originalOpen = fs.promises.open;
context.__artifactOriginalOpen = originalOpen;
fs.promises.open = async (...args) => {{
  if (args[0] !== {json.dumps(str(file))}) return originalOpen(...args);
  fs.promises.open = originalOpen; syncBuiltinESMExports();
  const handle = await originalOpen(...args);
  if ({json.dumps(mode)}.startsWith('stream')) {{
    handle.createReadStream = () => Readable.from((async function* () {{
      yield Buffer.from('partial');
      await new Promise(resolve => setTimeout(resolve, 25));
      throw Object.assign(new Error('injected stream EIO ' + args[0]), {{code: 'EIO'}});
    }})());
  }}
  const originalClose = handle.close.bind(handle);
  handle.close = async () => {{
    await originalClose();
    console.error('artifact-test-original-handle-closed', {json.dumps(artifact_id)});
    if ({json.dumps(mode)}.includes('close')) throw Object.assign(
      new Error('injected close EBADF ' + args[0]), {{code: 'EBADF'}});
  }};
  return handle;
}};
syncBuiltinESMExports(); return 'armed';
"""}, actor="agent")
    url = f"http://127.0.0.1:{browser_manager_processes[0]}{saved['screenshot_url']}?workspace_id={container.workspace_id}"
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if mode.startswith("stream"):
                with pytest.raises(httpx.RemoteProtocolError):
                    await client.get(url)
            else:
                completed = await client.get(url)
                assert completed.status_code == 200 and completed.content == expected
            deadline = time.monotonic() + 8
            while True:
                diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
                marker = f"读取浏览器产物失败: browser_id={browser_id}, artifact_id={artifact_id}, kind=screenshot, stage="
                stage, code = ("stream", "EIO") if mode.startswith("stream") else ("close", "EBADF")
                if marker + f"{stage}, code={code}" in diagnostics:
                    break
                assert time.monotonic() < deadline, "未收到流/关闭失败诊断"
                await asyncio.sleep(0.05)
            assert f"artifact-test-original-handle-closed {artifact_id}" in diagnostics
            assert "[browser-backend] 响应开始后传输失败" in diagnostics
            if mode.startswith("stream"):
                assert "injected stream EIO " + str(file) in diagnostics
            if "close" in mode:
                assert "injected close EBADF " + str(file) in diagnostics
            if mode == "stream_close":
                assert "AggregateError: 产物读取及关闭失败" in diagnostics
            retried = await client.get(url)
            assert retried.status_code == 200 and retried.content == expected
        assert (await transport.get_resource(browser_id))["reconciliation_status"] == "verified"
    finally:
        await transport.perform(browser_id, "run", {"code": """
const fs = (await import('node:fs')).default;
fs.promises.open = context.__artifactOriginalOpen;
delete context.__artifactOriginalOpen;
(await import('node:module')).syncBuiltinESMExports(); return 'restored';
"""}, actor="agent")
        deleted = await _detach_delete(integration_client, browser_id, browser_context)
        assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["screenshot", "download"])
async def test_browser_artifact_read_failure_reports_identity_and_allows_retry(
    kind: str, integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id, "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    browser_id = created.json()["data"]["resource_id"]
    root = Path(container.workspace_root)
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    if kind == "screenshot":
        saved = _json_tool_result(await create_screenshot_page_tool(browser_context).ainvoke({"pageId": browser_id}))
        artifact_id = saved["screenshot_id"]
        file = root / ".boxteam/browser-manager/screenshots" / f"{browser_id}-{artifact_id}.png"
        route = saved["screenshot_url"]
        expected_type = "image/png"
    else:
        await transport.perform(browser_id, "run", {"code": """
const started = page.waitForEvent('download');
await page.evaluate(() => {
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(new Blob(['真实下载正文'], {type: 'text/plain'}));
  anchor.download = '下载 文件.txt'; anchor.click();
});
await started;
"""}, actor="agent")
        deadline = time.monotonic() + 10
        while True:
            state = await transport.get_resource(browser_id)
            if state.get("downloads"):
                break
            assert time.monotonic() < deadline, "真实下载未完成"
            await asyncio.sleep(0.05)
        saved, = state["downloads"]
        artifact_id = saved["download_id"]
        file = root / ".boxteam/browser-manager/downloads" / browser_id / f"{artifact_id}-{saved['filename']}"
        route = f"/api/browsers/{browser_id}/downloads/{artifact_id}"
        expected_type = "application/octet-stream"
    expected = file.read_bytes()
    if kind == "screenshot":
        assert expected.startswith(b"\x89PNG")
    else:
        assert expected.decode() == "真实下载正文"
    state_file = root / ".boxteam/browser-manager/browsers.json"
    state_before = state_file.read_bytes()
    url = f"http://127.0.0.1:{browser_manager_processes[0]}{route}?workspace_id={container.workspace_id}"
    receipts: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=8) as client:
        for mode, status, code in [
            ("parent_file", 500, "ENOTDIR"), ("missing", 404, "browser_artifact_not_found"),
            ("directory", 500, "EISDIR"), ("permission", 500, "EACCES"),
        ]:
            if mode == "permission" and os.name == "nt":
                continue  # Windows权限失败由目标ACL验收，不用chmod冒充。
            if mode == "missing":
                file.unlink()
            elif mode == "parent_file":
                parked = file.parent.with_name(f"{file.parent.name}-artifact-read-backup")
                file.parent.rename(parked)
                file.parent.write_bytes(b"not-a-directory")
            elif mode == "directory":
                file.unlink()
                file.mkdir()
            else:
                file.chmod(0)
            try:
                failed = await client.get(url)
                assert failed.status_code == status, failed.text
                receipt = failed.json()
                assert receipt["code"] == code
                assert f"browser_id={browser_id}" in receipt["error"]
                assert f"artifact_id={artifact_id}" in receipt["error"]
                assert f"kind={kind}" in receipt["error"]
                assert str(root) not in failed.text
                assert state_file.read_bytes() == state_before
                receipts.append(receipt)
            finally:
                if mode == "missing":
                    file.write_bytes(expected)
                elif mode == "parent_file":
                    file.parent.unlink()
                    parked.rename(file.parent)
                elif mode == "directory":
                    file.rmdir()
                    file.write_bytes(expected)
                else:
                    file.chmod(0o600)
            recovered = await client.get(url)
            assert recovered.status_code == 200, recovered.text
            assert recovered.content == expected
            assert recovered.headers["content-type"] == expected_type
            assert int(recovered.headers["content-length"]) == len(expected)
            if kind == "download":
                assert recovered.headers["content-disposition"] == "attachment; filename*=UTF-8''" + quote(saved["filename"], safe="")
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert str(file) in diagnostics and "ENOTDIR" in diagnostics
    assert (await transport.get_resource(browser_id))["reconciliation_status"] == "verified"
    (artifacts / f"artifact-read-failure-{kind}-{time.time_ns()}.json").write_text(json.dumps({
        "receipts": receipts, "artifact_id": artifact_id, "byte_length": len(expected),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "mode", "arguments"), [
    ("dismiss", "sync", {"acceptModal": False}),
    ("accept", "async", {}),
    ("accept", "tool", {"promptText": "重试后的输入"}),
    ("dismiss", "api", {"acceptModal": False}),
])
async def test_dialog_failure_keeps_native_details_in_diagnostics_and_allows_retry(
    method: str, mode: str, arguments: dict[str, object], integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext, browser_runtime: WorkspaceToolRuntime,
    browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/private-dialog-profile'}"
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    # 由真实页面生成对话框；仅第三方 accept/dismiss 方法一次失败，不替产品写 pending 状态。
    opened = await transport.perform(browser_id, "run", {"code": (
        "page.once('dialog', dialog => {"
        f" const original = dialog.{method}; dialog.{method} = async function (...args) {{"
        f" this.{method} = original; throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}}); }};"
        " }); await page.evaluate(() => { window.dialogResult = window.prompt('原生提示', '原输入'); });"
    )}, actor="agent")
    assert opened["browser"]["pending_dialog"]["message"] == "原生提示"
    page_id = opened["browser"]["active_page_id"]
    handle_dialog = create_handle_dialog_tool(browser_context)
    if mode == "api":
        failed = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
            "expected_revision": resource["owner_revision"], "thread": resource["source_thread"],
            "action": "dialog", "payload": arguments,
        })
        assert failed.status_code == 500, failed.text
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
        receipt = failed.json()
    else:
        with pytest.raises(BrowserRequestError) as caught:
            if mode == "sync":
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/dialog", arguments)
            elif mode == "async":
                await transport.perform(browser_id, "dialog", arguments, actor="agent")
            else:
                await handle_dialog.ainvoke({"pageId": browser_id, **arguments})
        assert caught.value.status == 500 and caught.value.code == "EACCES"
        receipt = caught.value.payload
    assert f"stage=dialog.{method}" in json.dumps(receipt)
    assert f"page_id={page_id}" in json.dumps(receipt)
    assert str(root) not in json.dumps(receipt)
    state = await transport.get_resource(browser_id)
    assert state["pending_dialog"] == opened["browser"]["pending_dialog"]
    assert state["last_operation"]["status"] == "failed"
    assert f"stage=dialog.{method}" in state["last_operation"]["error"]
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert record["last_operation"] == state["last_operation"]
    assert str(root) not in json.dumps(record)
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert str(root) not in json.dumps(observed)
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    retried = _json_tool_result(await handle_dialog.ainvoke({"pageId": browser_id, **arguments}))
    assert retried["summary"] == f"已{'取消' if method == 'dismiss' else '接受'}对话框: 原生提示"
    assert (await transport.get_resource(browser_id))["pending_dialog"] is None
    result = await transport.perform(browser_id, "run", {"code": "return await page.evaluate(() => window.dialogResult);"}, actor="agent")
    assert result["result"] == (None if method == "dismiss" else arguments.get("promptText", ""))
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert reason in diagnostics and f"dialog.{method}" in diagnostics
    (artifacts / f"dialog-failure-{method}-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": receipt, "state": state, "node_record": record,
        "owner_projection": observed, "retried": retried, "result": result,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
async def test_file_selection_failure_keeps_native_path_in_diagnostics_and_allows_retry(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    missing = root / "不存在的上传 文件.txt"
    selected = root / "本机上传 文件.txt"
    selected.write_text("目标机器上的文件正文", encoding="utf-8")
    assert not missing.exists()
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    chooser_code = "await page.setContent('<input id=upload type=file>'); await page.locator('#upload').click();"
    opened = await transport.perform(browser_id, "run", {"code": chooser_code}, actor="agent")
    assert opened["browser"]["pending_file_chooser"] is True
    page_id = opened["browser"]["active_page_id"]
    handle_dialog = create_handle_dialog_tool(browser_context)
    receipts: list[dict[str, object]] = []
    payload = {"selectFiles": [str(missing)]}
    for mode in ("sync", "async", "tool", "api"):
        if mode == "api":
            failed = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
                "expected_revision": resource["owner_revision"], "thread": resource["source_thread"],
                "action": "dialog", "payload": payload,
            })
            assert failed.status_code == 500, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
            receipt = failed.json()
        else:
            with pytest.raises(BrowserRequestError) as caught:
                if mode == "sync":
                    await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/dialog", payload)
                elif mode == "async":
                    await transport.perform(browser_id, "dialog", payload, actor="agent")
                else:
                    await handle_dialog.ainvoke({"pageId": browser_id, **payload})
            assert caught.value.status == 500
            assert caught.value.code == "ENOENT"
            receipt = caught.value.payload
        assert "stage=select_files" in json.dumps(receipt)
        assert "code=ENOENT" in json.dumps(receipt)
        assert f"page_id={page_id}" in json.dumps(receipt)
        assert str(root) not in json.dumps(receipt)
        state = await transport.get_resource(browser_id)
        assert state["pending_file_chooser"] is True
        assert state["last_operation"]["status"] == "failed"
        records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
        assert record["last_operation"] == state["last_operation"]
        assert str(root) not in json.dumps(record)
        receipts.append(receipt)
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    retried = _json_tool_result(await handle_dialog.ainvoke({"pageId": browser_id, "selectFiles": [str(selected)]}))
    assert retried["summary"] == "已选择 1 个文件"
    assert (await transport.get_resource(browser_id))["pending_file_chooser"] is False
    read_file_code = "return await page.locator('#upload').evaluate(async e => ({name: e.files[0].name, text: await e.files[0].text()}));"
    native_file = await transport.perform(browser_id, "run", {"code": read_file_code}, actor="agent")
    assert native_file["result"] == {"name": selected.name, "text": selected.read_text(encoding="utf-8")}
    await transport.perform(browser_id, "run", {"code": chooser_code}, actor="agent")
    grant = await _grant(integration_client, browser_id, browser_context)
    async with websockets.connect(f"ws://127.0.0.1:{browser_manager_processes[0]}/browser?workspace_id={container.workspace_id}", proxy=None) as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "participantId": "user_file_selection_client", "grantToken": grant["grant_token"]}))
        await _recv_ws_type(websocket, "attached")
        await websocket.send(json.dumps({
            "type": "selectFiles", "browserId": browser_id, "clientOperationId": "upload-client-file",
            "files": [{"name": "客户端 文件.txt", "mimeType": "text/plain", "data": "5a6i5oi356uv5q2j5paH"}],
        }))
        completed = await _recv_ws_type(websocket, "operationAck")
        assert completed["clientOperationId"] == "upload-client-file"
        assert completed["operation"]["status"] == "completed"
        uploaded = await transport.perform(browser_id, "run", {"code": read_file_code}, actor="agent")
        assert uploaded["result"] == {"name": "客户端 文件.txt", "text": "客户端正文"}
        await websocket.send(json.dumps({"type": "detach"}))
        await _recv_ws_type(websocket, "detached")
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert str(missing) in diagnostics and "ENOENT" in diagnostics
    assert "stage: 'select_files'" in diagnostics
    (artifacts / f"file-selection-failure-{time.time_ns()}.json").write_text(json.dumps({
        "receipts": receipts, "node_record": record, "owner_projection": observed,
        "native_file": native_file, "uploaded_file": uploaded,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text
    assert (await integration_client.get(f"/api/v1/sessions/{browser_context.session_id}/resources")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "enabling"), [
    ("Network.setCacheDisabled", True), ("Network.setBypassServiceWorker", True),
    ("device_profile_goto", True), ("restore_device_page_url", True),
    ("Network.setBypassServiceWorker", False), ("Network.setCacheDisabled", False),
    ("multiple", False), ("stream_restart", False),
])
async def test_device_switch_failure_preserves_primary_and_cleanup_details(
    stage: str, enabling: bool, integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext, browser_runtime: WorkspaceToolRuntime,
    browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    root = Path(container.workspace_root)
    native_page = root / "设备 切换 #100%.html"
    native_page.write_text('<!doctype html><meta charset="utf-8"><title>设备切换页面</title>'
                          '<body><script>setInterval(() => {document.body.textContent = String(Date.now())}, 50)</script>', encoding="utf-8")
    url = native_page.as_uri()
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id, "url": url,
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    private_path = str(root / ".boxteam/browser-manager/private-device-profile")
    reason = f"injected EACCES: {private_path}"
    failure = f"throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}});"
    if stage in {"device_profile_goto", "multiple", "stream_restart"}:
        code = "let rollbackPending = false; const original = page.goto; page.goto = async function (...args) { page.goto = original; rollbackPending = true; " + failure + " };"
        if stage == "multiple":
            code += (
                " const viewport = page.setViewportSize; page.setViewportSize = async function (...args) {"
                " if (rollbackPending) { page.setViewportSize = viewport;"
                f" throw Object.assign(new Error({json.dumps(reason + '-rollback')}), {{code: 'EIO'}}); }}"
                " return await viewport.apply(this, args); };"
            )
    elif stage == "restore_device_page_url":
        code = (
            "const original = page.evaluate; page.evaluate = async function (fn, arg, ...args) {"
            " if (typeof arg === 'string' && arg.startsWith('file:')) { page.evaluate = original; " + failure + " }"
            " return await original.call(this, fn, arg, ...args); };"
        )
    else:
        code = ""
    if stage == "stream_restart":
        code += (
            " const cdp = await context.newCDPSession(page); const proto = Object.getPrototypeOf(cdp);"
            " const send = proto.send; await cdp.detach(); proto.send = async function (method, params) {"
            " if (method === 'Page.startScreencast' && rollbackPending) { proto.send = send;"
            f" throw Object.assign(new Error({json.dumps(reason + '-stream')}), {{code: 'EIO'}}); }}"
            " return await send.call(this, method, params); };"
        )
    elif stage.startswith("Network.") or stage == "multiple":
        method = "Network.setBypassServiceWorker" if stage == "multiple" else stage
        injected = failure if stage != "multiple" else f"throw Object.assign(new Error({json.dumps(reason + '-cleanup')}), {{code: 'EIO'}});"
        code += (
            " const cdp = await context.newCDPSession(page); const proto = Object.getPrototypeOf(cdp);"
            " const send = proto.send; await cdp.detach(); proto.send = async function (method, params) {"
            f" if (method === '{method}' && (params.cacheDisabled ?? params.bypass) === {json.dumps(enabling)}) {{ proto.send = send; {injected} }}"
            " return await send.call(this, method, params); };"
        )
    grant = await _grant(integration_client, browser_id, browser_context)
    expected_status = "verified" if enabling else "unknown"
    async with websockets.connect(f"ws://127.0.0.1:{browser_manager_processes[0]}/browser?workspace_id={container.workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        assert (await _recv_ws_type(websocket, "attached"))["browserId"] == browser_id
        await transport.perform(browser_id, "run", {"code": code}, actor="agent")
        command = {"type": "deviceProfile", "browserId": browser_id, "profileId": "pixel-7", "clientOperationId": "device-switch"}
        await websocket.send(json.dumps(command))
        receipt = await _recv_ws_type(websocket, "error")
        assert receipt["code"] == "EACCES"
        assert f"stage={'device_profile_goto' if stage in {'multiple', 'stream_restart'} else stage}" in receipt["message"]
        assert private_path not in json.dumps(receipt)
        state = await transport.get_resource(browser_id)
        assert state["status"] == "running"
        assert state["reconciliation_status"] == expected_status
        assert state["device_profile"] == "desktop"
        assert state["last_operation"]["status"] == "failed"
        if stage == "restore_device_page_url":
            # 页面已提交而replaceState失败，verified只证明运行时身份，不承诺撤销导航。
            assert state["url"].split("?")[0] == url
            assert "__boxteam_device_profile=" in state["url"]
        else:
            assert state["url"] == url
        records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
        assert record["last_operation"] == state["last_operation"]
        assert private_path not in json.dumps(record)
        if enabling:
            assert state["stream_metrics"]["profile"] is not None
            after_failure = time.time()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.1, deadline - time.monotonic()))
                if isinstance(raw, str):
                    assert json.loads(raw)["type"] != "error", raw
                    continue
                header_length = int.from_bytes(raw[:4], "big")
                frame = json.loads(raw[4:4 + header_length])
                assert frame["browserId"] == browser_id
                assert raw[4 + header_length:][:2] == b"\xff\xd8"
                await websocket.send(json.dumps({"type": "frameAck", "browserId": browser_id, "frameId": frame["frameId"]}))
                if frame["timestamp"] >= after_failure:
                    break
            else:
                raise AssertionError("设备回滚后没有收到新产生的画面帧")
            await websocket.send(json.dumps(command))
            ack = await _recv_ws_type(websocket, "operationAck")
            assert ack["clientOperationId"] == command["clientOperationId"]
            assert ack["operation"]["status"] == "completed"
            assert (await transport.get_resource(browser_id))["device_profile"] == "pixel-7"
        await websocket.send(json.dumps({"type": "detach"}))
        assert (await _recv_ws_type(websocket, "detached"))["browserId"] == browser_id
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == expected_status
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert reason in diagnostics
    if stage == "multiple":
        assert reason + "-cleanup" in diagnostics
        assert reason + "-rollback" in diagnostics
        assert "stage=Network.setBypassServiceWorker" in record["last_operation"]["error"]
        assert "stage=set_viewport" in record["last_operation"]["error"]
    if stage == "stream_restart":
        assert reason + "-stream" in diagnostics
        assert "stage=Page.startScreencast" in record["last_operation"]["error"]
    if expected_status == "unknown":
        rejected = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
            "action": "run", "payload": {"code": "throw new Error('must-not-execute');"},
            "expected_revision": observed["owner_revision"], "thread": resource["source_thread"],
        })
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"]
        await transport.stop_resource(browser_id)
        reconciled = await integration_client.post(f"/api/v1/resources/{browser_id}/stop", json={"expected_revision": observed["owner_revision"]})
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["data"]["reconciliation_status"] == "verified"
    (artifacts / f"device-switch-failure-{stage}-{enabling}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": receipt, "node_record": record, "owner_projection": observed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    "acquire_context", "create_page", "read_browser_defaults", "set_viewport", "Emulation.setDeviceMetricsOverride",
])
async def test_browser_startup_failure_keeps_native_details_in_diagnostics(
    stage: str, integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_runtime: WorkspaceToolRuntime, browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    command = {
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    }
    seeded = await integration_client.post("/api/v1/resources/browsers", json=command)
    assert seeded.status_code == 200, seeded.text
    seed_id = seeded.json()["data"]["resource_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/private-startup-profile'}"
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    failure = f"throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}});"
    # 只在第三方边界注入一次故障；新资源接纳、启动、清理和失败落盘仍由产品入口完成。
    if stage == "acquire_context":
        code = (
            "const proto = Object.getPrototypeOf(context.browser()); const original = proto.newContext;"
            " proto.newContext = async function (...args) { proto.newContext = original; " + failure + " };"
        )
    elif stage == "create_page":
        code = (
            "const proto = Object.getPrototypeOf(context); const original = proto.newPage;"
            " proto.newPage = async function (...args) { proto.newPage = original; " + failure + " };"
        )
    elif stage in {"read_browser_defaults", "set_viewport"}:
        method = "evaluate" if stage == "read_browser_defaults" else "setViewportSize"
        condition = "this !== originalPage"
        if stage == "read_browser_defaults":
            condition += " && String(args[0]).includes('navigator.userAgent')"
        code = (
            f"const originalPage = page; const proto = Object.getPrototypeOf(page); const original = proto.{method};"
            f" proto.{method} = async function (...args) {{ if ({condition}) {{ proto.{method} = original; {failure} }}"
            " return await original.call(this, ...args); };"
        )
    else:
        code = (
            "const cdp = await context.newCDPSession(page); const proto = Object.getPrototypeOf(cdp);"
            " const original = proto.send; await cdp.detach(); proto.send = async function (method, params) {"
            f" if (method === '{stage}') {{ proto.send = original; {failure} }}"
            " return await original.call(this, method, params); };"
        )
    await transport.perform(seed_id, "run", {"code": code}, actor="agent")
    failed = await integration_client.post("/api/v1/resources/browsers", json=command)
    assert failed.status_code == 500, failed.text
    assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
    assert f"stage={stage}" in failed.text
    assert str(root) not in failed.text
    listed = await integration_client.get(f"/api/v1/sessions/{browser_context.session_id}/resources")
    listed_failure, = [item for item in listed.json()["data"]["items"] if item["kind"] == "browser" and item["resource_id"] != seed_id]
    assert listed_failure["available_actions"] == []
    browser_id = listed_failure["resource_id"]
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["status"] == "failed"
    assert observed["reconciliation_status"] == "verified"
    assert "stop" in observed["available_actions"]
    assert f"stage={stage}" in observed["metadata"]["error_message"]
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    state = await transport.get_resource(browser_id)
    synchronous = await asyncio.to_thread(transport._json_request_sync, "GET", f"/api/browsers/{browser_id}", None)
    assert synchronous["data"]["error_message"] == state["error_message"]
    assert state["reconciliation_status"] == "verified"
    assert state["pages"] == []
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert record["error_message"] == state["error_message"]
    assert str(root) not in json.dumps(record)
    assert str(root) not in json.dumps(observed)
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert reason in diagnostics
    # 首次creating观察采用Node已核实的失败事实；仍通过显式stop关闭资源，再解除关联。
    reconciled = await integration_client.post(f"/api/v1/resources/{browser_id}/stop", json={
        "expected_revision": observed["owner_revision"],
    })
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["data"]["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    # 故障不会破坏共享Chromium，也不让已失败资源占据实际context。
    retried = await integration_client.post("/api/v1/resources/browsers", json=command)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    (artifacts / f"startup-failure-{stage}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": failed.json(), "owner_projection": observed, "node_record": record,
        "synchronous": synchronous, "retried": retried.json(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for resource_id in [browser_id, seed_id, retried.json()["data"]["resource_id"]]:
        deleted = await _detach_delete(integration_client, resource_id, browser_context)
        assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("navigation_type", "method", "mode"), [
    ("url", "goto", "sync"), ("reload", "reload", "async"),
    ("back", "goBack", "api"), ("forward", "goForward", "async"),
])
async def test_navigation_failure_keeps_native_details_in_diagnostics(
    navigation_type: str, method: str, mode: str, integration_client: httpx.AsyncClient,
    browser_context: CustomToolFactoryContext, browser_runtime: WorkspaceToolRuntime,
    browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/private-navigation-profile'}"
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    # 一次性替换第三方导航边界；收据、状态更新和持久化仍由真实产品入口完成。
    await transport.perform(browser_id, "run", {"code": (
        f"const original = page.{method}; page.{method} = async function (...args) {{ page.{method} = original;"
        f" throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}}); }};"
    )}, actor="agent")
    payload = {"type": navigation_type, "url": "https://example.test/navigation-target"}
    if mode == "api":
        failed = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
            "expected_revision": resource["owner_revision"], "thread": resource["source_thread"],
            "action": "navigate", "payload": payload,
        })
        assert failed.status_code == 500, failed.text
        assert failed.headers["X-Request-ID"]
        receipt = failed.json()
    else:
        with pytest.raises(BrowserRequestError) as caught:
            if mode == "sync":
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/navigate", payload)
            else:
                await transport.perform(browser_id, "navigate", payload, actor="agent")
        assert caught.value.status == 500
        assert caught.value.code == "EACCES"
        receipt = caught.value.payload
    assert f"stage=page.{method}" in json.dumps(receipt)
    assert str(root) not in json.dumps(receipt)
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == "verified"
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    state = await transport.get_resource(browser_id)
    assert state["navigation_error"]["message"] == state["last_operation"]["error"]
    assert f"stage=page.{method}" in state["navigation_error"]["message"]
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert str(root) not in json.dumps(record)
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert reason in diagnostics
    retried = await transport.perform(browser_id, "navigate", {"type": "url", "url": "about:blank"}, actor="agent")
    assert retried["operation"]["status"] == "completed"
    assert (await transport.get_resource(browser_id))["navigation_error"] is None
    (artifacts / f"navigation-failure-{method}-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": receipt, "node_record": record, "owner_projection": observed, "retried": retried,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "mode", "action", "expected_state", "reconciliation"), [
    ("capture_context", "sync", "freeze", "background", "verified"),
    ("Page.setWebLifecycleState", "async", "freeze", "background", "verified"),
    ("Page.setWebLifecycleState", "api", "wake", "restoring", "unknown"),
    ("create_page", "async", "wake", "discarded", "verified"),
    ("sync_page", "async", "wake", "discarded", "verified"),
    ("release_context", "api", "discard", "discarding", "unknown"),
])
async def test_runtime_lifecycle_failure_keeps_native_cause_out_of_receipts(
    stage: str, mode: str, action: str, expected_state: str, reconciliation: str,
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_runtime: WorkspaceToolRuntime, browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    reason = f"injected EACCES: {root / '.boxteam/browser-manager/private-runtime-profile'}"
    transport = BrowserManagerClient(workspace_id=container.workspace_id, backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}")
    command = {"expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    failure = f"throw Object.assign(new Error({json.dumps(reason)}), {{code: 'EACCES'}});"
    # 仅替换真实Playwright边界，触发一次后恢复原方法；状态转换、回滚与落盘由产品执行。
    if stage == "capture_context":
        code = "const original = context.storageState; context.storageState = async function (...args) { context.storageState = original; " + failure + " };"
    elif stage == "release_context":
        code = "const original = context.close; context.close = async function (...args) { context.close = original; " + failure + " };"
    elif stage == "create_page":
        code = "const proto = Object.getPrototypeOf(context); const original = proto.newPage; proto.newPage = async function (...args) { proto.newPage = original; " + failure + " };"
    elif stage == "sync_page":
        code = (
            "const originalPage = page; const proto = Object.getPrototypeOf(page); const original = proto.title;"
            " proto.title = async function (...args) {"
            f" if (this !== originalPage) {{ proto.title = original; {failure} }}"
            " return await original.call(this, ...args); };"
        )
    else:
        value = "active" if action == "wake" else "frozen"
        code = (
            "const cdp = await context.newCDPSession(page); const proto = Object.getPrototypeOf(cdp);"
            " const original = proto.send; await cdp.detach();"
            " proto.send = async function (method, params) {"
            f" if (method === 'Page.setWebLifecycleState' && params.state === '{value}') {{ proto.send = original; {failure} }}"
            " return await original.call(this, method, params); };"
        )
    await transport.perform(browser_id, "run", {"code": code}, actor="agent")
    if action != "freeze":
        prepared = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "freeze"})
        assert prepared.status_code == 200, prepared.text
    if stage in {"create_page", "sync_page"}:
        prepared = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "discard"})
        assert prepared.status_code == 200, prepared.text
    if mode == "api":
        response = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": action})
        assert response.status_code == 500, response.text
        assert response.headers["X-Request-ID"]
        receipt = response.json()
    else:
        with pytest.raises(BrowserRequestError) as caught:
            if mode == "sync":
                await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/{action}", {})
            else:
                await transport._json_request("POST", f"/api/browsers/{browser_id}/{action}", {})
        assert caught.value.status == 500
        assert caught.value.code == "EACCES"
        receipt = caught.value.payload
    assert f"stage={stage}" in json.dumps(receipt)
    assert str(root) not in json.dumps(receipt)
    observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
    assert observed["reconciliation_status"] == reconciliation
    assert observed["metadata"]["resource_state"] == expected_state
    assert f"stage={stage}" in observed["metadata"]["resource_transition_error"]
    assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
    synchronous = await asyncio.to_thread(transport._json_request_sync, "GET", f"/api/browsers/{browser_id}", None)
    assert synchronous["data"]["resource_transition_error"] == (await transport.get_resource(browser_id))["resource_transition_error"]
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert str(root) not in json.dumps(record)
    assert record["resource_transition_error"] == observed["metadata"]["resource_transition_error"]
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert reason in diagnostics
    assert "[browser-runtime] 资源生命周期操作失败" in diagnostics
    if reconciliation == "unknown":
        rejected = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
            **command, "action": "run", "payload": {"code": "throw new Error('must-not-execute');"},
        })
        assert rejected.status_code == 409, rejected.text
        # 未核实状态不能直接解除关联；先显式关闭实际运行时，再由owner核实终态。
        closed = await transport.stop_resource(browser_id)
        assert closed["browser"]["status"] == "closed"
        assert closed["browser"]["reconciliation_status"] == "verified"
        reconciled = await integration_client.post(f"/api/v1/resources/{browser_id}/stop", json={
            "expected_revision": observed["owner_revision"],
        })
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["data"]["reconciliation_status"] == "verified"
    elif action == "freeze":
        retried = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": action})
        assert retried.status_code == 200, retried.text
    (artifacts / f"runtime-lifecycle-{stage}-{mode}-{time.time_ns()}.json").write_text(json.dumps({
        "receipt": receipt, "owner_projection": observed, "node_record": record,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
async def test_native_file_page_url_survives_checkpoint_and_timeout_recovery(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_runtime: WorkspaceToolRuntime, browser_manager_processes: tuple[int, int],
) -> None:
    container, _session, _invocation = browser_runtime
    root = Path(container.workspace_root)
    native_page = root / "native-pages" / "本地 页面 #100%.html"
    native_page.parent.mkdir()
    native_page.write_text(
        '<!doctype html><meta charset="utf-8"><title>本机文件页面</title>'
        '<input id="value" value="initial"><p>native-file-target</p>', encoding="utf-8",
    )
    url = native_page.as_uri()
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": url,
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    assert resource["metadata"]["url"] == url
    assert resource["metadata"]["actual_url"] == url
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    synchronous = await asyncio.to_thread(transport._json_request_sync, "GET", f"/api/browsers/{browser_id}", None)
    assert synchronous["data"]["url"] == url
    assert (await transport.get_resource(browser_id))["url"] == url
    command = {"expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    edited = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
        **command, "action": "run", "payload": {"code": "await page.locator('#value').fill('restored-value'); return await page.title();"},
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["result"] == "本机文件页面"
    frozen = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "freeze"})
    assert frozen.status_code == 200, frozen.text
    checkpoint = json.loads((root / ".boxteam/browser-manager/checkpoints" / f"{browser_id}.json").read_text())
    page, = checkpoint["pages"]
    assert page["url"] == url
    assert page["requested_url"] == url
    discarded = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "discard"})
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["data"]["browser"]["metadata"]["resource_state"] == "discarded"
    awakened = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={**command, "action": "wake"})
    assert awakened.status_code == 200, awakened.text
    restored = await transport.perform(browser_id, "run", {"code": (
        "return {url: page.url(), title: await page.title(), value: await page.locator('#value').inputValue(),"
        " text: await page.locator('p').textContent()};"
    )}, actor="agent")
    assert restored["result"] == {"url": url, "title": "本机文件页面", "value": "restored-value", "text": "native-file-target"}

    missing_url = (native_page.parent / "不存在的页面.html").as_uri()
    missing = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json={
        **command, "action": "navigate", "payload": {"type": "url", "url": missing_url},
    })
    assert missing.status_code == 500, missing.text
    assert missing_url in missing.text
    failed_page = await transport.get_resource(browser_id)
    assert failed_page["navigation_error"]["requested_url"] == missing_url
    assert missing_url in failed_page["navigation_error"]["message"]
    assert failed_page["pages"][0]["navigation_error"]["requested_url"] == missing_url
    await transport.perform(browser_id, "navigate", {"type": "url", "url": url}, actor="agent")
    with pytest.raises(BrowserRequestError) as caught:
        await transport.perform(browser_id, "run", {"code": "await new Promise(() => undefined);", "timeoutMs": 25}, actor="agent")
    assert caught.value.status == 408
    assert caught.value.code == "browser_tool_timeout"
    recovered = await transport.perform(browser_id, "run", {"code": "return {url: page.url(), title: await page.title()};"}, actor="agent")
    assert recovered["result"] == {"url": url, "title": "本机文件页面"}
    records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
    record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
    assert record["url"] == url
    # 只豁免完整页面URL，不能让同一记录中的其它字段携带产物或存储位置。
    assert str(root) not in json.dumps(record).replace(json.dumps(url), '"file-page-url"')
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    (artifacts / f"native-file-url-lifecycle-{time.time_ns()}.json").write_text(json.dumps({
        "created": resource, "checkpoint": checkpoint, "restored": restored,
        "navigation_failure": missing.json(), "failed_page": failed_page,
        "timeout": caught.value.payload, "recovered": recovered, "record": record,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deleted = await _detach_delete(integration_client, browser_id, browser_context)
    assert deleted.status_code == 200, deleted.text


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


@pytest.mark.asyncio
async def test_screenshot_failure_reports_stage_and_recovers_through_tool(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    browser_id = created.json()["data"]["resource_id"]
    root = Path(container.workspace_root)
    destination = root / ".boxteam/browser-manager/screenshots"
    parked = destination.with_name(f"{destination.name}-{browser_id}-backup")
    if destination.exists():
        destination.rename(parked)
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    failures: list[dict[str, object]] = []
    screenshot_page = create_screenshot_page_tool(browser_context)
    try:
        destination.write_bytes(b"existing-directory-conflict")
        for mode in ("sync", "async", "tool"):
            with pytest.raises(BrowserRequestError) as caught:
                if mode == "sync":
                    await asyncio.to_thread(transport._json_request_sync, "POST", f"/api/browsers/{browser_id}/screenshot", {})
                elif mode == "async":
                    await transport._json_request("POST", f"/api/browsers/{browser_id}/screenshot", {})
                else:
                    await screenshot_page.ainvoke({"pageId": browser_id})
            failure = caught.value
            assert failure.status == 500
            assert failure.code in {"EEXIST", "ENOTDIR", "EPERM"}
            assert failure.payload["error"].startswith(f"保存浏览器截图失败: browser_id={browser_id}, screenshot_id=screenshot_")
            assert failure.payload["error"].endswith(f"stage=目录创建, code={failure.code}")
            assert failure.payload["operation_outcome"] == "failed"
            assert str(root) not in json.dumps(failure.payload)
            assert str(root) not in str(failure)
            failures.append(failure.payload)
            assert destination.read_bytes() == b"existing-directory-conflict"
        observed = await integration_client.get(f"/api/v1/resources/{browser_id}")
        assert observed.status_code == 200, observed.text
        assert observed.headers["X-Request-ID"] == observed.json()["request_id"]
        assert observed.json()["data"]["reconciliation_status"] == "verified"
        assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
        records = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())
        record, = [item for item in records["browsers"] if item["browser_id"] == browser_id]
        assert str(root) not in json.dumps(record)
        assert "screenshot_error" not in record
        artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
        diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
        assert "[browser-state-store] 保存浏览器截图失败" in diagnostics
        assert str(destination) in diagnostics
        destination.unlink()
        if parked.exists():
            parked.rename(destination)
        saved = _json_tool_result(await screenshot_page.ainvoke({"pageId": browser_id}))
        assert saved["mime_type"] == "image/png"
        assert str(root) not in json.dumps(saved)
        artifact_path = destination / f"{browser_id}-{saved['screenshot_id']}.png"
        expected = artifact_path.read_bytes()
        assert expected.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(expected) == saved["byte_length"]
        status, body = await asyncio.to_thread(_read_http, f"http://127.0.0.1:{browser_manager_processes[0]}{saved['screenshot_url']}?workspace_id={container.workspace_id}")
        assert status == 200 and body == expected
        (artifacts / f"screenshot-failure-{time.time_ns()}.json").write_text(json.dumps({
            "transport_failures": failures, "resource_response": observed.json(),
            "node_record": record, "recovered_screenshot": saved,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if destination.is_file():
            destination.unlink()
        if parked.exists():
            parked.rename(destination)
        deleted = await _detach_delete(integration_client, browser_id, browser_context)
        assert deleted.status_code == 200, deleted.text


@pytest.mark.asyncio
async def test_tool_runtime_reentry_retains_cancelled_queue_and_releases_holder(
    integration_workspace_root_path: str, integration_startup_mount: MountedWorkspace,
) -> None:
    from app.core.execution_admission_context import require_current_execution_admission
    from app.core.session_lifecycle_gate import ThreadExecutionHolderGate

    workspace = Path(integration_workspace_root_path)
    previous = None
    facts = []
    for _attempt in range(2):
        async with workspace_tool_runtime(workspace, startup_mount=integration_startup_mount) as (container, session_id, invocation):
            thread_id = invocation.thread_binding.thread_id
            admission = require_current_execution_admission(langgraph_thread_id=thread_id)
            record = container.checkpointer.execution_queue_record(session_id, thread_id, admission.job_id)
            assert record.phase == "started"
            assert record.cursor.admission == admission
            assert record.input.agent_id == (await container.session_service.get(session_id)).current_agent_id
            accepted = container.checkpointer.accepted_user_message(
                session_id, thread_id=thread_id, job_id=admission.job_id,
                message_id=record.input.message_id,
            )
            assert accepted.id == record.input.message_id
            assert accepted.content == "运行真实 工作区资源工具"
            async with ThreadExecutionHolderGate(workspace / ".boxteam/sessions").lock_for(session_id, thread_id) as competing:
                assert competing.acquired is False
            if previous is not None:
                prior_session, prior_thread, prior_job = previous
                settled = container.checkpointer.execution_queue_record(prior_session, prior_thread, prior_job)
                assert (settled.phase, settled.outcome) == ("terminal", "cancelled")
                assert await container.pending_request_store.records(session_id=prior_session) == ()
            facts.append({"session_id": session_id, "thread_id": thread_id, "job_id": admission.job_id,
                          "message_id": record.input.message_id, "claim_generation": record.claim_generation})
            previous = session_id, thread_id, admission.job_id
        async with ThreadExecutionHolderGate(workspace / ".boxteam/sessions").lock_for(session_id, thread_id) as released:
            assert released.acquired is True
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
    (artifacts / f"tool-runtime-reentry-{time.time_ns()}.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_state_publish_failure_reports_workspace_stage_and_allows_explicit_retry(
    integration_client: httpx.AsyncClient, browser_context: CustomToolFactoryContext,
    browser_manager_processes: tuple[int, int], browser_runtime: WorkspaceToolRuntime,
) -> None:
    container, _session, _invocation = browser_runtime
    created = await integration_client.post("/api/v1/resources/browsers", json={
        "session_id": browser_context.session_id,
        "thread_id": browser_context.invocation_context.thread_binding.thread_id,
        "url": "about:blank",
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    browser_id = resource["resource_id"]
    root = Path(container.workspace_root)
    state_file = root / ".boxteam/browser-manager/browsers.json"
    published_bytes = state_file.read_bytes()
    backup = state_file.with_suffix(".published-test-backup")
    state_file.rename(backup)
    state_file.mkdir()
    transport = BrowserManagerClient(
        workspace_id=container.workspace_id,
        backend_url=f"http://127.0.0.1:{browser_manager_processes[0]}",
    )
    failures: list[BrowserRequestError] = []
    command = {"action": "resource-policy", "expected_revision": resource["owner_revision"],
               "thread": resource["source_thread"], "payload": {"policy": "keep_alive"}}
    try:
        for mode in ("sync", "async"):
            with pytest.raises(BrowserRequestError) as caught:
                if mode == "sync":
                    await asyncio.to_thread(transport._json_request_sync, "PATCH", f"/api/browsers/{browser_id}/resource-policy", {"policy": "keep_alive"})
                else:
                    await transport._json_request("PATCH", f"/api/browsers/{browser_id}/resource-policy", {"policy": "keep_alive"})
            failure = caught.value
            assert failure.status == 500
            assert failure.code in {"EISDIR", "EPERM", "EEXIST"}
            assert failure.payload["error"] == f"写入浏览器状态失败: workspace_id={container.workspace_id}, artifact=browser_records, stage=文件发布, code={failure.code}"
            assert failure.payload["operation_outcome"] == "failed"
            assert str(root) not in json.dumps(failure.payload)
            failures.append(failure)
        failed = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
        assert failed.status_code == 500, failed.text
        assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
        assert failures[0].payload["error"] in failed.json()["detail"]
        assert str(root) not in failed.text
        assert state_file.is_dir() and list(state_file.iterdir()) == []
        assert backup.read_bytes() == published_bytes
        assert list(state_file.parent.glob("browsers.json.*.tmp")) == []
        observed = (await integration_client.get(f"/api/v1/resources/{browser_id}")).json()["data"]
        assert observed["reconciliation_status"] == "verified"
        assert observed["metadata"]["resource_policy"] == "keep_alive"
        assert container.browser_resource_owner._leases.leases_for_resource(browser_id) == []
        artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
        (artifacts / f"state-publish-failure-{time.time_ns()}.json").write_text(json.dumps({
            "transport_payloads": [failure.payload for failure in failures],
            "api_status": failed.status_code, "api_body": failed.json(),
            "request_id": failed.headers["X-Request-ID"], "owner_projection": observed,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        state_file.rmdir()
        backup.rename(state_file)
    retried = await integration_client.post(f"/api/v1/resources/{browser_id}/browser-operation", json=command)
    assert retried.status_code == 200, retried.text
    records = json.loads(state_file.read_text())
    record, = [entry for entry in records["browsers"] if entry["browser_id"] == browser_id]
    assert record["resource_policy"] == "keep_alive"
    assert str(root) not in json.dumps(record)
    diagnostics = "\n".join(item.read_text() for item in artifacts.glob("browser-backend-*.stderr.log"))
    assert "[browser-state-store] 写入浏览器状态失败" in diagnostics
    assert str(state_file) in diagnostics
    assert failures[0].code in diagnostics
