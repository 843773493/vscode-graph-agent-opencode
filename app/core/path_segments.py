"""会话生成器标题与导航名称的路径段约束。"""

from __future__ import annotations


def validate_generator_name(value: str) -> None:
    """校验会话生成器输入的标题或导航名称。"""
    normalized = value.strip()
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"命名路径段非法: {value!r}")


__all__ = [
    "validate_generator_name",
]
