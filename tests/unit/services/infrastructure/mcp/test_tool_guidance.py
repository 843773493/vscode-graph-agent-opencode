"""McpToolGuidanceProducer focused 合同测试（OpenSpec E4 第二段）。

覆盖：确定性派生、空目录固定 envelope、不可信 description 有界清洗、
增删改 delta、tombstone 稳定、dirty 输入 fail closed。
"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.services.infrastructure.mcp.catalog_owner import McpToolDescriptor
from app.services.infrastructure.mcp.tool_guidance import (
    MAX_GUIDANCE_DESCRIPTION_LENGTH,
    McpToolGuidanceError,
    McpToolGuidanceProducer,
)


class _EchoInput(BaseModel):
    text: str
    level: int


def _make_remote_tool(name: str) -> StructuredTool:
    async def _call(text: str, level: int = 0) -> str:
        return f"{name}:{text}:{level}"

    return StructuredTool.from_function(
        coroutine=_call,
        name=name,
        description=f"{name} 工具",
        args_schema=_EchoInput,
    )


def _descriptor(
    tool_id: str,
    *,
    description: str = "工具说明",
    server_id: str = "mini",
) -> McpToolDescriptor:
    return McpToolDescriptor(
        tool_id=tool_id,
        server_id=server_id,
        remote_name=tool_id.split("__")[-1],
        description=description,
    )


def test_produce_is_deterministic_and_sorted() -> None:
    producer = McpToolGuidanceProducer()
    descriptors = [
        _descriptor("mcp__mini__zeta"),
        _descriptor("mcp__mini__alpha"),
    ]
    tools_by_id = {
        "mcp__mini__zeta": _make_remote_tool("zeta"),
        "mcp__mini__alpha": _make_remote_tool("alpha"),
    }
    first = producer.produce(
        catalog_revision="sha256:" + "0" * 64,
        descriptors=descriptors,
        tools_by_id=tools_by_id,
    )
    second = producer.produce(
        catalog_revision="sha256:" + "0" * 64,
        descriptors=list(reversed(descriptors)),
        tools_by_id=tools_by_id,
    )
    assert first == second
    assert [entry.tool_id for entry in first.entries] == [
        "mcp__mini__alpha",
        "mcp__mini__zeta",
    ]
    assert first.added_tool_ids == ("mcp__mini__alpha", "mcp__mini__zeta")
    # 参数摘要有界且确定性：字段按名排序，类型取 schema type。
    assert first.entries[0].args_summary == "level:integer, text:string"


def test_empty_catalog_fixed_envelope() -> None:
    producer_a = McpToolGuidanceProducer()
    producer_b = McpToolGuidanceProducer()
    snapshot_a = producer_a.produce(
        catalog_revision="sha256:" + "1" * 64,
        descriptors=[],
        tools_by_id={},
    )
    snapshot_b = producer_b.produce(
        catalog_revision="sha256:" + "1" * 64,
        descriptors=[],
        tools_by_id={},
    )
    assert snapshot_a.entries == ()
    assert snapshot_a.tombstones == ()
    assert snapshot_a.added_tool_ids == ()
    assert snapshot_a.guidance_revision == snapshot_b.guidance_revision
    assert snapshot_a == snapshot_b


def test_untrusted_description_bounded_and_sanitized() -> None:
    producer = McpToolGuidanceProducer()
    dirty_description = "a\x00b\x1fc" + "长" * 500
    snapshot = producer.produce(
        catalog_revision="sha256:" + "0" * 64,
        descriptors=[_descriptor("mcp__mini__echo", description=dirty_description)],
        tools_by_id={"mcp__mini__echo": _make_remote_tool("echo")},
    )
    description = snapshot.entries[0].description
    assert len(description) <= MAX_GUIDANCE_DESCRIPTION_LENGTH
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in description)


def test_delta_add_modify_tombstone() -> None:
    producer = McpToolGuidanceProducer()
    revision = "sha256:" + "0" * 64
    tools = {
        "mcp__mini__alpha": _make_remote_tool("alpha"),
        "mcp__mini__beta": _make_remote_tool("beta"),
    }
    first = producer.produce(
        catalog_revision=revision,
        descriptors=[_descriptor("mcp__mini__alpha"), _descriptor("mcp__mini__beta")],
        tools_by_id=tools,
    )
    # 修改 beta 描述、删除 alpha、新增 gamma。
    second_tools = {
        "mcp__mini__beta": _make_remote_tool("beta"),
        "mcp__mini__gamma": _make_remote_tool("gamma"),
    }
    second = producer.produce(
        catalog_revision=revision,
        descriptors=[
            _descriptor("mcp__mini__beta", description="修改后的说明"),
            _descriptor("mcp__mini__gamma"),
        ],
        tools_by_id=second_tools,
        previous=first,
    )
    assert second.added_tool_ids == ("mcp__mini__gamma",)
    assert second.modified_tool_ids == ("mcp__mini__beta",)
    assert second.tombstones == ("mcp__mini__alpha",)
    assert second.entries[0].description == "修改后的说明"


def test_tombstone_stable_after_next_unchanged_produce() -> None:
    producer = McpToolGuidanceProducer()
    revision = "sha256:" + "0" * 64
    first = producer.produce(
        catalog_revision=revision,
        descriptors=[_descriptor("mcp__mini__alpha")],
        tools_by_id={"mcp__mini__alpha": _make_remote_tool("alpha")},
    )
    removed = producer.produce(
        catalog_revision=revision,
        descriptors=[],
        tools_by_id={},
        previous=first,
    )
    assert removed.tombstones == ("mcp__mini__alpha",)
    # 目录未再变化时重新派生：delta 全空，tombstone 不重复出现。
    stable = producer.produce(
        catalog_revision=revision,
        descriptors=[],
        tools_by_id={},
        previous=removed,
    )
    assert stable.tombstones == ()
    assert stable.added_tool_ids == ()
    assert stable.modified_tool_ids == ()
    assert stable.guidance_revision == removed.guidance_revision


def test_dirty_descriptor_fails_closed() -> None:
    producer = McpToolGuidanceProducer()
    with pytest.raises(McpToolGuidanceError):
        producer.produce(
            catalog_revision="sha256:" + "0" * 64,
            descriptors=[_descriptor("mcp__mini__missing")],
            tools_by_id={"mcp__mini__echo": _make_remote_tool("echo")},
        )
