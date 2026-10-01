from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from deepagents.backends import CompositeBackend
from langchain_core.messages import AnyMessage, get_buffer_string

from app.agents.workspace_backend import build_workspace_backend


class ContextHistoryStore:
    def __init__(self, workspace_root: Path) -> None:
        self._backend = build_workspace_backend(workspace_root)

    @property
    def backend(self) -> CompositeBackend:
        return self._backend

    async def offload_history(
        self,
        *,
        session_id: str,
        messages: list[AnyMessage],
    ) -> str:
        backend_path = f"/session-artifacts/{session_id}/context/history.md"
        timestamp = datetime.now(UTC).isoformat()
        new_section = (
            f"## Summarized at {timestamp}\n\n"
            f"{get_buffer_string(messages)}\n\n"
        )

        responses = await self._backend.adownload_files([backend_path])
        existing_content = ""
        if responses:
            response = responses[0]
            if response.error is None and response.content is not None:
                existing_content = response.content.decode("utf-8")
            elif response.error != "file_not_found":
                raise RuntimeError(
                    "读取 compact 历史文件失败: "
                    f"path={backend_path}, error={response.error}"
                )

        combined_content = existing_content + new_section
        if existing_content:
            result = await self._backend.aedit(
                backend_path,
                existing_content,
                combined_content,
            )
        else:
            result = await self._backend.awrite(backend_path, combined_content)

        if result is None or result.error:
            raise RuntimeError(
                "写入 compact 历史文件失败: "
                f"path={backend_path}, "
                f"error={result.error if result else 'backend returned None'}"
            )

        return backend_path.removeprefix("/")
