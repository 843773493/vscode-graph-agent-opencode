"""为 custom-tool 工作区中的 Web 边界会话生成 canonical rollout。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


def boundary_item(
    *,
    item_id: str,
    semantic_kind: SemanticKind,
    payload_kind: PayloadKind,
    status: CanonicalItemStatus,
    payload: object,
    turn_id: str,
    wire_role: str = "assistant",
    projection_identity: ItemProjectionIdentity | None = None,
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=999,
        item_id=item_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status=status,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": f"model-call-{turn_id}",
            "invocation_id": f"execution-{turn_id}",
        },
        payload=payload,
        projection_identity=projection_identity
        or ItemProjectionIdentity(
            projection_message_id=item_id.removeprefix("item-"),
            model_call_id=f"model-call-{turn_id}",
        ),
        turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER,
        message_group_id=f"message-{turn_id}",
        wire_role=wire_role,
    )


def tool_call_payload(
    call_id: str,
    name: str,
    args: dict[str, object],
) -> dict[str, object]:
    return {
        "tool_call_id": call_id,
        "tool_invocation_id": f"invocation-{call_id}",
        "tool_attempt_id": f"attempt-{call_id}",
        "name": name,
        "args": args,
    }


def tool_result_payload(
    call_id: str,
    name: str,
    content: str,
) -> dict[str, object]:
    return {
        "tool_call_id": call_id,
        "tool_invocation_id": f"invocation-{call_id}",
        "tool_attempt_id": f"attempt-{call_id}",
        "result_id": f"result-{call_id}",
        "name": name,
        "content": content,
        "tool_outcome": "success",
        "execution_confirmed": True,
    }


def seed_boundary_turn(
    sessions_dir: Path,
    *,
    session_id: str,
    turn_id: str,
    user_content: str,
    items: tuple[CanonicalItemRecord, ...],
    outcome: str,
    turn_status: str,
    final_item_id: str | None = None,
) -> None:
    saver = RolloutCheckpointSaver(sessions_dir)
    owner = saver.resolve_main_thread_owner(session_id)
    accepted = saver.accept_turn(
        session_id,
        thread_id=owner.thread_id,
        accepted_ingress_id=f"ingress-{turn_id}",
        acceptance_idempotency_key=f"acceptance-{turn_id}",
        job_id=f"job-{turn_id}",
        admission_idempotency_key=f"execution-admission-{turn_id}",
        payload=user_content,
        payload_kind=PayloadKind.TEXT,
        turn_id=turn_id,
        root_item_id=f"item-boundary-user-{turn_id}",
        initial_execution_id=f"execution-{turn_id}",
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = f"checkpoint-{turn_id}"
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                content=user_content,
                id=f"boundary-user-{turn_id}",
                response_metadata={"message_metadata": {"turn_id": turn_id}},
            )
        ]
    }
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    admission = saver.execution_admission(
        session_id,
        thread_id=owner.thread_id,
        execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.put(
            build_checkpoint_config(owner.thread_id),
            checkpoint,
            {"source": f"boundary-cases-{turn_id}-fixture", "step": 1},
            {"messages": "1"},
        )
    saver.converge_execution(
        session_id,
        thread_id=owner.thread_id,
        turn_id=turn_id,
        execution_id=str(accepted["initial_execution_id"]),
        outcome=outcome,
        turn_status=turn_status,
        items=items,
        final_item_id=final_item_id,
    )


def _seed_case_0001(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019bf985d867717182fe710be0f6adae",
        turn_id="boundary-turn-0001",
        user_content="验证同一 AI 消息中的 text 与 tool_call 展示边界。",
        items=(
            boundary_item(
                item_id="item-boundary-reasoning-0001",
                semantic_kind=SemanticKind.REASONING,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="先确认文件，再把普通文本和工具调用保持在同一模型消息中。",
                turn_id="boundary-turn-0001",
            ),
            boundary_item(
                item_id="item-boundary-text-0001",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="我先读取 README，再根据结果回答。",
                turn_id="boundary-turn-0001",
            ),
            boundary_item(
                item_id="item-boundary-call-0001",
                semantic_kind=SemanticKind.TOOL_CALL,
                payload_kind=PayloadKind.TOOL_CALL,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_call_payload(
                    "call-boundary-text-tool", "read_file", {"path": "README.md"}
                ),
                turn_id="boundary-turn-0001",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0001",
                    block_id="model-call-"
                    + "boundary-turn-0001"
                    + ":tool-call:"
                    + "call-boundary-text-tool",
                    block_index=0,
                ),
            ),
            boundary_item(
                item_id="item-boundary-result-0001",
                semantic_kind=SemanticKind.TOOL_RESULT,
                payload_kind=PayloadKind.TOOL_RESULT,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_result_payload(
                    "call-boundary-text-tool",
                    "read_file",
                    '{"path": "README.md", "result": "boundary fixture read success"}',
                ),
                turn_id="boundary-turn-0001",
                wire_role="tool",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0001"
                ),
            ),
            boundary_item(
                item_id="item-boundary-final-0001",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="README 已读取；普通文本、tool_call 和工具结果按顺序展示。",
                turn_id="boundary-turn-0001",
            ),
        ),
        outcome="completed",
        turn_status="completed",
        final_item_id="item-boundary-final-0001",
    )


def _seed_case_0002(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019bed0e9f8f7b27a213abe862c0b31a",
        turn_id="boundary-turn-0002",
        user_content="模拟用户在工具参数仍未完成时中断 Turn。",
        items=(
            boundary_item(
                item_id="item-boundary-text-0002",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.PARTIAL,
                payload="我准备读取配置文件。",
                turn_id="boundary-turn-0002",
                projection_identity=ItemProjectionIdentity(
                    phase="assistant_text",
                    completion_reason="user_interrupt",
                    model_call_id="model-call-" + "boundary-turn-0002",
                ),
            ),
            boundary_item(
                item_id="item-boundary-call-0002",
                semantic_kind=SemanticKind.TOOL_CALL,
                payload_kind=PayloadKind.TOOL_CALL,
                status=CanonicalItemStatus.PARTIAL,
                payload=tool_call_payload(
                    "call-boundary-partial-tool", "read_file", {"path": "config/"}
                ),
                turn_id="boundary-turn-0002",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0002",
                    block_id="model-call-"
                    + "boundary-turn-0002"
                    + ":tool-call:"
                    + "call-boundary-partial-tool",
                    block_index=0,
                ),
            ),
        ),
        outcome="cancelled",
        turn_status="cancelled",
    )


def _seed_case_0003(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019bc1148cec71b78b1aaa3b19b60363",
        turn_id="boundary-turn-0003",
        user_content="模拟工具已经启动但后端在 tool.completed 前退出。",
        items=(
            boundary_item(
                item_id="item-boundary-text-0003",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="我已经启动大输出工具，正在等待结果。",
                turn_id="boundary-turn-0003",
            ),
            boundary_item(
                item_id="item-boundary-call-0003",
                semantic_kind=SemanticKind.TOOL_CALL,
                payload_kind=PayloadKind.TOOL_CALL,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_call_payload(
                    "call-boundary-unknown-tool",
                    "large_test_output",
                    {"lines": 768, "marker": "unknown-result"},
                ),
                turn_id="boundary-turn-0003",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0003",
                    block_id="model-call-"
                    + "boundary-turn-0003"
                    + ":tool-call:"
                    + "call-boundary-unknown-tool",
                    block_index=0,
                ),
            ),
        ),
        outcome="failed",
        turn_status="failed",
    )


def _seed_case_0004(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019c5123cafb789e8fdf8704f6464f74",
        turn_id="boundary-turn-0004",
        user_content="模拟文本 block 已产生内容后用户中断。",
        items=(
            boundary_item(
                item_id="item-boundary-partial-text-0004",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.PARTIAL,
                payload="我已经开始分析这个问题，但回答在这里被用户中断……",
                turn_id="boundary-turn-0004",
                projection_identity=ItemProjectionIdentity(
                    phase="assistant_text",
                    completion_reason="user_interrupt",
                    model_call_id="model-call-" + "boundary-turn-0004",
                ),
            ),
        ),
        outcome="cancelled",
        turn_status="cancelled",
    )


def _seed_case_0005(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019bdcfcb97f7dab8f30a959418a1658",
        turn_id="boundary-turn-0005",
        user_content="验证普通文本中的 function 标记不会被当成工作区文件引用。",
        items=(
            boundary_item(
                item_id="item-boundary-final-0005",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload=(
                    '<function=read_file>{"path":"README.md"}</function>'
                    " 这是普通消息文本，不是实际工具调用。"
                ),
                turn_id="boundary-turn-0005",
            ),
        ),
        outcome="completed",
        turn_status="completed",
        final_item_id="item-boundary-final-0005",
    )


def _seed_case_0006(sessions_dir: Path) -> None:
    seed_boundary_turn(
        sessions_dir,
        session_id="ses_019b7a50d46c7e7f86c9148c7069cf97",
        turn_id="boundary-turn-0006",
        user_content="验证一条 AI 消息中包含普通文本和多个并行 tool_call。",
        items=(
            boundary_item(
                item_id="item-boundary-text-0006",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="我将同时检查两个文件。",
                turn_id="boundary-turn-0006",
            ),
            boundary_item(
                item_id="item-boundary-call-0006-read",
                semantic_kind=SemanticKind.TOOL_CALL,
                payload_kind=PayloadKind.TOOL_CALL,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_call_payload(
                    "call-boundary-parallel-read", "read_file", {"path": "README.md"}
                ),
                turn_id="boundary-turn-0006",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0006",
                    block_id="model-call-"
                    + "boundary-turn-0006"
                    + ":tool-call:"
                    + "call-boundary-parallel-read",
                    block_index=0,
                ),
            ),
            boundary_item(
                item_id="item-boundary-call-0006-search",
                semantic_kind=SemanticKind.TOOL_CALL,
                payload_kind=PayloadKind.TOOL_CALL,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_call_payload(
                    "call-boundary-parallel-search",
                    "search_files",
                    {"query": "MessageBlock"},
                ),
                turn_id="boundary-turn-0006",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0006",
                    block_id="model-call-"
                    + "boundary-turn-0006"
                    + ":tool-call:"
                    + "call-boundary-parallel-search",
                    block_index=1,
                ),
            ),
            boundary_item(
                item_id="item-boundary-result-0006-read",
                semantic_kind=SemanticKind.TOOL_RESULT,
                payload_kind=PayloadKind.TOOL_RESULT,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_result_payload(
                    "call-boundary-parallel-read",
                    "read_file",
                    '{"result": "README ok"}',
                ),
                turn_id="boundary-turn-0006",
                wire_role="tool",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0006"
                ),
            ),
            boundary_item(
                item_id="item-boundary-result-0006-search",
                semantic_kind=SemanticKind.TOOL_RESULT,
                payload_kind=PayloadKind.TOOL_RESULT,
                status=CanonicalItemStatus.COMPLETED,
                payload=tool_result_payload(
                    "call-boundary-parallel-search",
                    "search_files",
                    '{"matches": 3}',
                ),
                turn_id="boundary-turn-0006",
                wire_role="tool",
                projection_identity=ItemProjectionIdentity(
                    model_call_id="model-call-" + "boundary-turn-0006"
                ),
            ),
            boundary_item(
                item_id="item-boundary-final-0006",
                semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                payload_kind=PayloadKind.TEXT,
                status=CanonicalItemStatus.COMPLETED,
                payload="两个工具都已返回，结果已合并到最终答复。",
                turn_id="boundary-turn-0006",
            ),
        ),
        outcome="completed",
        turn_status="completed",
        final_item_id="item-boundary-final-0006",
    )


SESSION_SEEDS = {
    "ses_019bf985d867717182fe710be0f6adae": _seed_case_0001,
    "ses_019bed0e9f8f7b27a213abe862c0b31a": _seed_case_0002,
    "ses_019bc1148cec71b78b1aaa3b19b60363": _seed_case_0003,
    "ses_019c5123cafb789e8fdf8704f6464f74": _seed_case_0004,
    "ses_019bdcfcb97f7dab8f30a959418a1658": _seed_case_0005,
    "ses_019b7a50d46c7e7f86c9148c7069cf97": _seed_case_0006,
}


def seed_boundary_cases(
    workspace_root: Path,
    session_ids: Sequence[str] | None = None,
) -> None:
    """按模板 v1 文案与终态，重写指定边界会话（默认全部 6 个）。"""
    targets = tuple(session_ids) if session_ids is not None else tuple(SESSION_SEEDS)
    unknown = [session_id for session_id in targets if session_id not in SESSION_SEEDS]
    if unknown:
        raise ValueError(f"未知边界会话: {unknown}")
    sessions_dir = workspace_root / ".boxteam" / "sessions"
    for session_id in targets:
        SESSION_SEEDS[session_id](sessions_dir)
