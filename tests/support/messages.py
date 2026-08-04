from __future__ import annotations

import pytest


def last_assistant_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = message.get("content", "")
            if content:
                return content
    pytest.fail("未找到非空 assistant 消息")

