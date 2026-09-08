"""已接受消息内容的无 I/O block view。

这里仅把已有消息内容展开为可遍历的 block，不执行 provider 响应归一化、
schema 选择或外部请求编码。provider 响应的归一化仍归
``app.agents.providers``。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def direct_content_blocks(content: Any) -> list[dict[str, Any]]:
    """将已接受的消息 content 展开为独立副本，保持原有顺序。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            if value:
                blocks.append({"type": "text", "text": value})
            continue
        if isinstance(value, Mapping):
            blocks.append(
                {str(key): copy.deepcopy(item) for key, item in value.items()}
            )
            continue
        if value is not None:
            blocks.append({"type": "text", "text": str(value)})
    return blocks


__all__ = ["direct_content_blocks"]
