"""条件 instruction 来源随真实 ToolSet 可见性移除、恢复与旧封存重试。"""

from __future__ import annotations

import pytest
from langchain_core.messages import messages_to_dict

from app.domain.itemized.epoch.prefix_epoch import EpochReason
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from tests.integration.backend.sessions.rollout_context.test_agent_production_context import (
    factory_context,
    registry_db,
)
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)

__all__ = ["factory_context", "registry_db"]


@pytest.fixture(autouse=True)
def isolated_policy_workspace(request: pytest.FixtureRequest) -> None:
    """每个参数先重建正式工作区，隔离文件级共享的 ToolSelectionStore。"""
    prepare_rollout_workspace(request)


@pytest.mark.parametrize(
    ("source_id", "policy_key", "tool_names"),
    [
        pytest.param(
            "instruction:team-coordination-policy", "team_coordination",
            ("create_team",), id="R03",
        ),
        pytest.param(
            "instruction:todo-list", "todo_list", ("write_todos",), id="R04",
        ),
        pytest.param(
            "instruction:filesystem-rules", "filesystem_rules",
            ("ls", "read_file", "write_file", "edit_file", "glob", "grep"), id="R06",
        ),
        pytest.param(
            "instruction:compact-tool-usage", "compact_conversation",
            ("compact_conversation",), id="R07",
        ),
    ],
)
def test_conditional_source_follows_visible_tool_policy_and_preserves_sealed_retry(
    factory_context,
    source_id: str,
    policy_key: str,
    tool_names: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_team 是受信注册桩；这里只证明 policy/source，不执行团队创建。"""
    saver, session, thread, _connection, selection, manager, captured, registered, dispatch = factory_context
    owner = ContextSourceOwnerKey(session, thread)
    bridge = captured["middleware"][-1]
    registered_names = set(registered)
    assert set(tool_names) <= registered_names
    descriptors = {descriptor.source_id: descriptor for descriptor in manager.descriptors()}
    assert descriptors[source_id].policy_key == policy_key
    records = []
    initial_source_ids = None
    initial_policy_keys = None

    reads = {"sources": 0, "rebuild_sources": 0, "tool_sets": 0}
    prepare_sources = manager.prepare_sources
    prepare_rebuild_sources = manager.prepare_rebuild_sources
    tool_snapshot = bridge._tool_set_producer.snapshot

    def count_sources(*args: object, **kwargs: object):
        reads["sources"] += 1
        return prepare_sources(*args, **kwargs)

    def count_rebuild_sources(*args: object, **kwargs: object):
        reads["rebuild_sources"] += 1
        return prepare_rebuild_sources(*args, **kwargs)

    def count_tool_sets(*args: object, **kwargs: object):
        reads["tool_sets"] += 1
        return tool_snapshot(*args, **kwargs)

    for step, visible in enumerate((True, False, True)):
        selection.apply_changes(
            agent_id="default",
            changes={name: (True, visible) for name in tool_names},
        )
        chosen = selection.snapshot("default")
        assert all(chosen.execution_overrides[name] is True for name in tool_names)
        assert all(chosen.model_visibility_overrides[name] is visible for name in tool_names)
        assert set(registered) == registered_names

        if records:
            # 新 desired 选择已经发布；旧请求只读取旧 seal，不能消费当前来源状态。
            pending_before_retry = manager.prepare_pending()
            control_before_retry = saver.load_context_source_control_states(owner)
            reads.update({"sources": 0, "rebuild_sources": 0, "tool_sets": 0})

            with monkeypatch.context() as retry_patch:
                retry_patch.setattr(manager, "prepare_sources", count_sources)
                retry_patch.setattr(manager, "prepare_rebuild_sources", count_rebuild_sources)
                retry_patch.setattr(bridge._tool_set_producer, "snapshot", count_tool_sets)
                for old_task, old_snapshot, old_bytes, old_messages, old_tools in records:
                    retry = dispatch(old_task)
                    assert canonical_json_bytes(messages_to_dict(retry.messages)) == old_messages
                    assert canonical_json_bytes(retry.tools) == old_tools
                    restored = saver.get_context_assembly(
                        session, thread, assembly_id=old_snapshot.assembly_id,
                    )
                    assert canonical_json_bytes(restored.to_dict()) == old_bytes
            assert reads == {"sources": 0, "rebuild_sources": 0, "tool_sets": 0}
            assert manager.prepare_pending() == pending_before_retry
            assert saver.load_context_source_control_states(owner) == control_before_retry
            assert selection.snapshot("default") == chosen
            assert len(saver.list_context_assemblies(session, thread)) == len(records)

        task = f"conditional-policy-{policy_key}-{step}"
        outbound = dispatch(task)
        assemblies = saver.list_context_assemblies(session, thread)
        assert len(assemblies) == step + 1
        snapshot = assemblies[-1]
        binding = snapshot.seal_binding
        assert binding is not None and binding.root is not None
        assert binding.root.epoch == binding.prefix.state.epoch
        assert binding.root.tool_compatibility_key == binding.prefix.state.tool_compatibility_key
        root_source_ids = {source.source_identity for source in binding.root.sources}
        tool_refs = [
            ref for ref in snapshot.tool_set_refs
            if ref.ref_id == binding.tool_set_snapshot_id
        ]
        assert len(tool_refs) == 1
        enabled_policy_keys = set(tool_refs[0].tool_policy["enabled_policy_keys"])
        wire_names = {schema["function"]["name"] for schema in outbound.tools}
        expected_tools = registered_names if visible else registered_names - set(tool_names)
        assert wire_names == expected_tools
        assert tool_refs[0].tools == tuple(outbound.tools)
        wire = snapshot.request_hash_preimage
        assert isinstance(wire, dict)
        assert canonical_json_bytes(wire["tools"]) == canonical_json_bytes(outbound.tools)
        assert (source_id in root_source_ids) is visible
        assert (policy_key in enabled_policy_keys) is visible
        assert not any(
            frame.item_ref.source_identity == source_id
            for frame in binding.prefix.state.frames if frame.role == "user"
        )

        if step == 0:
            initial_source_ids = root_source_ids
            initial_policy_keys = enabled_policy_keys
            assert binding.prefix.state.epoch.reason == EpochReason.INITIAL
        else:
            previous = records[-1][1].seal_binding
            assert previous is not None and previous.root is not None
            assert binding.prefix.state.epoch.ordinal == previous.prefix.state.epoch.ordinal + 1
            assert binding.prefix.state.epoch.reason == EpochReason.TOOLSET_CHANGED
            assert binding.previous_assembly_id == records[-1][1].assembly_id
            assert binding.root != previous.root and binding.root.content != previous.root.content
            assert initial_source_ids is not None and initial_policy_keys is not None
            assert root_source_ids == (initial_source_ids if visible else initial_source_ids - {source_id})
            assert enabled_policy_keys == (initial_policy_keys if visible else initial_policy_keys - {policy_key})

        # 条件策略未启用时可保留 desired；只按 delta 与真实 sealed policy 判断 pending。
        pending = manager.prepare_pending()
        if pending is not None:
            pending_sources = {source.descriptor.source_id: source for source in pending.sources}
            for delta in pending.deltas:
                required_policy = pending_sources[delta.source_id].descriptor.policy_key
                assert required_policy is not None and required_policy not in enabled_policy_keys
        for _old_task, old_snapshot, old_bytes, _old_messages, _old_tools in records:
            restored = saver.get_context_assembly(
                session, thread, assembly_id=old_snapshot.assembly_id,
            )
            assert canonical_json_bytes(restored.to_dict()) == old_bytes
        records.append((
            task, snapshot, canonical_json_bytes(snapshot.to_dict()),
            canonical_json_bytes(messages_to_dict(outbound.messages)),
            canonical_json_bytes(outbound.tools),
        ))

    assert len(records) == 3
