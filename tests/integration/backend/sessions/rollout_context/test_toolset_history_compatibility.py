"""新 ToolSet 无法投影旧工具事实时，真实 preparation 必须整体拒绝。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.services.infrastructure.rollout_context.runtime import prefix_compiler
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.mapping.itemized import prefix_frames
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    dispatch_saver,
    native_dispatch_workspace,
    native_http_server,
)
from tests.integration.backend.sessions.test_itemized_dispatch_contract import (
    _sealed_contract_rows,
    _select_contract_tools,
    dispatch_bridge,
    toolset_dispatch_case,
)

__all__ = [
    "dispatch_bridge", "dispatch_saver", "native_dispatch_workspace",
    "native_http_server", "toolset_dispatch_case",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
@pytest.mark.parametrize("unsupported_kind", ["tool_call", "tool_result"])
async def test_rebase_rejects_unprojectable_old_tool_history_without_rewriting_facts(
    toolset_dispatch_case, monkeypatch: pytest.MonkeyPatch, target_format: str, unsupported_kind: str,
) -> None:
    case = toolset_dispatch_case
    assert case.target_format == target_format
    messages = (
        AIMessage(content="读取旧工具", id="history-compat-call", tool_calls=[{
            "name": "inspect_file", "args": {"path": "README.md"}, "id": "history-compat-tool",
        }]),
        ToolMessage(content="必须保留的旧工具结果", id="history-compat-result",
                    name="inspect_file", tool_call_id="history-compat-tool"),
    )
    first = await case.dispatch("history-compat-original", messages=messages)
    items = case.saver._storage.storage.read_items(case.session, case.thread)
    old_tool_ids = {item.item_id for item in items if item.semantic_kind in {"tool_call", "tool_result"}}
    assert len(old_tool_ids) == 2
    unsupported_ids = {item.item_id for item in items if item.semantic_kind == unsupported_kind}
    assert len(unsupported_ids) == 1
    sealed_rows = _sealed_contract_rows(case)
    selected = _select_contract_tools(case, "inspect_folder")
    case.manager.observe("instruction:native-contract", "只能在兼容的 seal 后生效", revision="history-compat:next")
    pending = case.manager.prepare_pending()
    owner = ContextSourceOwnerKey(case.session, case.thread)
    controls = case.saver.load_context_source_control_states(owner)
    original_frames = prefix_compiler.project_canonical_prefix_frames
    original_projection = prefix_frames.project_canonical_items
    rejected_groups = []

    def unsupported_projection(group, **options):
        projected = original_projection(group, **options)
        rejected = unsupported_ids.intersection(item.item_id for item in group)
        if rejected:
            rejected_groups.extend(sorted(rejected))
            options["capability_losses"].append("controlled-provider:old-tool-history")
        return projected

    def compile_with_limited_projector(items, **options):
        # 只在新 epoch 编译边界模拟 Provider 的能力拒绝；旧 assembly 的真实校验照常运行。
        with monkeypatch.context() as projection_patch:
            projection_patch.setattr(prefix_frames, "project_canonical_items", unsupported_projection)
            return original_frames(items, **options)

    monkeypatch.setattr(prefix_compiler, "project_canonical_prefix_frames", compile_with_limited_projector)
    with pytest.raises(RuntimeError, match="provider-history-projection-unsupported: controlled-provider:old-tool-history"):
        await case.dispatch("history-compat-rebase")

    assert set(rejected_groups) == unsupported_ids
    assert len(case.state.requests) == len(case.dispatches) == 1
    assert case.saver.list_context_assemblies(case.session, case.thread) == (first,)
    assert _sealed_contract_rows(case) == sealed_rows
    assert case.saver._storage.storage.read_items(case.session, case.thread) == items
    assert case.saver.load_context_source_control_states(owner) == controls
    assert case.manager.prepare_pending() == pending
    assert case.selection.snapshot("dispatch-test") == selected
    assert first.seal_binding.selection_revision != selected.revision
    with case.saver._context_reader.open_snapshot(case.session, case.thread) as snapshot:
        assert snapshot.connection.execute(
            "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed' AND status='committed'"
        ).fetchone() == (1,)
