"""C7-B2 focused：tool protocol boundary preflight。

唯一公共 validator（tool_protocol_boundary）接入 create_context_boundary
chokepoint；rewind（含 replay 使用的 arewind/arewind_to_turn）经由同一
入口验证冲突零副作用与闭合边界成功。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.request_plan import ContextContribution
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.business.message_display import project_message_for_display
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    CONFLICT_CODE,
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.mapping.itemized.provider_request import (
    project_runtime_notice_content,
)

SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd656"


def test_generated_session_notice_keeps_provider_prompt_and_safe_display_separate() -> None:
    prompt = internal_message_factory.build(
        kind="generated_session_result",
        control="内部禁止跨会话通信的控制正文。",
        sections=(
            PromptSection("control_context", {"generated_session_id": "branch-session"}),
            PromptSection("generated_session_result", "分支私有结果正文。"),
        ),
        metadata={
            "internal_display_provenance": {
                "source_session_id": "branch-session",
            }
        },
        display_content="生成分支已结束，主会话正在处理返回结果。",
    )
    message = HumanMessage(
        id="internal-report-back",
        content=prompt.content,
        response_metadata=prompt.metadata,
    )
    codec = LangChainMessageCodec()
    (item,) = codec.items_for_message(
        message,
        item_sequence=1,
        message_id="internal-report-back",
        turn_id=None,
        execution_id="execution-report-back",
        timestamp="2026-10-03T00:00:00+00:00",
    )

    assert item.semantic_kind == SemanticKind.RUNTIME_NOTICE
    assert item.turn_id is None
    assert item.turn_scope == "pending_next_turn"
    assert item.payload_kind == "structured_content"
    assert item.producer_ref["invocation_id"] == "execution-report-back"
    assert item.payload == {
        "prompt": prompt.content,
        "display_part": {
            "kind": "generated_session_result",
            "text": "生成分支已结束，主会话正在处理返回结果。",
            "provenance": {"source_session_id": "branch-session"},
        },
    }

    restored = codec.from_dict(codec.project_message((item,)))
    assert isinstance(restored, HumanMessage)
    assert restored.content == prompt.content
    assert restored.response_metadata["display_content"] == (
        "生成分支已结束，主会话正在处理返回结果。"
    )
    assert "turn_id" not in restored.response_metadata
    assert "job_id" not in restored.response_metadata
    display = project_message_for_display(
        restored.content,
        restored.response_metadata,
    )
    assert display.content == "生成分支已结束，主会话正在处理返回结果。"
    assert "source_session_id" not in display.metadata

    provider_content = project_runtime_notice_content(item.payload)
    assert provider_content == prompt.content
    assert "分支私有结果正文。" in provider_content
    assert "生成分支已结束，主会话正在处理返回结果。" not in provider_content


@pytest.mark.parametrize(
    ("message", "message_id", "semantic_kind"),
    [
        (
            AIMessage(id="internal-assistant-output", content="内部模型回复"),
            "internal-assistant-output",
            "assistant_output",
        ),
        (
            AIMessage(
                id="internal-tool-call",
                content="",
                tool_calls=[
                    {"id": "call-internal", "name": "read_file", "args": {}}
                ],
            ),
            "internal-tool-call",
            "tool_call",
        ),
        (
            ToolMessage(
                id="internal-tool-result",
                content="工具结果",
                tool_call_id="call-internal",
                name="read_file",
            ),
            "internal-tool-result",
            "tool_result",
        ),
    ],
)
def test_standalone_execution_outputs_keep_semantic_kind_without_turn(
    message: object,
    message_id: str,
    semantic_kind: str,
) -> None:
    (item,) = LangChainMessageCodec().items_for_message(
        message,
        item_sequence=1,
        message_id=message_id,
        turn_id=None,
        execution_id="execution-standalone",
        timestamp="2026-10-03T00:00:00+00:00",
    )

    assert item.semantic_kind == semantic_kind
    assert item.turn_id is None
    assert item.turn_scope is None
    assert item.producer_ref["invocation_id"] == "execution-standalone"


def test_turnless_message_requires_explicit_execution_id() -> None:
    with pytest.raises(ValueError, match="Turn-less canonical message"):
        LangChainMessageCodec().items_for_message(
            AIMessage(id="unbound-assistant", content="未绑定消息"),
            item_sequence=1,
            message_id="unbound-assistant",
            turn_id=None,
            timestamp="2026-10-03T00:00:00+00:00",
        )


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        channel: str(index + 1)
        for index, channel in enumerate(checkpoint["channel_values"])
    }
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def _put_checkpoint(
    saver: RolloutCheckpointSaver,
    config: object,
    checkpoint_id: str,
    messages: list[object],
    step: int,
) -> object:
    return saver.put(
        config,
        _checkpoint(checkpoint_id, messages),
        {"source": "job", "step": step},
        {"messages": str(step)},
    )


def _seed_closed_tool_group(saver: RolloutCheckpointSaver) -> None:
    """checkpoint-1 只有 user；checkpoint-2 追加 call 与 terminal result。"""
    user = HumanMessage(content="读取配置", id="user-1")
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(SESSION_ID),
        "checkpoint-1",
        [user],
        1,
    )
    call = AIMessage(
        content="",
        id="assistant-call-1",
        tool_calls=[
            {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
        ],
    )
    result = ToolMessage(
        content='{"ok":true}',
        id="tool-result-1",
        tool_call_id="call-1",
        name="read_file",
    )
    _put_checkpoint(
        saver,
        first_config,
        "checkpoint-2",
        [user, call, result],
        2,
    )


def test_rewind_conflict_keeps_old_view_zero_side_effects(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    _seed_closed_tool_group(saver)
    manifest_before = storage.initialize(SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        storage.rewind_to_checkpoint(
            thread_id=SESSION_ID,
            checkpoint_id="checkpoint-2",
            source_anchor="assistant-call-1",
            anchor_mode="inclusive",
        )
    error = exc_info.value
    assert error.code == CONFLICT_CODE
    # 安全 anchor 候选：call 之前的完整前缀，以及包含全部 call+result 的 view
    cutoff_indices = [anchor.cutoff_index for anchor in error.safe_anchors]
    assert 1 in cutoff_indices
    assert 3 in cutoff_indices
    for anchor in error.safe_anchors:
        assert (
            anchor.cutoff_message_sequence is None
            or type(anchor.cutoff_message_sequence) is int
        )

    manifest_after = storage.initialize(SESSION_ID)
    assert manifest_after == manifest_before


def test_rewind_conflict_for_pending_call_has_no_safe_anchor_above(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    user = HumanMessage(content="读取配置", id="user-1")
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(SESSION_ID),
        "checkpoint-1",
        [user],
        1,
    )
    call = AIMessage(
        content="",
        id="assistant-call-1",
        tool_calls=[
            {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
        ],
    )
    _put_checkpoint(
        saver,
        first_config,
        "checkpoint-2",
        [user, call],
        2,
    )
    manifest_before = storage.initialize(SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        storage.rewind_to_checkpoint(
            thread_id=SESSION_ID,
            checkpoint_id="checkpoint-2",
            source_anchor="assistant-call-1",
            anchor_mode="inclusive",
        )
    # pending call 没有任何 terminal result：包含 call 的前缀全部未闭合，
    # 因此只有 call 之前的候选，没有更大的安全切点。
    assert [anchor.cutoff_index for anchor in exc_info.value.safe_anchors] == [1]
    assert storage.initialize(SESSION_ID) == manifest_before


def test_rewind_at_closed_boundaries_succeeds(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    _seed_closed_tool_group(saver)
    manifest_before = storage.initialize(SESSION_ID)

    # 包含完整 call+result 的闭合边界：rewind 成功并推进 view revision。
    manifest = storage.rewind_to_checkpoint(
        thread_id=SESSION_ID,
        checkpoint_id="checkpoint-2",
        source_anchor="tool-result-1",
        anchor_mode="inclusive",
    )
    assert manifest.history_view_revision == manifest_before.history_view_revision + 1

    # 位于 tool-call group 之前的 user 锚点同样是闭合边界。
    manifest_user = storage.rewind_to_checkpoint(
        thread_id=SESSION_ID,
        checkpoint_id="checkpoint-1",
        source_anchor="user-1",
        anchor_mode="inclusive",
    )
    assert (
        manifest_user.history_view_revision == manifest.history_view_revision + 1
    )


def test_rewind_keeps_sealed_assembly_bytes_unchanged(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """rewind 只推进 view/pending epoch：已 seal assembly 的持久字节保持不变。

    OpenSpec add-context-injection-lifecycle 6.3：旧 sealed bytes 不变；
    rewind 提交 view/transition，不改写已封存 assembly 与 detail ref。
    """
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    # user 消息携带 turn_id，使后续 prepare/seal 有真实 Turn root 可绑定。
    user = HumanMessage(
        content="读取配置",
        id="user-1",
        response_metadata={"turn_id": "rewind-turn"},
    )
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(SESSION_ID),
        "checkpoint-1",
        [user],
        1,
    )
    call = AIMessage(
        content="",
        id="assistant-call-1",
        tool_calls=[
            {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
        ],
    )
    result = ToolMessage(
        content='{"ok":true}',
        id="tool-result-1",
        tool_call_id="call-1",
        name="read_file",
    )
    _put_checkpoint(
        saver,
        first_config,
        "checkpoint-2",
        [user, call, result],
        2,
    )
    manifest_before = storage.initialize(SESSION_ID)

    body = {"policy": "rewind-guard-prompt"}
    prepared = saver.prepare_context_for_provider(
        SESSION_ID,
        turn_id="rewind-turn",
        prompt_contributions=(
            ContextContribution(
                contribution_id="rewind-guard-prompt",
                source_kind="workspace_policy",
                source_revision="rewind-guard-prompt",
                body=body,
                content_hash=contribution_content_hash("prompt", body),
            ),
        ),
        provider_version="rewind-guard-provider",
        target_format="chat_completions",
        plan_creation_idempotency_key="rewind-guard:create",
        seal_idempotency_key="rewind-guard:seal",
    )
    assert prepared["losses"] == ()
    sealed_before = saver.list_context_assemblies(SESSION_ID)
    assert len(sealed_before) == 1
    assembly_before = sealed_before[0]
    detail_ref_before = storage.get_context_assembly_detail_ref(
        SESSION_ID, assembly_id=assembly_before.assembly_id
    )

    # rewind 到闭合边界（user 锚点）并推进 view revision。
    manifest = storage.rewind_to_checkpoint(
        thread_id=SESSION_ID,
        checkpoint_id="checkpoint-1",
        source_anchor="user-1",
        anchor_mode="inclusive",
    )
    assert manifest.history_view_revision == manifest_before.history_view_revision + 1

    # 已 seal assembly 的持久字节/详情引用逐项不变。
    assert saver.list_context_assemblies(SESSION_ID) == sealed_before
    assert (
        saver.get_context_assembly(SESSION_ID, assembly_id=assembly_before.assembly_id)
        == assembly_before
    )
    assert (
        storage.get_context_assembly_detail_ref(
            SESSION_ID, assembly_id=assembly_before.assembly_id
        )
        == detail_ref_before
    )
