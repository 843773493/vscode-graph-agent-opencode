from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.core.path_utils import get_workspace_root
from app.schemas.background_message import BackgroundMessageKind
from app.schemas.common import RunMode
from app.schemas.message import MessageCreate, MessageRunRequest, RunOptions
from app.services.message_service import MessageService


def _get_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_python_executable() -> Path:
    candidates = []

    env_python = os.environ.get("BOXTEAM_PYTHON_EXECUTABLE")
    if env_python:
        candidates.append(Path(env_python))

    candidates.extend(
        [
            get_workspace_root() / ".venv" / "Scripts" / "python.exe",
            _get_repo_root() / ".venv" / "Scripts" / "python.exe",
        ]
    )

    for python_executable in candidates:
        if python_executable.exists():
            return python_executable

    candidate_list = "\n".join(str(path) for path in candidates)
    raise RuntimeError(
        "未找到可用的 Python 解释器。\n"
        "已检查以下路径：\n"
        f"{candidate_list}\n"
        "请确认仓库根目录或工作区根目录下存在 .venv\\Scripts\\python.exe，"
        "或者通过 BOXTEAM_PYTHON_EXECUTABLE 显式指定。"
    )


def create_python_execution_tool(session_id: str, agent_id: str = "deep_agent"):
    del session_id, agent_id
    python_executable = get_python_executable()

    @tool("python_exec")
    async def python_exec(code: str, timeout_seconds: int = 30) -> dict[str, Any]:
        """使用工作区 .venv\\Scripts\\python.exe 执行 Python 代码。"""
        if not code.strip():
            raise ValueError("code 不能为空")

        workspace_root = get_workspace_root()
        cache_dir = workspace_root / ".boxteam" / "cache" / "python_exec"
        cache_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".py",
            dir=cache_dir,
            delete=False,
        ) as temp_file:
            script_path = Path(temp_file.name)
            temp_file.write(textwrap.dedent(code))

        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(workspace_root), existing_pythonpath] if existing_pythonpath else [str(workspace_root)]
        )

        try:
            process = await asyncio.create_subprocess_exec(
                str(python_executable),
                str(script_path),
                cwd=str(workspace_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                raise RuntimeError(
                    "Python 代码执行超时。\n"
                    f"超时时间: {timeout_seconds}s\n"
                    f"STDOUT:\n{stdout_bytes.decode('utf-8', errors='replace')}\n"
                    f"STDERR:\n{stderr_bytes.decode('utf-8', errors='replace')}"
                )
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        result = {
            "python_executable": str(python_executable),
            "returncode": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

        if process.returncode != 0:
            raise RuntimeError(
                "Python 代码执行失败。\n"
                f"退出码: {process.returncode}\n"
                f"STDOUT:\n{stdout_text}\n"
                f"STDERR:\n{stderr_text}"
            )

        return result

    return python_exec


def create_system_time_emitter_tool(session_id: str, agent_id: str = "deep_agent"):
    @tool("emit_system_time_messages")
    async def emit_system_time_messages(
        interval_seconds: float = 1.0,
        message_count: int = 5,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """按固定间隔向后台消息总线发送当前系统时间。"""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if message_count <= 0:
            raise ValueError("message_count 必须大于 0")

        resolved_source_id = source_id or f"time_{session_id}_{int(time.time() * 1000)}"
        emitted_messages = []
        message_service = BackgroundMessageBus.get_instance()

        for index in range(message_count):
            current_time = datetime.now().isoformat(timespec="seconds")
            message = message_service.emit(
                session_id,
                agent_id,
                current_time,
                kind=BackgroundMessageKind.normal,
                source_id=resolved_source_id,
                payload={
                    "index": index + 1,
                    "message_count": message_count,
                    "interval_seconds": interval_seconds,
                },
            )
            emitted_messages.append(message.model_dump(mode="json"))

            if index < message_count - 1:
                await asyncio.sleep(interval_seconds)

        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "source_id": resolved_source_id,
            "interval_seconds": interval_seconds,
            "message_count": message_count,
            "messages": emitted_messages,
        }

    return emit_system_time_messages


def create_monitor_session_agent_end_tool(session_id: str, agent_id: str = "deep_agent"):
    @tool("monitor_session_agent_end")
    async def monitor_session_agent_end(
        target_session_id: str,
        timeout_seconds: int = 3000,
        poll_interval_seconds: float = 1.0,
        max_events: int | None = None,
    ) -> dict[str, Any]:
        """开启后台任务，持续监控特定session的AGENT_END事件，每当该事件发生时则将agent最后输出的文本以打断参数转发到后台消息队列"""
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        if max_events is not None and max_events <= 0:
            raise ValueError("max_events 必须为正整数或 None")

        submitted_at = datetime.now()
        monitor_source_id = f"monitor:{target_session_id}:{uuid.uuid4().hex[:12]}"

        async def _monitor_background_task() -> dict[str, Any]:
            job_event_bus = JobEventBus.get_instance()
            message_bus = BackgroundMessageBus.get_instance()
            from app.services.job_service import JobService

            job_service = JobService.get_instance()
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            seen_event_ids: set[str] = set()
            emitted_events: list[dict[str, Any]] = []
            emitted_count = 0

            while True:
                target_jobs = await job_service.list(session_id=target_session_id)
                target_jobs.sort(key=lambda item: item.created_at)

                for target_job in target_jobs:
                    events = await job_event_bus.list_events(target_job.job_id, limit=1000)
                    events.sort(key=lambda item: item.timestamp)

                    for event in events:
                        if event.event_id in seen_event_ids:
                            continue
                        seen_event_ids.add(event.event_id)

                        if event.type != EventType.AGENT_END:
                            continue
                        if event.timestamp <= submitted_at:
                            continue

                        final_text = event.payload.get("final_text")
                        if not final_text:
                            continue

                        emitted_message = message_bus.emit(
                            session_id,
                            agent_id,
                            final_text,
                            kind=BackgroundMessageKind.interrupt,
                            source_id=monitor_source_id,
                            payload={
                                "target_session_id": target_session_id,
                                "target_job_id": target_job.job_id,
                                "target_event_id": event.event_id,
                                "target_event_timestamp": event.timestamp.isoformat(),
                                "final_text": final_text,
                                "monitor_source_id": monitor_source_id,
                                "sequence": emitted_count + 1,
                            },
                        )

                        emitted_count += 1
                        emitted_events.append(
                            {
                                "target_session_id": target_session_id,
                                "target_job_id": target_job.job_id,
                                "target_event_id": event.event_id,
                                "target_event_timestamp": event.timestamp.isoformat(),
                                "final_text": final_text,
                                "emitted_background_message": emitted_message.model_dump(mode="json"),
                            }
                        )

                        if max_events is not None and emitted_count >= max_events:
                            return {
                                "target_session_id": target_session_id,
                                "monitor_source_id": monitor_source_id,
                                "emitted_count": emitted_count,
                                "timed_out": False,
                                "events": emitted_events,
                            }

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if emitted_count == 0:
                        raise TimeoutError(f"监控 session {target_session_id} 的 AGENT_END 超时")
                    return {
                        "target_session_id": target_session_id,
                        "monitor_source_id": monitor_source_id,
                        "emitted_count": emitted_count,
                        "timed_out": True,
                        "events": emitted_events,
                    }

                await asyncio.sleep(min(poll_interval_seconds, remaining))

        handle = BackgroundTaskRegistry.get_instance().spawn(
            session_id=session_id,
            task_name="monitor_session_agent_end",
            runner=_monitor_background_task,
            metadata={
                "target_session_id": target_session_id,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "max_events": max_events,
                "source_id": monitor_source_id,
                "submitted_at": submitted_at.isoformat(),
            },
        )

        return handle.to_dict()

    return monitor_session_agent_end


def create_background_message_collection_tool(session_id: str, agent_id: str = "deep_agent"):
    @tool("collect_background_messages")
    async def collect_background_messages(
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
    ) -> dict[str, Any]:
        """持续收集当前 session/agent 的后台消息。"""
        batch = await BackgroundMessageBus.get_instance().collect(
            session_id,
            agent_id,
            source_id=source_id,
            after_message_id=after_message_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            stop_on_interrupt=stop_on_interrupt,
        )
        return batch.model_dump(mode="json")

    return collect_background_messages


def create_send_message_to_session_tool(sender_agent_id: str = "deep_agent"):
    @tool("send_message_to_session")
    async def send_message_to_session(
        target_session_id: str,
        content: str,
    ) -> dict[str, Any]:
        """模拟用户向目标 session 发送消息，并立即启动目标 session 的新任务。"""
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if not content.strip():
            raise ValueError("content 不能为空")

        run_request = MessageRunRequest(
            message=MessageCreate(
                role="user",
                content=content,
            ),
            run=RunOptions(
                mode=RunMode.single_agent,
                agent_id=sender_agent_id,
            ),
        )

        result = await MessageService.get_instance().create_and_run(target_session_id, run_request)
        return result.model_dump(mode="json")

    return send_message_to_session


def build_default_tools(
    session_id: str,
    agent_id: str = "deep_agent",
    sender_agent_id: str = "deep_agent",
) -> list:
    return [
        create_python_execution_tool(session_id=session_id, agent_id=agent_id),
        create_system_time_emitter_tool(session_id=session_id, agent_id=agent_id),
        create_monitor_session_agent_end_tool(session_id=session_id, agent_id=agent_id),
        create_background_message_collection_tool(session_id=session_id, agent_id=agent_id),
        create_send_message_to_session_tool(sender_agent_id=sender_agent_id),
    ]
