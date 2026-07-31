from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import commentjson
import httpx
import pytest


async def _wait_for_log_entry(
    *,
    log_path: Path,
    marker: str,
    process: subprocess.Popen[str],
    start_offset: int = 0,
    timeout_seconds: float = 10,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    content = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "等待日志时工作区后端提前退出: "
                f"pid={process.pid}, returncode={process.returncode}"
            )
        if log_path.is_file():
            content = log_path.read_text(encoding="utf-8")
            if marker in content[start_offset:]:
                return content
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待日志超时: marker={marker!r}, path={log_path}, tail={content[-2000:]!r}"
    )


@pytest.mark.asyncio
async def test_workspace_backend_writes_startup_trace_and_config_reload_logs(
    client: httpx.AsyncClient,
    e2e_backend_process: subprocess.Popen[str],
    e2e_workspace_config_path: str,
    e2e_workspace_root_path: str,
) -> None:
    config_path = Path(e2e_workspace_config_path)
    log_path = (
        Path(e2e_workspace_root_path)
        / ".boxteam"
        / "logs"
        / "e2e-backend.stderr.log"
    )
    original_config = config_path.read_text(encoding="utf-8")

    startup_log = await _wait_for_log_entry(
        log_path=log_path,
        marker="工作区后端日志已初始化",
        process=e2e_backend_process,
    )
    assert "工作区后端日志引导已初始化" in startup_log

    trace_offset = len(startup_log)
    trace_response = await client.get(
        "/api/v1/health",
        headers={"X-Request-ID": "req_logging_e2e"},
    )
    assert trace_response.status_code == 200, trace_response.text
    assert trace_response.headers["X-Request-ID"] == "req_logging_e2e"
    await _wait_for_log_entry(
        log_path=log_path,
        marker="request_id=req_logging_e2e",
        process=e2e_backend_process,
        start_offset=trace_offset,
    )

    config = commentjson.loads(original_config)
    config["agents"]["default"]["description"] += " [logging-e2e-first]"
    first_valid_config = json.dumps(config, ensure_ascii=False, indent=2)

    try:
        reload_offset = len(log_path.read_text(encoding="utf-8"))
        config_path.write_text(first_valid_config, encoding="utf-8")
        first_reload_log = await _wait_for_log_entry(
            log_path=log_path,
            marker="配置热重载成功",
            process=e2e_backend_process,
            start_offset=reload_offset,
        )

        legacy_custom_tools = config["agents"]["default"]["tools"]["custom"]
        config.pop("config_version", None)
        config["agents"]["default"]["tools"]["custom"] = [
            (
                {
                    "name": "read_context",
                    "factory": (
                        "app.agents.tools.session_history:"
                        "create_read_session_recent_text_messages_tool"
                    ),
                }
                if item.get("tool_id") == "read_context"
                else item
            )
            for item in legacy_custom_tools
        ]
        migration_offset = len(first_reload_log)
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await _wait_for_log_entry(
            log_path=log_path,
            marker="配置源迁移成功",
            process=e2e_backend_process,
            start_offset=migration_offset,
        )
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        assert config["config_version"] == 4
        migrated_tool_ids = {
            item.get("tool_id")
            for item in config["agents"]["default"]["tools"]["custom"]
        }
        assert migrated_tool_ids >= {
            "read_context"
        }
        reload_status = await client.get("/api/v1/config/reload-status")
        assert reload_status.status_code == 200, reload_status.text
        assert reload_status.json()["data"]["healthy"] is True

        failure_offset = len(log_path.read_text(encoding="utf-8"))
        config_path.write_text("{ invalid jsonc", encoding="utf-8")
        failure_log = await _wait_for_log_entry(
            log_path=log_path,
            marker="配置热重载失败",
            process=e2e_backend_process,
            start_offset=failure_offset,
        )
        assert "继续使用最后一个有效快照" in failure_log[failure_offset:]

        config["agents"]["default"]["description"] += " [logging-e2e-recovered]"
        recovered_config = json.dumps(config, ensure_ascii=False, indent=2)
        recovery_offset = len(failure_log)
        config_path.write_text(recovered_config, encoding="utf-8")
        await _wait_for_log_entry(
            log_path=log_path,
            marker="配置热重载成功",
            process=e2e_backend_process,
            start_offset=recovery_offset,
        )
    finally:
        config_path.write_text(original_config, encoding="utf-8")
