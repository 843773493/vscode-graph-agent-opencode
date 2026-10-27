"""运行态来源发布的纯值编码；正文和 observed/applied 仍归唯一 ContextStore。"""

from app.abstractions.internal_message import (
    PreparedInternalMessage,
    RuntimeContextMode,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
)


def runtime_state_revision(content_revision: str, sequence: int) -> str:
    """来源版本由原owner控制次序分配；相同正文回切也不能冒充旧的更新。"""
    return "sha256:" + sha256_jcs({"kind": "runtime_state", "content_revision": content_revision,
                                  "control_sequence": sequence}).rsplit(":", 1)[1]


def runtime_context_decision(
    session_id: str, thread_id: str, message: PreparedInternalMessage, *, source_id: str, item_id: str,
    context_mode: RuntimeContextMode,
) -> ApplySourceLifecycleDecision:
    state = context_mode == "state"
    kind = message.metadata["structured_prompt_kind"]
    if not isinstance(kind, str):
        raise TypeError("runtime source kind必须是字符串")
    return ApplySourceLifecycleDecision(
        owner=MutationIntentOwner(session_id=session_id, thread_id=thread_id),
        source_id=source_id, source_kind="runtime_state" if state else kind, name=source_id if state else kind,
        decision_kind="base" if state else "observe_pending", revision=sha256_jcs({"content": message.content, "metadata": message.metadata}),
        tracking_status="tracked" if state else None, pending_only=True, content=message.content,
        item_id=item_id, turn_scope="ambient",
        extensions={"boxteam.checkpoint.annotations": {
            "extension_version": "1", "protection": "public", "value": dict(message.metadata),
        }},
    )
