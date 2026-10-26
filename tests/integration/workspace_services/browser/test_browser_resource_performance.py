from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest
import websockets

from app.core.workspace_identity import load_or_create_workspace_id
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)

MAX_SHARED_RUNTIME_PROCESS_MEMORY_KIB = 400 * 1024
MAX_FROZEN_CPU_PERCENT = 5.0
MAX_FROZEN_CPU_RATIO = 0.05
MAX_FREEZE_MS = 300
MAX_FROZEN_WAKE_MS = 500
MAX_DISCARDED_WAKE_MS = 700
MIN_INTERACTIVE_STREAM_FPS = 15.0
MAX_INTERACTIVE_STREAM_BITRATE_BPS = 2_200_000
MAX_ATTACH_FIRST_FRAME_MS = 700
MAX_LATEST_FRAME_AFTER_ACK_MS = 250
MIN_LATEST_FRAME_CAPTURE_INTERVAL_SECONDS = 0.2


class _DynamicPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"""<!doctype html><html><head><style>
        @keyframes spin { to { transform: rotate(360deg); } }
        #spinner { width: 100px; height: 100px; background: #c44; animation: spin 0.05s linear infinite; }
        </style></head><body><input id=q value=initial><div id=spinner></div>
        <div style='height:2400px'></div><script>
        setInterval(() => {
          const deadline = performance.now() + 15;
          while (performance.now() < deadline) Math.sqrt(Math.random());
        }, 20);
        </script></body></html>"""
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _browser_backend_process(
    *,
    workspace_root: Path,
    backend_port: int,
    log_path: Path,
) -> subprocess.Popen[str]:
    ensure_local_port_available(backend_port, service_name="Browser 性能测试后端")
    project_root = Path.cwd().resolve()
    if os.name == "posix":
        process_group_options: dict[str, int | bool] = {"start_new_session": True}
    elif os.name == "nt":
        process_group_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        raise RuntimeError(f"Browser 性能测试不支持当前操作系统: {os.name}")
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                "node",
                "backend.js",
                "--host",
                "127.0.0.1",
                "--port",
                str(backend_port),
                "--frontend-url",
                "http://127.0.0.1:1",
                "--workspace-root",
                str(workspace_root),
            ],
            cwd=project_root / "src" / "workspace-services" / "browser" / "server",
            env={
                **os.environ,
                "BOXTEAM_PROJECT_ROOT": str(project_root),
                "WORKSPACE_ROOT": str(workspace_root),
                "BOXTEAM_BROWSER_WORKSPACE_ROOT": str(workspace_root),
                "BOXTEAM_WORKSPACE_ID": load_or_create_workspace_id(workspace_root),
            },
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            **process_group_options,
        )
    try:
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", process)
    except BaseException as startup_error:
        try:
            _terminate_browser_backend(process, graceful=False)
        except BaseException as cleanup_error:  # noqa: BLE001 - 启动和清理错误一并保留
            raise BaseExceptionGroup(
                "Browser 性能测试后端就绪检查失败且进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    return process


def _terminate_browser_backend(
    process: subprocess.Popen[str], *, graceful: bool = True,
) -> None:
    if not graceful or os.name != "nt":
        terminate_process(process)
        return
    if process.poll() is not None:
        return

    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    except OSError as signal_error:
        try:
            terminate_process(process)
        except BaseException as cleanup_error:  # noqa: BLE001 - 同时保留优雅关闭和强制清理错误
            raise BaseExceptionGroup(
                "Browser 后端无法接收关闭信号且进程清理失败",
                [signal_error, cleanup_error],
            ) from None
        raise RuntimeError(
            "Browser 后端未收到 Windows CTRL_BREAK_EVENT，已强制清理"
        ) from signal_error

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_process(process)


def _owner_headers(workspace_id: str) -> dict[str, str]:
    return {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-browser"}


def _create_options(workspace_id: str, url: str) -> dict[str, object]:
    return {"workspace_id": workspace_id, "browser_id": f"browser_{uuid4().hex}", "owner_revision": 1, "url": url}


def _descendant_processes(root_pid: int) -> list[psutil.Process]:
    try:
        return psutil.Process(root_pid).children(recursive=True)
    except psutil.Error as error:
        raise RuntimeError(
            f"无法枚举 Browser 后端子进程: pid={root_pid}: {error}"
        ) from error


def _process_tree_memory_kib(root_pid: int) -> tuple[int, str]:
    metric = "pss" if sys.platform == "linux" else "uss"
    total = 0
    sampled_processes = 0
    for process in _descendant_processes(root_pid):
        try:
            memory = process.memory_full_info()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise RuntimeError(
                f"无法读取 Browser 子进程内存: pid={process.pid}: {error}"
            ) from error
        measured_bytes = getattr(memory, metric, None)
        if measured_bytes is None:
            raise RuntimeError(
                f"当前平台的进程内存信息缺少 {metric.upper()} 指标: "
                f"platform={sys.platform}, pid={process.pid}"
            )
        total += measured_bytes
        sampled_processes += 1
    if sampled_processes == 0:
        raise RuntimeError(f"Browser 后端没有可采样的子进程: pid={root_pid}")
    return total // 1024, metric


def _process_tree_cpu_seconds(root_pid: int) -> float:
    seconds = 0.0
    sampled_processes = 0
    for process in _descendant_processes(root_pid):
        try:
            cpu_times = process.cpu_times()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise RuntimeError(
                f"无法读取 Browser 子进程 CPU 时间: pid={process.pid}: {error}"
            ) from error
        seconds += cpu_times.user + cpu_times.system
        sampled_processes += 1
    if sampled_processes == 0:
        raise RuntimeError(f"Browser 后端没有可采样的子进程: pid={root_pid}")
    return seconds


def _cpu_percent(root_pid: int, duration_seconds: float = 1.0) -> float:
    start = _process_tree_cpu_seconds(root_pid)
    started_at = time.monotonic()
    time.sleep(duration_seconds)
    elapsed = time.monotonic() - started_at
    end = _process_tree_cpu_seconds(root_pid)
    return max(0.0, (end - start) / elapsed * 100)


def _data(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    payload = response.json().get("data")
    assert isinstance(payload, dict), response.text
    return payload


def _browser_frame_metadata(message: bytes) -> dict[str, object]:
    assert len(message) >= 4, "浏览器二进制帧缺少元数据长度"
    metadata_length = int.from_bytes(message[:4], "big")
    assert 0 < metadata_length <= len(message) - 4, "浏览器二进制帧元数据长度非法"
    metadata = json.loads(message[4 : 4 + metadata_length])
    assert metadata["type"] == "frame"
    assert isinstance(metadata["frameId"], int)
    return metadata


async def _ack_browser_frame(
    websocket: websockets.ClientConnection,
    message: bytes,
) -> dict[str, object]:
    metadata = _browser_frame_metadata(message)
    await websocket.send(json.dumps({
        "type": "frameAck",
        "browserId": metadata["browserId"],
        "frameId": metadata["frameId"],
        "decodeMs": 0,
        "drawMs": 0,
    }))
    return metadata


async def _measure_attached_stream(backend_port: int, browser_id: str, workspace_id: str) -> dict[str, float]:
    started_at = time.monotonic()
    frames: list[tuple[float, int]] = []
    attached = False
    interaction_sent = False
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", headers=_owner_headers(workspace_id)) as client:
        grant = _data(await client.post(f"/api/browsers/{browser_id}/transport-grant", json={"owner_revision": 1, "association": None}))
    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser?workspace_id={workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        deadline = started_at + 8.0
        while time.monotonic() < deadline:
            if not interaction_sent and time.monotonic() >= started_at + 6.25:
                await websocket.send(json.dumps({
                    "type": "pointer",
                    "browserId": browser_id,
                    "action": "move",
                    "x": 20,
                    "y": 20,
                }))
                interaction_sent = True
            try:
                message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
            if isinstance(message, bytes):
                frames.append((time.monotonic(), len(message)))
                await _ack_browser_frame(websocket, message)
                continue
            payload = json.loads(message)
            if payload.get("type") == "attached":
                attached = True
        await websocket.send(json.dumps({"type": "detach"}))
    assert attached, "WebSocket 未返回 attached"
    assert len(frames) >= 2, f"attach 后帧数不足: {len(frames)}"
    first_frame_at = frames[0][0]

    def window_rate(window_start: float, window_end: float) -> tuple[float, float, int]:
        window_frames = [size for timestamp, size in frames if window_start <= timestamp < window_end]
        duration = window_end - window_start
        return len(window_frames) / duration, sum(window_frames) * 8 / duration, len(window_frames)

    interactive_fps, interactive_bitrate, interactive_frames = window_rate(
        first_frame_at,
        first_frame_at + 1.3,
    )
    relaxed_fps, relaxed_bitrate, relaxed_frames = window_rate(
        started_at + 5.0,
        started_at + 6.2,
    )
    boosted_fps, boosted_bitrate, boosted_frames = window_rate(
        started_at + 6.5,
        started_at + 7.8,
    )
    return {
        "first_frame_ms": round((first_frame_at - started_at) * 1000),
        "fps": interactive_fps,
        "bitrate_bps": interactive_bitrate,
        "frame_count": float(interactive_frames),
        "relaxed_fps": relaxed_fps,
        "relaxed_bitrate_bps": relaxed_bitrate,
        "relaxed_frame_count": float(relaxed_frames),
        "boosted_fps": boosted_fps,
        "boosted_bitrate_bps": boosted_bitrate,
        "boosted_frame_count": float(boosted_frames),
    }


async def _measure_static_stream(backend_port: int, browser_id: str, workspace_id: str) -> dict[str, float]:
    started_at = time.monotonic()
    frames: list[tuple[float, int]] = []
    attached = False
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", headers=_owner_headers(workspace_id)) as client:
        grant = _data(await client.post(f"/api/browsers/{browser_id}/transport-grant", json={"owner_revision": 1, "association": None}))
    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser?workspace_id={workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        deadline = started_at + 3.5
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
            if isinstance(message, bytes):
                frames.append((time.monotonic(), len(message)))
                await _ack_browser_frame(websocket, message)
                continue
            if json.loads(message).get("type") == "attached":
                attached = True
        await websocket.send(json.dumps({"type": "detach"}))
    assert attached, "静态页面 WebSocket 未返回 attached"
    late_frames = [size for timestamp, size in frames if timestamp >= started_at + 2.3]
    return {
        "total_frame_count": float(len(frames)),
        "late_frame_count": float(len(late_frames)),
        "late_fps": len(late_frames) / 1.2,
        "late_bitrate_bps": sum(late_frames) * 8 / 1.2,
    }


async def _measure_latest_frame_delivery(backend_port: int, browser_id: str, workspace_id: str) -> dict[str, float]:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", headers=_owner_headers(workspace_id)) as client:
        grant = _data(await client.post(f"/api/browsers/{browser_id}/transport-grant", json={"owner_revision": 1, "association": None}))
    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/browser?workspace_id={workspace_id}") as websocket:
        await websocket.send(json.dumps({"type": "attach", "browserId": browser_id, "grantToken": grant["grant_token"]}))
        first_frame: bytes | None = None
        while first_frame is None:
            message = await asyncio.wait_for(websocket.recv(), timeout=2)
            if isinstance(message, bytes):
                first_frame = message
        first_metadata = _browser_frame_metadata(first_frame)
        await asyncio.sleep(0.3)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            headers=_owner_headers(workspace_id), timeout=5,
        ) as client:
            response = await client.post(
                f"/api/browsers/{browser_id}/run",
                json={
                    "code": (
                        "await page.evaluate(() => { document.body.style.backgroundColor = "
                        "document.body.style.backgroundColor === 'rgb(1, 2, 3)' ? "
                        "'rgb(4, 5, 6)' : 'rgb(1, 2, 3)'; }); return true;"
                    ),
                },
            )
            assert response.status_code == 200, response.text
            pending_deadline = time.monotonic() + 1
            while True:
                stream_state = _data(await client.get(f"/api/browsers/{browser_id}"))
                delivery = stream_state["stream_metrics"]["delivery"]
                if delivery["clients"][0]["has_pending_frame"] is True:
                    break
                assert time.monotonic() < pending_deadline, delivery
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.15)
            stream_state = _data(await client.get(f"/api/browsers/{browser_id}"))
            delivery = stream_state["stream_metrics"]["delivery"]
            assert delivery["frames_superseded"] > 0, delivery
        ack_started_at = time.monotonic()
        await _ack_browser_frame(websocket, first_frame)
        next_frame: bytes | None = None
        while next_frame is None:
            message = await asyncio.wait_for(websocket.recv(), timeout=2)
            if isinstance(message, bytes):
                next_frame = message
        received_after_ack_ms = (time.monotonic() - ack_started_at) * 1000
        next_metadata = await _ack_browser_frame(websocket, next_frame)
        await websocket.send(json.dumps({"type": "detach"}))
    return {
        "next_frame_after_ack_ms": received_after_ack_ms,
        # 两帧捕获时间之差，证明待发帧已更新；不是捕获到接收之间的帧龄。
        "capture_interval_seconds": (
            float(next_metadata["timestamp"]) - float(first_metadata["timestamp"])
        ),
    }


@pytest.fixture(scope="module")
def dynamic_page_server(request: pytest.FixtureRequest) -> Generator[str, None, None]:
    port = integration_port_block_for_file(Path(request.node.fspath)).port(62)
    with ExitStack() as lifetime:
        server = ThreadingHTTPServer(("127.0.0.1", port), _DynamicPageHandler)
        lifetime.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        lifetime.callback(thread.join, timeout=5)
        lifetime.callback(server.shutdown)
        yield f"http://127.0.0.1:{port}/"


def test_browser_resource_governance_meets_first_ci_budget(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    dynamic_page_server: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path).resolve()
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "browser-resource-performance.json"
    log_path = artifacts / "browser-resource-backend.log"
    result_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    port = integration_port_block_for_file(Path(request.node.fspath)).port(60)
    measurements: dict[str, object] = {}
    workspace_id = load_or_create_workspace_id(workspace_root)
    with ExitStack() as lifetime:
        process = _browser_backend_process(
            workspace_root=workspace_root,
            backend_port=port,
            log_path=log_path,
        )
        lifetime.callback(_terminate_browser_backend, process)
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=_owner_headers(workspace_id), timeout=30)
        lifetime.callback(client.close)
        first = _data(client.post(
            "/api/browsers",
            json=_create_options(workspace_id, dynamic_page_server),
        ))
        first_id = str(first["browser_id"])
        _data(client.post(
            f"/api/browsers/{first_id}/run",
            json={
                "code": (
                    "await page.locator('#q').fill('restored-value');"
                    "await page.evaluate(() => { sessionStorage.setItem('restore-key', 'restore-value');"
                    "window.scrollTo(0, 900); }); return true;"
                )
            },
        ))
        time.sleep(0.5)
        live_cpu = _cpu_percent(process.pid)
        freeze_started = time.monotonic()
        frozen = _data(client.post(f"/api/browsers/{first_id}/freeze"))
        freeze_ms = round((time.monotonic() - freeze_started) * 1000)
        frozen_cpu = _cpu_percent(process.pid)
        assert frozen["resource_state"] == "frozen"
        assert live_cpu >= 20, f"动态基线过低，无法验证冻结收益: {live_cpu:.2f}%"
        assert frozen_cpu <= MAX_FROZEN_CPU_PERCENT
        assert frozen_cpu <= live_cpu * MAX_FROZEN_CPU_RATIO
        assert freeze_ms <= MAX_FREEZE_MS

        wake_started = time.monotonic()
        woke = _data(client.post(f"/api/browsers/{first_id}/wake"))
        frozen_wake_ms = round((time.monotonic() - wake_started) * 1000)
        assert woke["resource_state"] == "background"
        assert frozen_wake_ms <= MAX_FROZEN_WAKE_MS

        browser_ids = [first_id]
        for _ in range(4):
            browser = _data(client.post(
                "/api/browsers",
                json=_create_options(workspace_id, "about:blank"),
            ))
            browser_ids.append(str(browser["browser_id"]))
        for browser_id, extra_pages in zip(browser_ids, [2, 2, 2, 2, 1], strict=True):
            for _ in range(extra_pages):
                _data(client.post(
                    f"/api/browsers/{browser_id}/navigate",
                    json={"type": "new_tab", "url": "about:blank"},
                ))
        time.sleep(0.5)
        shared_runtime_memory_kib, shared_runtime_memory_metric = (
            _process_tree_memory_kib(process.pid)
        )
        health = client.get("/health").json()
        assert health["browser_runtime"]["context_count"] == 5
        assert shared_runtime_memory_kib <= MAX_SHARED_RUNTIME_PROCESS_MEMORY_KIB

        _data(client.post(
            f"/api/browsers/{first_id}/navigate",
            json={"type": "activate_tab", "tab_id": first_id},
        ))
        for browser_id in browser_ids:
            state = _data(client.post(f"/api/browsers/{browser_id}/freeze"))
            assert state["resource_state"] == "frozen"
        discarded = _data(client.post(f"/api/browsers/{first_id}/discard"))
        assert discarded["resource_state"] == "discarded"
        assert len(discarded["pages"]) == 3

        shutdown_hot_id = browser_ids[1]
        _data(client.post(f"/api/browsers/{shutdown_hot_id}/wake"))
        _data(client.post(
            f"/api/browsers/{shutdown_hot_id}/navigate",
            json={"type": "url", "url": dynamic_page_server},
        ))
        _data(client.post(
            f"/api/browsers/{shutdown_hot_id}/run",
            json={"code": "await page.locator('#q').fill('shutdown-checkpoint'); return true;"},
        ))

        _terminate_browser_backend(process)
        process = _browser_backend_process(
            workspace_root=workspace_root,
            backend_port=port,
            log_path=log_path,
        )
        lifetime.callback(_terminate_browser_backend, process)
        client.close()
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=_owner_headers(workspace_id), timeout=30)
        lifetime.callback(client.close)
        restored_record = _data(client.get(f"/api/browsers/{first_id}"))
        assert restored_record["resource_state"] == "discarded"
        shutdown_checkpoint = _data(client.get(f"/api/browsers/{shutdown_hot_id}"))
        assert shutdown_checkpoint["status"] == "running"
        assert shutdown_checkpoint["resource_state"] == "discarded"
        _data(client.post(f"/api/browsers/{shutdown_hot_id}/wake"))
        shutdown_restored = _data(client.post(
            f"/api/browsers/{shutdown_hot_id}/run",
            json={"code": "return await page.locator('#q').inputValue();"},
        ))
        assert shutdown_restored["result"] == "shutdown-checkpoint"
        cold_wake_started = time.monotonic()
        restored_record = _data(client.post(f"/api/browsers/{first_id}/wake"))
        cold_wake_ms = round((time.monotonic() - cold_wake_started) * 1000)
        assert cold_wake_ms <= MAX_DISCARDED_WAKE_MS
        restored = _data(client.post(
            f"/api/browsers/{first_id}/run",
            json={
                "code": (
                    "return { value: await page.locator('#q').inputValue(),"
                    "storage: await page.evaluate(() => sessionStorage.getItem('restore-key'))," 
                    "scrollY: await page.evaluate(() => window.scrollY) };"
                )
            },
        ))
        assert restored["result"] == {
            "value": "restored-value",
            "storage": "restore-value",
            "scrollY": 900,
        }
        _data(client.post(
            f"/api/browsers/{first_id}/run",
            json={"code": "await page.evaluate(() => window.scrollTo(0, 0)); return true;"},
        ))

        stream = asyncio.run(_measure_attached_stream(port, first_id, workspace_id))
        assert stream["first_frame_ms"] <= MAX_ATTACH_FIRST_FRAME_MS
        assert stream["fps"] >= MIN_INTERACTIVE_STREAM_FPS
        assert stream["bitrate_bps"] <= MAX_INTERACTIVE_STREAM_BITRATE_BPS
        assert stream["relaxed_fps"] <= stream["fps"] * 0.65
        assert stream["relaxed_bitrate_bps"] <= stream["bitrate_bps"] * 0.65
        assert stream["boosted_fps"] >= MIN_INTERACTIVE_STREAM_FPS
        assert stream["boosted_fps"] >= stream["relaxed_fps"] * 1.5

        latest_frame = asyncio.run(_measure_latest_frame_delivery(port, first_id, workspace_id))
        assert latest_frame["next_frame_after_ack_ms"] <= MAX_LATEST_FRAME_AFTER_ACK_MS
        assert latest_frame["capture_interval_seconds"] >= MIN_LATEST_FRAME_CAPTURE_INTERVAL_SECONDS

        static_browser = _data(client.post(
            "/api/browsers",
            json=_create_options(workspace_id, "about:blank"),
        ))
        static_stream = asyncio.run(_measure_static_stream(port, str(static_browser["browser_id"]), workspace_id))
        assert static_stream["late_frame_count"] == 0
        assert static_stream["late_bitrate_bps"] == 0

        _data(client.patch(
            f"/api/browsers/{first_id}/resource-policy",
            json={"policy": "keep_alive"},
        ))
        protected = client.post(f"/api/browsers/{first_id}/freeze")
        assert protected.status_code == 409
        assert protected.json()["code"] == "browser_resource_protected"

        measurements = {
            "live_cpu_percent": round(live_cpu, 2),
            "frozen_cpu_percent": round(frozen_cpu, 2),
            "freeze_ms": freeze_ms,
            "frozen_wake_ms": frozen_wake_ms,
            "cold_wake_ms": cold_wake_ms,
            "shared_runtime_memory_kib": shared_runtime_memory_kib,
            "shared_runtime_memory_metric": shared_runtime_memory_metric,
            "browser_count": 5,
            "page_count": 14,
            "interactive_stream": {
                key: round(value, 2) for key, value in stream.items()
            },
            "static_stream": {
                key: round(value, 2) for key, value in static_stream.items()
            },
            "latest_frame_delivery": {
                key: round(value, 2) for key, value in latest_frame.items()
            },
            "budgets": {
                "max_shared_runtime_process_memory_kib": MAX_SHARED_RUNTIME_PROCESS_MEMORY_KIB,
                "max_frozen_cpu_percent": MAX_FROZEN_CPU_PERCENT,
                "max_frozen_cpu_ratio": MAX_FROZEN_CPU_RATIO,
                "max_freeze_ms": MAX_FREEZE_MS,
                "max_frozen_wake_ms": MAX_FROZEN_WAKE_MS,
                "max_discarded_wake_ms": MAX_DISCARDED_WAKE_MS,
                "min_interactive_stream_fps": MIN_INTERACTIVE_STREAM_FPS,
                "max_interactive_stream_bitrate_bps": MAX_INTERACTIVE_STREAM_BITRATE_BPS,
                "max_attach_first_frame_ms": MAX_ATTACH_FIRST_FRAME_MS,
                "max_latest_frame_after_ack_ms": MAX_LATEST_FRAME_AFTER_ACK_MS,
                "min_latest_frame_capture_interval_seconds": MIN_LATEST_FRAME_CAPTURE_INTERVAL_SECONDS,
            },
        }
        result_path.write_text(
            json.dumps(measurements, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    assert result_path.is_file(), measurements
