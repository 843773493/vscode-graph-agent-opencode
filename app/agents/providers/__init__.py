"""Provider wrappers 的轻量包入口。

纯 provider content normalization、mapping 和 infrastructure bridge 不应因
导入包名而提前加载两个完整模型 wrapper；wrapper 只有在显式请求时才懒加载。
"""

from __future__ import annotations

from typing import Any

__all__ = ["BoxteamLiteLLMChatModel", "BoxteamOpenAIResponsesModel"]


def __getattr__(name: str) -> Any:
    if name == "BoxteamLiteLLMChatModel":
        from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel

        return BoxteamLiteLLMChatModel
    if name == "BoxteamOpenAIResponsesModel":
        from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel

        return BoxteamOpenAIResponsesModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
