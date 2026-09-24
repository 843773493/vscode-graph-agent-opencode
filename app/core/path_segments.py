"""稳定 ID 与用户显示名对应的物理路径段规则。"""

from __future__ import annotations

import re

_STABLE_ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


def physical_segment(name: str, stable_id: str) -> str:
    """返回由稳定 ID 独占的物理路径段，显示名不参与路径。"""
    del name
    if _STABLE_ID_SEGMENT.fullmatch(stable_id) is None:
        raise ValueError(f"稳定 ID 不能作为物理路径段: {stable_id!r}")
    return stable_id


def validate_generator_physical_segment(value: str) -> None:
    """校验会话生成器输入的显示名路径段。"""
    normalized = value.strip()
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"命名路径段非法: {value!r}")


__all__ = [
    "physical_segment",
    "validate_generator_physical_segment",
]
