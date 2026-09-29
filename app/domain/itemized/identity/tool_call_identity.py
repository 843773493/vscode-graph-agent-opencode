"""provider 工具调用身份在 canonical item 上的稳定还原规则。

stream sink 写入的 tool_call ID 会带上所属 model call 的 scope 前缀
(``{model_call_id}:tool-call:{provider_id}``)，用于区分同一次 Turn 内不同
model call 复用的 provider ID。provider wire 只认原始 ID，因此所有把
canonical item 投影到 provider 协议的位置都必须经过这里的还原。
"""

from __future__ import annotations

from collections.abc import Mapping

_TOOL_CALL_SCOPE_SEPARATOR = ":tool-call:"


def provider_tool_call_id(metadata: Mapping[str, object], tool_call_id: str) -> str:
    """把 stream 的 model-call scoped ID 还原为 provider call 身份。

    只有显式 ``model_call_id`` 前缀匹配时才还原；禁止按正文或 hash 猜测。
    """
    # TODO: 旧 stream item 迁移完成后，可删除 scoped ID 的历史还原分支。
    model_call_id = metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        prefix = f"{model_call_id}{_TOOL_CALL_SCOPE_SEPARATOR}"
        if tool_call_id.startswith(prefix):
            return tool_call_id.removeprefix(prefix)
    return tool_call_id
