"""稳定 ID 与用户显示名对应的物理路径段规则。"""

from __future__ import annotations

import re
import unicodedata

_INVALID_SEGMENT_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_STABLE_ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def physical_segment(name: str, stable_id: str) -> str:
    """返回由稳定 ID 独占的物理路径段，显示名不参与路径。"""
    del name
    if _STABLE_ID_SEGMENT.fullmatch(stable_id) is None:
        raise ValueError(f"稳定 ID 不能作为物理路径段: {stable_id!r}")
    return stable_id


def physical_display_segment(name: str) -> str:
    """返回安全的显示名路径段，供尚未切换的旧布局工具使用。"""
    normalized = unicodedata.normalize("NFKC", name).strip().rstrip(". ")
    normalized = _INVALID_SEGMENT_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().rstrip(". ")
    if not normalized:
        normalized = "未命名"
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    max_name_length = 96 - len("--12345678")
    normalized = normalized[:max_name_length].rstrip(". ") or "未命名"
    return normalized


def validate_generator_physical_segment(value: str) -> None:
    """校验会话生成器输入的显示名路径段。"""
    normalized = value.strip()
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"命名路径段非法: {value!r}")


def display_name_from_segment(segment: str, stable_id: str) -> str:
    """从旧布局路径段提取显示名。"""
    for suffix in (f"--{stable_id}", f"--{stable_id[-8:]}"):
        if segment.endswith(suffix):
            return segment[: -len(suffix)] or "未命名"
    return segment


__all__ = [
    "display_name_from_segment",
    "physical_display_segment",
    "physical_segment",
    "validate_generator_physical_segment",
]
