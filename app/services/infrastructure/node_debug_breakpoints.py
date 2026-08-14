from __future__ import annotations

import hashlib
from pathlib import Path

from app.schemas.public_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationBreakpointDTO,
)


def source_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def anchor_breakpoint(
    breakpoint: NodeDebugBreakpointDTO,
    path: Path,
    *,
    line: int | None = None,
    relocation_status: str = "current",
    relocation_message: str | None = None,
) -> NodeDebugBreakpointDTO:
    source_lines = _source_lines(path)
    anchored_line = line or breakpoint.line
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
            "previous_line": (
                source_lines[anchored_line - 2] if anchored_line > 1 else None
            ),
            "next_line": (
                source_lines[anchored_line]
                if anchored_line < len(source_lines)
                else None
            ),
            "source_digest": source_digest(path),
            "relocation_status": relocation_status,
            "relocation_message": relocation_message,
        }
    )


def reconcile_breakpoint(
    breakpoint: NodeDebugBreakpointDTO,
    path: Path,
) -> NodeDebugBreakpointDTO:
    current_digest = source_digest(path)
    if current_digest is None:
        return breakpoint.model_copy(
            update={
                "verified": False,
                "actual_line": None,
                "inspector_id": None,
                "relocation_status": "source_deleted",
                "relocation_message": f"断点源文件已删除: {breakpoint.path}",
            }
        )
    if breakpoint.relocation_status in {"pending_update", "source_deleted"}:
        # 源码变化后的断点必须由用户或 Agent 显式重新设置；即使文件恢复，
        # 也不能根据旧锚点自动恢复，避免把断点悄悄绑定到错误代码。
        return breakpoint
    if breakpoint.source_digest is None or breakpoint.source_line is None:
        return anchor_breakpoint(breakpoint, path)
    if current_digest == breakpoint.source_digest:
        return breakpoint

    return breakpoint.model_copy(
        update={
            "verified": False,
            "actual_line": None,
            "inspector_id": None,
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
            "inspector_id": None,
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


def _source_lines(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"源码断点文件不存在: {path}")
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RuntimeError(f"源码断点文件不是 UTF-8 文本: {path}") from error


__all__ = [
    "anchor_breakpoint",
    "persistable_breakpoint",
    "portable_breakpoint",
    "reconcile_breakpoint",
    "runtime_breakpoint",
    "source_digest",
]
