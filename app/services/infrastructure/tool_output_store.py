from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from langchain_core.messages import ToolMessage

from app.abstractions.resource_file_content import ResourceFileContent
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id

DEFAULT_MAX_OUTPUT_LINES = 2_000
DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024
_OUTPUT_COMPARISON_CHUNK_BYTES = 64 * 1024
TOOL_OUTPUT_ARTIFACT_KEY = "tool_output"
# 与 str.splitlines() 的边界一致，逐行取样时不创建全量行列表。
_LINE_BREAK_PATTERN = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


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
        self._path_resolver = get_session_path_resolver(
            self._workspace_root / ".boxteam" / "sessions"
        )
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

        reference = self._materialize(
            session_id=session_id, tool_name=tool_name, tool_call_id=tool_call_id,
            encoded=encoded, line_count=line_count, output_key=tool_call_id,
            truncated=True,
        )
        resource_uri = reference.read_path
        marker = (
            f"... 工具输出过大，完整内容已保存到 {resource_uri} ...\n"
            "请先用 grep 搜索该路径，再用 read_file 按行分段读取。"
        )
        preview = _bounded_structured_preview(
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
        return message.model_copy(update={"content": preview, "artifact": artifact})

    @property
    def inline_byte_limit(self) -> int:
        """自带有界结构的工具在生成结果前使用相同的最终输出上限。"""
        return self._max_bytes

    def materialize(
        self, *, session_id: str, tool_name: str, tool_call_id: str,
        text: str, output_key: str,
    ) -> ToolOutputReference:
        """保存完整的不可变工具正文供普通资源读取，不生成截断预览。

        output_key 区分同一调用内的多个派生正文；同 key 的重放仍须逐字节一致。
        """
        return self._materialize(
            session_id=session_id, tool_name=tool_name, tool_call_id=tool_call_id,
            encoded=text.encode("utf-8"), line_count=_line_count(text),
            output_key=output_key, truncated=False,
        )

    def _materialize(
        self, *, session_id: str, tool_name: str, tool_call_id: str,
        encoded: bytes, line_count: int, output_key: str, truncated: bool,
    ) -> ToolOutputReference:
        output_path = self._output_path(
            session_id=session_id,
            tool_call_id=output_key,
        )
        resource_uri = session_artifact_uri(
            read_workspace_id(self._workspace_root), session_id, "tool-results", output_path.name,
            gateway_authority=get_current_gateway_id(),
        )
        try:
            _write_exact_output(output_path, encoded, resource_uri)
        except OSError as error:
            raise type(error)(
                error.errno,
                f"工具结果 write 失败: uri={resource_uri}, error={type(error).__name__}, errno={error.errno}",
            ) from error
        return ToolOutputReference(
            type="tool_output",
            path=resource_uri,
            read_path=resource_uri,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            byte_count=len(encoded),
            line_count=line_count,
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            truncated=truncated,
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
        session_root = self._path_resolver.resolve_session_node(session_id)
        output_dir = session_root / "tool-results"
        if output_dir.is_symlink():
            raise PermissionError("工具结果目录不能是符号链接")
        filename_digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:24]
        return output_dir / f"tool_{filename_digest}.txt"

    def read(self, session_id: str, uri: str, max_bytes: int) -> ResourceFileContent:
        parsed = parse_session_artifact_uri(uri, local_gateway_id=get_current_gateway_id())
        if (parsed.gateway_authority is not None or parsed.scope_id != read_workspace_id(self._workspace_root)
                or parsed.tail_segments[:2] != (session_id, "tool-results")):
            raise PermissionError("工具结果引用不属于当前工作区会话")
        try:
            session_node = self._path_resolver.resolve_session_node(session_id)
        except KeyError as error:
            raise FileNotFoundError(f"工具结果所属会话不存在: session_id={session_id}") from error
        directory = session_node / "tool-results"
        path = directory / parsed.tail_segments[2]
        if directory.is_symlink() or path.is_symlink():
            raise PermissionError("工具结果及其目录不能是符号链接")
        try:
            with path.open("rb") as stream:
                data = stream.read(max_bytes + 1)
        except OSError as error:
            raise type(error)(
                error.errno,
                f"工具结果 read 失败: uri={uri}, error={type(error).__name__}, errno={error.errno}",
            ) from error
        return ResourceFileContent(data=data, content_type="text/plain")


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


def _write_exact_output(path: Path, content: bytes, resource_uri: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not _file_matches_bytes(path, content):
            raise RuntimeError(f"同一 tool_call_id 对应的工具输出发生变化: {resource_uri}")
        return

    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary_path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        if not _file_matches_bytes(path, content):
            raise RuntimeError(f"同一 tool_call_id 对应的工具输出发生变化: {resource_uri}")
    finally:
        temporary_path.unlink(missing_ok=True)


def _file_matches_bytes(path: Path, expected: bytes) -> bool:
    if path.stat().st_size != len(expected):
        return False
    with path.open("rb") as stream:
        for offset in range(0, len(expected), _OUTPUT_COMPARISON_CHUNK_BYTES):
            actual_chunk = stream.read(_OUTPUT_COMPARISON_CHUNK_BYTES)
            expected_chunk = expected[offset : offset + _OUTPUT_COMPARISON_CHUNK_BYTES]
            if actual_chunk != expected_chunk:
                return False
        return stream.read(1) == b""


def _take_prefix_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _take_suffix_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _iter_source_lines(text: str) -> Iterator[str]:
    """按 splitlines 的边界产出行，避免复制整份文本的行列表。"""
    if not text:
        return
    start = 0
    for line_break in _LINE_BREAK_PATTERN.finditer(text):
        yield text[start:line_break.start()]
        start = line_break.end()
    if start < len(text):
        yield text[start:]


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

    available_lines = max_lines - marker.count("\n") - 4
    head_line_count = (available_lines + 1) // 2
    tail_line_count = available_lines // 2
    head_lines: list[str] = []
    tail_lines: deque[str] = deque(maxlen=max(0, tail_line_count))
    for line_index, line in enumerate(_iter_source_lines(text)):
        if line_index < head_line_count:
            head_lines.append(line)
        tail_lines.append(line)
    head = "\n".join(head_lines)
    tail = "\n".join(tail_lines) if tail_line_count else ""

    available_bytes = max_bytes - fixed_bytes
    head = _take_prefix_bytes(head, (available_bytes + 1) // 2)
    tail = _take_suffix_bytes(tail, available_bytes // 2)
    return f"{head.rstrip()}{separator}{marker}{separator}{tail.lstrip()}"


def _bounded_structured_preview(
    text: str,
    *,
    marker: str,
    max_lines: int,
    max_bytes: int,
) -> str:
    """截断工具输出，同时保持 JSON object 类型的工具结果仍可解析。"""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _bounded_preview(
            text,
            marker=marker,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("output"), str):
        return _bounded_preview(
            text,
            marker=marker,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output = payload["output"]
    marker_bytes = len(("\n\n" + marker + "\n\n").encode("utf-8"))
    lower_bound = marker_bytes + 1
    upper_bound = max_bytes
    best: str | None = None
    while lower_bound <= upper_bound:
        preview_budget = (lower_bound + upper_bound) // 2
        candidate_payload = dict(payload)
        candidate_payload["output"] = _bounded_preview(
            output,
            marker=marker,
            max_lines=max_lines,
            max_bytes=preview_budget,
        )
        candidate = json.dumps(candidate_payload, ensure_ascii=False)
        if len(candidate.encode("utf-8")) <= max_bytes:
            best = candidate
            lower_bound = preview_budget + 1
        else:
            upper_bound = preview_budget - 1

    if best is None:
        raise ValueError("结构化工具输出的元数据超过最大预览字节数")
    return best
