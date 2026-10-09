"""provider 工具调用身份在 canonical item 上的稳定还原规则。

stream sink 写入的 tool_call ID 会带上所属 model call 的 scope 前缀
(``{model_call_id}:tool-call:{provider_id}``)，用于区分同一次 Turn 内不同
model call 复用的 provider ID。provider wire 只认原始 ID，因此所有把
canonical item 投影到 provider 协议的位置都必须经过这里的还原。
"""

from __future__ import annotations

import hashlib
import json

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity

_TOOL_CALL_SCOPE_SEPARATOR = ":tool-call:"


def software_tool_operation_id(operation: str, session_id: str, thread_id: str, model_call_id: str, call_id: str) -> str:
    """软件操作 identity 只取自持久的 emitting call 与精确 owner。"""
    preimage = json.dumps([operation, session_id, thread_id, model_call_id, call_id], ensure_ascii=False, separators=(",", ":"))
    return "cop_" + hashlib.sha256(preimage.encode()).hexdigest()


def provider_tool_call_id(identity: ItemProjectionIdentity | None, tool_call_id: str) -> str:
    """把 stream 的 model-call scoped ID 还原为 provider call 身份。

    只有显式 ``model_call_id`` 前缀匹配时才还原；禁止按正文或 hash 猜测。
    """
    model_call_id = identity.model_call_id if identity is not None else None
    if isinstance(model_call_id, str) and model_call_id:
        prefix = f"{model_call_id}{_TOOL_CALL_SCOPE_SEPARATOR}"
        if tool_call_id.startswith(prefix):
            return tool_call_id.removeprefix(prefix)
    return tool_call_id


def matches_tool_call_id(
    identity: ItemProjectionIdentity | None,
    tool_call_id: str,
    selected_id: str,
) -> bool:
    """仅在实际模型调用 scope 内匹配 raw 与 scoped ID 两种载体。"""
    return provider_tool_call_id(identity, tool_call_id) == provider_tool_call_id(
        identity, selected_id
    )
