#!/usr/bin/env python3
"""Checkpoint 保存与读取端到端测试。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import httpx
import pytest

from tests.e2e.utils import last_assistant_message, normalize_text, wait_for_job_done


E2E_READY_TIMEOUT_SECONDS = 60


def _terminate_process(process: subprocess.Popen[str], stdout_file=None, stderr_file=None) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=10)
            except Exception:
                pass

    if stdout_file is not None:
        try:
            stdout_file.close()
        except Exception:
            pass
    if stderr_file is not None:
        try:
            stderr_file.close()
        except Exception:
            pass


def _start_backend(workspace_root: str, port: int, config_path: str) -> tuple[subprocess.Popen[str], Any, Any]:
    _kill_process_on_port(port)

    project_root = Path.cwd().resolve()
    python_executable = _resolve_workspace_python_executable(project_root)

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["PYTHONUNBUFFERED"] = "1"

    log_dir = Path(workspace_root) / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "e2e-backend-restart.stdout.log"
    stderr_path = log_dir / "e2e-backend-restart.stderr.log"
    stdout_file = open(stdout_path, "a", encoding="utf-8")
    stderr_file = open(stderr_path, "a", encoding="utf-8")

    process = subprocess.Popen(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )

    try:
        _wait_for_backend_ready(port, process)
    except Exception:
        _terminate_process(process, stdout_file, stderr_file)
        raise

    return process, stdout_file, stderr_file


async def _send_simple_message(client: httpx.AsyncClient, session_id: str, content: str) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["job_id"]


def _read_checkpoint_jsonl_records(checkpoint_jsonl: Path) -> list[dict]:
    records: list[dict] = []
    if not checkpoint_jsonl.exists():
        return records
    with checkpoint_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _resolve_workspace_python_executable(project_root: Path) -> Path:
    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python

    posix_python = project_root / ".venv" / "bin" / "python"
    if posix_python.exists():
        return posix_python

    raise FileNotFoundError(
        f"未找到工作区虚拟环境 Python，可尝试路径: {windows_python} 或 {posix_python}"
    )


def _wait_for_backend_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + E2E_READY_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"后端进程提前退出，返回码: {process.returncode}\n"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)

    raise TimeoutError(
        f"后端在 {E2E_READY_TIMEOUT_SECONDS} 秒内未就绪，端口: {port}\n"
    )


def _kill_process_on_port(port: int) -> None:
    if os.name != "nt":
        return

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return

    target_pids: set[int] = set()
    for line in result.stdout.decode("utf-8", errors="ignore").splitlines():
        try:
            target_pids.add(int(line.strip()))
        except ValueError:
            continue

    for pid in target_pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


@pytest.mark.asyncio
async def test_checkpoint_save_reload_and_survive_restart(
    client: httpx.AsyncClient,
    e2e_backend_process: subprocess.Popen[str],
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
    e2e_config_path: str,
):
    """checkpoint 保存到磁盘、多次运行读取上下文、后端重启后仍能恢复。"""
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Checkpoint Persistence Full Flow Test"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_simple_message(
        client,
        session_id,
        "请记住这个数字：42。只回复'已记住'。",
    )
    first_job_data = await wait_for_job_done(client, first_job_id)
    assert first_job_data["status"] in {"completed", "succeeded"}

    checkpoint_jsonl = (
        Path(e2e_workspace_root_path) / ".boxteam" / "checkpoints" / session_id / "checkpoints.jsonl"
    )
    assert checkpoint_jsonl.exists(), "第一次运行后 checkpoint JSONL 文件应已创建"

    first_records = _read_checkpoint_jsonl_records(checkpoint_jsonl)
    assert first_records, "第一次运行后 checkpoint JSONL 文件为空"
    for record in first_records:
        assert "checkpoint_id" in record
        assert "checkpoint" in record
        assert "metadata" in record

    blobs_dir = checkpoint_jsonl.parent / "blobs"
    assert blobs_dir.exists(), "checkpoint blobs 目录未创建"
    assert any(blobs_dir.iterdir()), "checkpoint blobs 目录为空"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    first_messages = messages_response.json()["data"]["items"]
    assert len(first_messages) == 2
    assert first_messages[0]["role"] == "user"
    assert first_messages[1]["role"] == "assistant"

    second_job_id = await _send_simple_message(
        client,
        session_id,
        "我刚才让你记住的数字是多少？只回复数字。",
    )
    second_job_data = await wait_for_job_done(client, second_job_id)
    assert second_job_data["status"] in {"completed", "succeeded"}

    second_records = _read_checkpoint_jsonl_records(checkpoint_jsonl)
    assert len(second_records) > len(first_records), "第二次运行后 checkpoint 记录数应增加"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    second_messages = messages_response.json()["data"]["items"]
    assert len(second_messages) == 4, f"历史消息数量不符合预期: {len(second_messages)}"

    roles = [msg["role"] for msg in second_messages]
    assert roles == ["user", "assistant", "user", "assistant"], f"消息 role 序列不符合预期: {roles}"

    second_reply = normalize_text(last_assistant_message(second_messages))
    assert "42" in second_reply, f"助手未从 checkpoint 恢复上下文，回复: {second_reply}"

    _terminate_process(e2e_backend_process)

    restarted_process, stdout_file, stderr_file = _start_backend(
        e2e_workspace_root_path, e2e_backend_port, e2e_config_path
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{e2e_backend_port}",
            timeout=30,
            headers={"X-Local-Token": "local-dev-token"},
        ) as restarted_client:
            messages_response = await restarted_client.get(f"/api/v1/sessions/{session_id}/messages")
            assert messages_response.status_code == 200
            restarted_messages = messages_response.json()["data"]["items"]
            assert len(restarted_messages) == 4, f"重启后应能读取历史消息: {len(restarted_messages)}"

            third_job_id = await _send_simple_message(
                restarted_client,
                session_id,
                "把之前记住的数字加 1 等于多少？只回复数字。",
            )
            third_job_data = await wait_for_job_done(restarted_client, third_job_id)
            assert third_job_data["status"] in {"completed", "succeeded"}

            messages_response = await restarted_client.get(f"/api/v1/sessions/{session_id}/messages")
            assert messages_response.status_code == 200
            final_messages = messages_response.json()["data"]["items"]
            assert len(final_messages) == 6

            third_reply = normalize_text(last_assistant_message(final_messages))
            assert "43" in third_reply, f"重启后续对话未恢复上下文，回复: {third_reply}"
    finally:
        _terminate_process(restarted_process, stdout_file, stderr_file)

