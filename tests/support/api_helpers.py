"""HTTP API 集成测试的共享确定性命中辅助函数。

多个 integration 文件都要经真实 HTTP 边界建会话并构造 LangGraph checkpoint
样板块；这两者与业务语义无关，收敛到唯一实现，避免逐文件复制漂移。
"""

from __future__ import annotations

import httpx
from langgraph.checkpoint.base import empty_checkpoint


async def create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    if response.status_code != 200:
        raise AssertionError(
            f"创建会话失败: status={response.status_code}, body={response.text}"
        )
    session_id = response.json()["data"]["session_id"]
    if not isinstance(session_id, str) or not session_id:
        raise AssertionError(f"创建会话成功响应缺少有效 session_id: {response.text}")
    return session_id


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
