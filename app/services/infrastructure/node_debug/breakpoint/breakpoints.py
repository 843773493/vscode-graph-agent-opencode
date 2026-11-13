from __future__ import annotations

import hashlib
import stat
from functools import cached_property
from pathlib import Path

from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationBreakpointDTO,
)


class BreakpointSource:
    """一次源码观察；同文件的摘要、断点核对和锚点共用原始字节。"""

    def __init__(self, path: Path, *, uri: str) -> None:
        self.uri = uri
        self._source = _source_bytes(path, uri)
        self.digest = hashlib.sha256(self._source).hexdigest() if self._source is not None else None

    @cached_property
    def _lines(self) -> list[str]:
        if self._source is None:
            raise FileNotFoundError(f"源码断点文件不存在: {self.uri}")
        try:
            return self._source.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise RuntimeError(f"源码断点文件不是 UTF-8 文本: {self.uri}") from error

    def anchor(
        self,
        breakpoint: NodeDebugBreakpointDTO,
    ) -> NodeDebugBreakpointDTO:
        source_lines = self._lines
        anchored_line = breakpoint.line
        if anchored_line > len(source_lines):
            raise ValueError(
                f"源码断点行号超出文件范围: {breakpoint.path}:{anchored_line}，"
                f"文件共 {len(source_lines)} 行"
            )
        return breakpoint.model_copy(
            update={
                "line": anchored_line,
                "original_line": breakpoint.original_line or breakpoint.line,
                "source_line": source_lines[anchored_line - 1],
                "previous_line": source_lines[anchored_line - 2] if anchored_line > 1 else None,
                "next_line": source_lines[anchored_line] if anchored_line < len(source_lines) else None,
                "source_digest": self.digest,
                "relocation_status": "current",
                "relocation_message": None,
            }
        )

    def reconcile(self, breakpoint: NodeDebugBreakpointDTO) -> NodeDebugBreakpointDTO:
        if self._source is None:
            return breakpoint.model_copy(
                update={
                    "verified": False,
                    "actual_line": None,
                    "relocation_status": "source_deleted",
                    "relocation_message": f"断点源文件已删除: {breakpoint.path}",
                }
            )
        if breakpoint.relocation_status in {"pending_update", "source_deleted"}:
            # 文件恢复后仍须显式重设，不能把旧断点悄悄绑定到其它代码。
            return breakpoint
        if breakpoint.source_digest is None or breakpoint.source_line is None:
            return self.anchor(breakpoint)
        if self.digest == breakpoint.source_digest:
            return breakpoint
        return breakpoint.model_copy(
            update={
                "verified": False,
                "actual_line": None,
                "relocation_status": "pending_update",
                "relocation_message": (
                    "源码已变化，断点未自动重定位；请检查后重新设置 "
                    f"{breakpoint.path}:{breakpoint.line}"
                ),
            }
        )


def persistable_breakpoint(
    breakpoint: NodeDebugBreakpointDTO,
) -> NodeDebugBreakpointDTO:
    return breakpoint.model_copy(
        update={
            "verified": False,
            "actual_line": None,
        },
        deep=True,
    )


def portable_breakpoint(
    breakpoint: NodeDebugBreakpointDTO,
) -> NodeDebugConfigurationBreakpointDTO:
    """移除运行时字段，生成可直接复制的方案断点。"""

    return NodeDebugConfigurationBreakpointDTO(
        breakpoint_id=breakpoint.breakpoint_id,
        path=breakpoint.path,
        line=breakpoint.line,
        column=breakpoint.column,
        condition=breakpoint.condition,
        hit_condition=breakpoint.hit_condition,
        log_message=breakpoint.log_message,
        original_line=breakpoint.original_line,
        source_line=breakpoint.source_line,
        previous_line=breakpoint.previous_line,
        next_line=breakpoint.next_line,
        source_digest=breakpoint.source_digest,
        relocation_status=breakpoint.relocation_status,
        relocation_message=breakpoint.relocation_message,
        created_at=breakpoint.created_at,
    )


def runtime_breakpoint(
    breakpoint: NodeDebugConfigurationBreakpointDTO,
) -> NodeDebugBreakpointDTO:
    """从方案断点构建尚未安装到 Inspector 的运行时断点。"""

    return NodeDebugBreakpointDTO(
        breakpoint_id=breakpoint.breakpoint_id,
        path=breakpoint.path,
        line=breakpoint.line,
        column=breakpoint.column,
        condition=breakpoint.condition,
        hit_condition=breakpoint.hit_condition,
        log_message=breakpoint.log_message,
        original_line=breakpoint.original_line,
        source_line=breakpoint.source_line,
        previous_line=breakpoint.previous_line,
        next_line=breakpoint.next_line,
        source_digest=breakpoint.source_digest,
        relocation_status=breakpoint.relocation_status,
        relocation_message=breakpoint.relocation_message,
        created_at=breakpoint.created_at,
    )


def _source_bytes(path: Path, uri: str) -> bytes | None:
    """只读取一次源码；初始缺失与检查后的读取失败保持不同语义。"""
    try:
        try:
            mode = path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not stat.S_ISREG(mode):
            return None
        return path.read_bytes()
    except OSError as error:
        raise type(error)(
            error.errno,
            f"断点源码读取失败: {uri}, error={type(error).__name__}, errno={error.errno}",
        ) from error


__all__ = [
    "BreakpointSource",
    "persistable_breakpoint",
    "portable_breakpoint",
    "runtime_breakpoint",
]
