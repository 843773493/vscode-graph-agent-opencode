"""HTTP API 集成测试的共享确定性命中辅助函数。

多个 integration 文件都要经真实 HTTP 边界建会话并构造 LangGraph checkpoint
样板块；这两者与业务语义无关，收敛到唯一实现，避免逐文件复制漂移。
"""

from __future__ import annotations

import httpx
from langgraph.checkpoint.base import empty_checkpoint


async def create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


def checkpoint_fixture(
    checkpoint_id: str,
    messages: list[object],
    *,
    channel_version: int,
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        "messages": f"{channel_version:032d}.fixture"
    }
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


__all__ = ["checkpoint_fixture", "create_session"]
