"""AIMessage.content 的运行时 Schema。

LiteLLM 的 ``reasoning_content`` 和 ``reasoning_items`` 是响应对象上的独立
字段。项目把它们包装成同名 carrier block，只为在一个有序 content 列表中
保留字段名和出现位置；carrier 不是 LiteLLM 的 wire protocol，也不是新的
provider 语义。
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


class ContentBlockSchema(BaseModel):
    """单个 content block 的宽松 provider schema。

    provider 可能随时增加字段，因此未知字段必须保留。只有项目自己定义的
    两类 carrier 需要额外校验字段类型和字段归属。
    """

    model_config = ConfigDict(extra="allow")

    type: str
    reasoning_content: str | None = None
    reasoning_items: list[dict[str, Any]] | None = None

    _carrier_fields: ClassVar[frozenset[str]] = frozenset(
        {"reasoning_content", "reasoning_items"}
    )

    @model_validator(mode="before")
    @classmethod
    def validate_carrier_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise TypeError("AIMessage.content 的每个 block 必须是对象")
        block_type = value.get("type")
        if not isinstance(block_type, str) or not block_type:
            raise ValueError("AIMessage.content block 必须包含非空 type")

        present = cls._carrier_fields.intersection(value)
        if block_type == "reasoning_content":
            if present != {"reasoning_content"}:
                raise ValueError(
                    "reasoning_content carrier 只能携带 reasoning_content 字段"
                )
            if not isinstance(value["reasoning_content"], str):
                raise TypeError("reasoning_content carrier 的值必须是字符串")
        elif block_type == "reasoning_items":
            if present != {"reasoning_items"}:
                raise ValueError(
                    "reasoning_items carrier 只能携带 reasoning_items 字段"
                )
            items = value["reasoning_items"]
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise TypeError(
                    "reasoning_items carrier 的值必须是对象数组"
                )
        elif present:
            raise ValueError(
                "reasoning_content/reasoning_items 只能出现在同名 carrier block 中"
            )
        return value


def validate_content_blocks(content: Any) -> Any:
    """校验并深复制 canonical content，不改变原始字段和字段顺序。

    字符串 content 仍是 LangChain 允许的空文本/纯文本表示；一旦 content
    使用列表，每个元素都必须是可校验的对象。函数返回输入的深复制，而不是
    ``model_dump()``，避免 Pydantic 默认值或字段重排污染 provider 原文。
    """

    if isinstance(content, str):
        return copy.deepcopy(content)
    if not isinstance(content, list):
        raise TypeError("AIMessage.content 必须是字符串或 block 数组")
    for block in content:
        ContentBlockSchema.model_validate(block)
    return copy.deepcopy(content)

