"""原子 selection 与受信注册目录共同生产 ToolSet，旧请求过滤链已删除。"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from app.agents.policy import ToolMetadata, ToolPolicyResolver
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


@pytest.fixture
def registered_producer_factory(tmp_path):
    store_index = 0

    def create(tool_names=("read_file", "write_file"), *, include_structured_output=False):
        nonlocal store_index
        store = ToolSelectionStore(boxteam_root=tmp_path / f".boxteam-{store_index}")
        store_index += 1
        tools = [
            StructuredTool.from_function(
                lambda path: path,
                name=name,
                description="文件工具",
            )
            for name in tool_names
        ]
        producer = RegisteredToolSetProducer(
            agent_id="default",
            selection_reader=store,
            policy_resolver=ToolPolicyResolver(
                policy_defaults={}, policy_rules={}, restrictions={}
            ),
            tools=tools,
            metadata={
                tool.name: ToolMetadata(
                    tool_id=tool.name,
                    origin="builtin",
                    kind="default",
                    group_id="filesystem",
                )
                for tool in tools
            },
        )
        structured_tools = (
            {
                "structured_response": StructuredTool.from_function(
                    lambda value: value,
                    name="structured_response",
                    description="结构化响应",
                )
            }
            if include_structured_output
            else {}
        )
        producer.freeze_structured_output_tools(structured_tools)
        return store, producer, tools

    return create


@pytest.fixture
def registered_producer(registered_producer_factory):
    return registered_producer_factory()


def test_visibility_changes_next_snapshot_and_preserves_owned_catalog(
    registered_producer,
):
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


def test_snapshot_sorts_wire_order_by_name_independent_of_registration_order(
    registered_producer_factory,
):
    _store, producer, _tools = registered_producer_factory(("write_file", "read_file"))
    assert producer.snapshot().wire_tool_order == ("read_file", "write_file")


def test_registered_producer_factory_isolates_selection_stores(
    registered_producer_factory,
):
    first_store, first_producer, _first_tools = registered_producer_factory()
    first_store.apply_changes(agent_id="default", changes={"write_file": (True, False)})
    assert first_producer.snapshot().tool_names == ("read_file",)

    _second_store, second_producer, _second_tools = registered_producer_factory()
    assert second_producer.snapshot().tool_names == ("read_file", "write_file")


def test_snapshot_caches_default_visibility_but_resolves_overrides(
    registered_producer,
    monkeypatch: pytest.MonkeyPatch,
):
    store, producer, _tools = registered_producer
    resolve_policy = producer._policy_resolver.resolve
    resolved_tool_names = []

    def track_resolution(metadata, **overrides):
        resolved_tool_names.append(metadata.tool_id)
        return resolve_policy(metadata, **overrides)

    monkeypatch.setattr(producer._policy_resolver, "resolve", track_resolution)

    assert producer.snapshot().tool_names == ("read_file", "write_file")
    assert producer.snapshot().tool_names == ("read_file", "write_file")
    assert resolved_tool_names == ["read_file", "write_file"]

    store.apply_changes(agent_id="default", changes={"write_file": (True, False)})

    assert producer.snapshot().tool_names == ("read_file",)
    assert resolved_tool_names == ["read_file", "write_file", "write_file"]


def test_snapshot_reuses_immutable_output_for_same_selection_and_options(
    registered_producer_factory,
):
    store, producer, _tools = registered_producer_factory(
        include_structured_output=True
    )
    normal = producer.snapshot()

    assert producer.snapshot() is normal

    structured = producer.snapshot(include_structured_output=True)
    assert structured is not normal
    assert structured.wire_tool_order == (
        *normal.wire_tool_order,
        "structured_response",
    )
    normal_after_option_change = producer.snapshot()
    assert normal_after_option_change is not normal
    assert normal_after_option_change.revision == normal.revision
    assert producer.snapshot() is normal_after_option_change

    store.apply_changes(agent_id="default", changes={"external_target": (False, False)})
    changed_selection = producer.snapshot()
    assert changed_selection is not normal_after_option_change
    assert changed_selection.selection_revision != normal.selection_revision
    assert changed_selection.revision == normal.revision
    assert producer.snapshot() is changed_selection

    disabled = producer.snapshot(tools_enabled=False)
    assert disabled is not changed_selection
    assert disabled.tool_names == ()
    assert disabled.selection_revision == changed_selection.selection_revision
    assert producer.snapshot(tools_enabled=False) is disabled
    resumed = producer.snapshot()
    assert resumed.tool_names == normal.tool_names
    assert resumed.selection_revision == changed_selection.selection_revision
    assert producer.snapshot() is resumed
