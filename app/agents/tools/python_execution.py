from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.core.env import get_project_root
from app.core.path_utils import get_workspace_root


def get_python_executable() -> Path:
    candidates = []

    env_python = os.environ.get("BOXTEAM_PYTHON_EXECUTABLE")
    if env_python:
        candidates.append(Path(env_python))

    candidates.extend(
        [
            get_workspace_root() / ".venv" / "Scripts" / "python.exe",
            get_workspace_root() / ".venv" / "bin" / "python",
            get_project_root() / ".venv" / "Scripts" / "python.exe",
            get_project_root() / ".venv" / "bin" / "python",
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
        "请确认仓库根目录或工作区根目录下存在 .venv 虚拟环境，"
        "或者通过 BOXTEAM_PYTHON_EXECUTABLE 显式指定。"
    )


def create_python_execution_tool(session_id: str, agent_id: str = "default") -> BaseTool:
    """创建用于执行 Python 代码的工具。"""
    del session_id, agent_id
    python_executable = get_python_executable()

    @tool("python_exec")
    async def python_exec(code: str, timeout_seconds: int = 30) -> dict[str, Any]:
        """使用工作区 .venv 虚拟环境中的 Python 解释器执行 Python 代码。"""
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
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
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
