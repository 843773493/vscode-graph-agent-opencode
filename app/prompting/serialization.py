from __future__ import annotations

import json


def serialize_prompt_json(value: object) -> str:
    """序列化嵌入结构化提示的 JSON，并阻止数据伪造标签边界。"""
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


__all__ = ["serialize_prompt_json"]
