from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from langchain_core.messages import ToolMessage

from app.core.path_utils import safe_join


DEFAULT_MAX_OUTPUT_LINES = 2_000
DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024
TOOL_OUTPUT_ARTIFACT_KEY = "tool_output"


@dataclass(frozen=True, slots=True)
class ToolOutputReference:
    """完整工具输出在工作区内的稳定引用。"""

    type: str
    path: str
    read_path: str
    tool_name: str
    tool_call_id: str
    byte_count: int
    line_count: int
    content_sha256: str
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ToolOutputStore:
    """将过大的文本工具结果物化到会话目录，并返回有界预览。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if max_lines < 8:
            raise ValueError("tool output max_lines 不能小于 8")
        if max_bytes < 1_024:
            raise ValueError("tool output max_bytes 不能小于 1024")
        self._workspace_root = workspace_root.resolve()
        self._max_lines = max_lines
        self._max_bytes = max_bytes

    def bound(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_call_id: str,
        message: ToolMessage,
    ) -> ToolMessage:
        text = _text_content(message.content)
        if text is None:
            return message

        encoded = text.encode("utf-8")
        line_count = _line_count(text)
        if line_count <= self._max_lines and len(encoded) <= self._max_bytes:
            return message

        content_sha256 = hashlib.sha256(encoded).hexdigest()
        output_path = self._output_path(
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        _write_exact_output(output_path, encoded)
        relative_path = output_path.relative_to(self._workspace_root).as_posix()
        read_path = f"/{relative_path}"
        reference = ToolOutputReference(
            type="tool_output",
            path=relative_path,
            read_path=read_path,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            byte_count=len(encoded),
            line_count=line_count,
            content_sha256=content_sha256,
            truncated=True,
        )
        marker = (
            f"... 工具输出过大，完整内容已保存到 {relative_path} ...\n"
            f"模型读取路径：{read_path}\n"
            "请先用 grep 搜索该路径，再用 read_file 按行分段读取。"
        )
        preview = _bounded_preview(
            text,
            marker=marker,
            max_lines=self._max_lines,
            max_bytes=self._max_bytes,
        )
        artifact: dict[str, object] = {
            TOOL_OUTPUT_ARTIFACT_KEY: reference.to_dict(),
        }
        if message.artifact is not None:
            artifact["original_tool_artifact"] = message.artifact
        return message.model_copy(
            update={
                "content": preview,
                "artifact": artifact,
            }
        )

    async def abound(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_call_id: str,
        message: ToolMessage,
    ) -> ToolMessage:
        return await asyncio.to_thread(
            self.bound,
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            message=message,
        )

    def _output_path(self, *, session_id: str, tool_call_id: str) -> Path:
        sessions_root = self._workspace_root / ".boxteam" / "sessions"
        session_root = safe_join(sessions_root, session_id)
        output_dir = session_root / "tool-results"
        filename_digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:24]
        return output_dir / f"tool_{filename_digest}.txt"


def extract_tool_output_reference(message: object) -> dict[str, object] | None:
    """从 ToolMessage.artifact 中读取可公开的工具输出引用。"""

    artifact = getattr(message, "artifact", None)
    if not isinstance(artifact, Mapping):
        return None
    reference = artifact.get(TOOL_OUTPUT_ARTIFACT_KEY)
    if not isinstance(reference, Mapping):
        return None
    return {str(key): value for key, value in reference.items()}


def _text_content(content: str | list[str | dict[object, object]]) -> str | None:
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, Mapping):
            return None
        block_type = block.get("type")
        text = block.get("text")
        if block_type not in {"text", "plain_text"} or not isinstance(text, str):
            return None
        parts.append(text)
    return "".join(parts)


def _line_count(text: str) -> int:
    return text.count("\n") + 1


def _write_exact_output(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing != content:
            raise RuntimeError(f"同一 tool_call_id 对应的工具输出发生变化: {path}")
        return

    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary_path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        existing = path.read_bytes()
        if existing != content:
            raise RuntimeError(f"同一 tool_call_id 对应的工具输出发生变化: {path}")
    finally:
        temporary_path.unlink(missing_ok=True)


def _take_prefix_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _take_suffix_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _bounded_preview(
    text: str,
    *,
    marker: str,
    max_lines: int,
    max_bytes: int,
) -> str:
    separator = "\n\n"
    fixed_bytes = len((separator + marker + separator).encode("utf-8"))
    if fixed_bytes >= max_bytes:
        raise ValueError("工具输出 marker 超过最大预览字节数")

    source_lines = text.splitlines()
    available_lines = max_lines - marker.count("\n") - 4
    head_line_count = (available_lines + 1) // 2
    tail_line_count = available_lines // 2
    head = "\n".join(source_lines[:head_line_count])
    tail = "\n".join(source_lines[-tail_line_count:]) if tail_line_count else ""

    available_bytes = max_bytes - fixed_bytes
    head = _take_prefix_bytes(head, (available_bytes + 1) // 2)
    tail = _take_suffix_bytes(tail, available_bytes // 2)
    return f"{head.rstrip()}{separator}{marker}{separator}{tail.lstrip()}"
