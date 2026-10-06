"""原子 selection 与受信注册目录共同生产 ToolSet，旧请求过滤链已删除。"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from app.agents.policy import ToolMetadata, ToolPolicyResolver
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


@pytest.fixture
def registered_producer(tmp_path):
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    tools = [StructuredTool.from_function(
        lambda path: path, name=name, description="文件工具",
    ) for name in ("read_file", "write_file")]
    producer = RegisteredToolSetProducer(
        agent_id="default", selection_reader=store,
        policy_resolver=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={}),
        tools=tools, metadata={tool.name: ToolMetadata(
            tool_id=tool.name, origin="builtin", kind="default", group_id="filesystem",
        ) for tool in tools},
    )
    producer.freeze_structured_output_tools({})
    return store, producer, tools


def test_visibility_changes_next_snapshot_and_preserves_owned_catalog(registered_producer):
    store, producer, tools = registered_producer
    before = producer.snapshot()
    store.apply_changes(agent_id="default", changes={"write_file": (True, False)})
    after = producer.snapshot()
    assert before.tool_names == ("read_file", "write_file")
    assert after.tool_names == ("read_file",)
    assert [tool.name for tool in tools] == ["read_file", "write_file"]
    assert before.revision != after.revision
    assert before.tool_names == ("read_file", "write_file")


def test_execution_disable_then_enable_uses_same_registered_schema(registered_producer):
    store, producer, _tools = registered_producer
    before = producer.snapshot()
    store.apply_changes(agent_id="default", changes={"write_file": (False, False)})
    assert producer.snapshot().tool_names == ("read_file",)
    store.apply_changes(agent_id="default", changes={"write_file": (True, True)})
    enabled = producer.snapshot()
    assert enabled.tools == before.tools
    assert enabled.revision == before.revision


def test_unrelated_hidden_selection_does_not_rewrite_tool_manifest(registered_producer):
    store, producer, _tools = registered_producer
    before = producer.snapshot()
    store.apply_changes(agent_id="default", changes={"external_target": (False, False)})
    after = producer.snapshot()
    assert after.selection_revision != before.selection_revision
    assert after.revision == before.revision


def test_catalog_schema_is_frozen_at_registration(registered_producer):
    _store, producer, tools = registered_producer
    before = producer.snapshot()
    tools[0].description = "不允许修改已注册快照"
    assert producer.snapshot().tools == before.tools
