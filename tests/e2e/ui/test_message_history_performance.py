from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.path_utils import get_session_path_resolver
from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.e2e.ports import e2e_port_block_for_file


SESSION_COUNT = 6
TURNS_PER_SESSION = 240
IMAGE_EVERY_TURNS = 2


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _performance_fixture_png(width: int = 512, height: int = 320) -> bytes:
    """生成难压缩但可重复的 PNG，确保测试真的能观察图片传输量。"""
    rows = bytearray()
    state = 0x4D595DF4
    for _y in range(height):
        rows.append(0)
        for _x in range(width):
            state = (1664525 * state + 1013904223) & 0xFFFFFFFF
            rows.extend(((state >> 16) & 0xFF, (state >> 8) & 0xFF, state & 0xFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
        + _png_chunk(b"IEND", b"")
    )


async def _seed_sessions(
    *,
    gateway_url: str,
    workspace_root: Path,
) -> list[dict[str, str]]:
    saver = FileSystemCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    png = _performance_fixture_png()
    png_data_url = f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
    sessions: list[dict[str, str]] = []
    async with httpx.AsyncClient(
        base_url=gateway_url,
        headers=LOCAL_TOKEN_HEADERS,
        timeout=30,
    ) as client:
        for session_index in range(SESSION_COUNT):
            title = f"性能历史会话 {session_index + 1}"
            response = await client.post("/api/v1/sessions", json={"title": title})
            assert response.status_code == 200, response.text
            session_id = response.json()["data"]["session_id"]
            messages = []
            base_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
                days=session_index
            )
            session_dir = get_session_path_resolver(
                workspace_root / ".boxteam" / "sessions"
            ).resolve_session_dir(session_id)
            attachments_dir = (
                session_dir / "attachments"
            )
            attachments_dir.mkdir(parents=True, exist_ok=True)
            image_source = attachments_dir / "performance-fixture-source.png"
            image_source.write_bytes(png)

            for turn_index in range(TURNS_PER_SESSION):
                timestamp = (base_time + timedelta(seconds=turn_index)).isoformat()
                user_marker = f"E2E-S{session_index + 1}-TURN-{turn_index + 1:04d}-USER"
                assistant_marker = (
                    f"E2E-S{session_index + 1}-TURN-{turn_index + 1:04d}-LATEST"
                )
                user_metadata: dict[str, object] = {
                    "message_id": f"msg_perf_s{session_index + 1}_u{turn_index + 1}",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                if turn_index % IMAGE_EVERY_TURNS == 0:
                    image_file = attachments_dir / f"fixture-{turn_index + 1:04d}.png"
                    os.link(image_source, image_file)
                    user_metadata["attachments"] = [
                        {
                            "file_id": image_file.relative_to(workspace_root).as_posix(),
                            "name": f"fixture-{turn_index + 1:04d}.png",
                            "content_type": "image/png",
                        }
                    ]
                user_content: str | list[dict[str, object]] = user_marker
                if (
                    turn_index % IMAGE_EVERY_TURNS == 0
                    and turn_index >= TURNS_PER_SESSION - 4
                ):
                    # 最新页必须包含真实 image_url data URL，以防消息投影把大块
                    # base64 泄漏进 metadata.content_blocks，绕过附件缩略图管线。
                    user_content = [
                        {"type": "text", "text": user_marker},
                        {"type": "image_url", "image_url": {"url": png_data_url}},
                    ]
                messages.extend(
                    [
                        HumanMessage(
                            id=str(user_metadata["message_id"]),
                            content=user_content,
                            response_metadata=user_metadata,
                        ),
                        AIMessage(
                            id=f"msg_perf_s{session_index + 1}_a{turn_index + 1}",
                            content=assistant_marker,
                            response_metadata={
                                "message_id": f"msg_perf_s{session_index + 1}_a{turn_index + 1}",
                                "created_at": timestamp,
                                "updated_at": timestamp,
                                "phase": "final_answer",
                            },
                        ),
                    ]
                )

            await saver.aput(
                build_checkpoint_config(session_id),
                {
                    "v": 1,
                    "id": f"ckpt_perf_{session_index + 1}",
                    "ts": (base_time + timedelta(seconds=TURNS_PER_SESSION)).isoformat(),
                    "channel_values": {"messages": messages},
                    "channel_versions": {"messages": 1},
                    "versions_seen": {},
                    "pending_sends": [],
                    "updated_channels": ["messages"],
                },
                {"source": "e2e_fixture", "step": 1, "parents": {}},
                {"messages": 1},
            )
            sessions.append(
                {
                    "sessionId": session_id,
                    "title": title,
                    "oldestMarker": f"E2E-S{session_index + 1}-TURN-0001-USER",
                    "latestMarker": (
                        f"E2E-S{session_index + 1}-TURN-{TURNS_PER_SESSION:04d}-LATEST"
                    ),
                }
            )
    return sessions


@pytest.mark.asyncio
async def test_large_image_conversations_remain_incremental_and_fast(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    output_root = workspace_root.parent
    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    metrics_path = artifacts / "message-history-performance-metrics.json"
    screenshot_path = artifacts / "message-history-performance-failure.png"
    metrics_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    web_dist = project_root / "src" / "web" / "dist"
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail(
            "消息历史性能 E2E 需要 Chromium；设置 PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"
        )

    build = subprocess.run(
        ["bun", "run", "build"],
        cwd=project_root / "src" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"
    port = e2e_port_block_for_file(Path(request.node.fspath)).port(20)
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url="http://127.0.0.1:9",
        port=port,
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_dist)},
    )
    try:
        sessions = await _seed_sessions(
            gateway_url=f"http://127.0.0.1:{port}",
            workspace_root=workspace_root,
        )
        image_size = len(_performance_fixture_png())
        fixture = {
            "sessions": sessions,
            "turnsPerSession": TURNS_PER_SESSION,
            "imageCountPerSession": (TURNS_PER_SESSION + IMAGE_EVERY_TURNS - 1)
            // IMAGE_EVERY_TURNS,
            "originalImageBytes": image_size,
            "historyPagesToLoad": 5,
            # 冷启动阈值包含隔离 Gateway、checkpoint 读取和浏览器首次 React 渲染。
            # DOM/附件阈值是结构性回归门槛，不应因 CI 机器较慢而放宽。
            "thresholds": {
                "initialOpenMs": 5000,
                "switchSessionP95Ms": 1200,
                "cachedSessionReturnMs": 350,
                "historyLoadP95Ms": 1500,
                "maxRenderedTurns": 60,
                "maxDomNodes": 6000,
                "maxInitialMessageBytes": 256 * 1024,
                "maxInitialAttachmentRequests": 8,
                "maxInitialAttachmentBytes": 1024 * 1024,
                "maxViewerOriginalRequests": 3,
                "maxCachedSessionAttachmentRequests": 0,
                "maxAnchorDeltaPx": 8,
                "maxUsedJsHeapBytes": 192 * 1024 * 1024,
                "maxUsedJsHeapGrowthBytes": 64 * 1024 * 1024,
            },
        }
        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{port}",
                "BOXTEAM_E2E_METRICS_PATH": str(metrics_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "BOXTEAM_E2E_FIXTURE": json.dumps(fixture, ensure_ascii=False),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        try:
            result = subprocess.run(
                ["node", "tests/e2e/ui/message_history_performance.mjs"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            pytest.fail(
                "消息历史性能浏览器驱动在 180 秒内未结束；"
                f"stdout={error.stdout!r} stderr={error.stderr!r}"
            )
        assert metrics_path.is_file(), (
            "浏览器测试没有写出 metrics JSON:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        assert result.returncode == 0, (
            "消息历史性能指标失败:\n"
            + "\n".join(metrics.get("failures", []))
            + f"\nmetrics={metrics_path}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert metrics["passed"] is True
    finally:
        close_gateway_process(gateway)
