"""send 工具仅收业务参数；受信 invocation 和 source thread 由后端提供。"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from app.abstractions.communication import SessionCommunicationProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.schemas.internal_v2.pending_request import DeliveryPolicy
from app.services.business.communication.admission import CommunicationPayload


class SendMessageInput(BaseModel):
    """通信只携带正文与投递语义，不接收团队、任务或 Goal 状态。"""

    model_config = ConfigDict(extra="forbid")

    target_session_id: str = Field(description="目标 Session ID 或规范 Session URI；目标固定为权威 main thread")
    content: str = Field(description="要发送的消息正文")
    target_workspace_id: str | None = None
    kind: Literal["question", "reply", "progress", "result"] = "result"
    reply_to_communication_id: str | None = None
    delivery_policy: DeliveryPolicy = "after_turn"


def create_send_message_to_session_tool(
    sender_session_id: str, *, communication_service: SessionCommunicationProtocol,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    @tool("send_message_to_session", args_schema=SendMessageInput)
    async def send_message_to_session(
        target_session_id: str,
        content: str,
        target_workspace_id: str | None = None,
        kind: Literal["question", "reply", "progress", "result"] = "result",
        reply_to_communication_id: str | None = None,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> dict[str, object]:
        """向目标 main thread 发送通信；成功表示目标已持久接收，execution 由唯一 worker 准入。

        返回 communication_id 可用于 reply 与 wait；每次新的工具 invocation 是新的逻辑发送。
        """
        binding = invocation_context.thread_binding
        if binding is None or binding.session_id != sender_session_id:
            raise RuntimeError("send 缺少精确 source thread binding")
        payload = CommunicationPayload(content, kind, reply_to_communication_id, delivery_policy)
        receipt = await communication_service.send(
            session_id=binding.session_id, thread_id=binding.thread_id,
            send_operation_id=invocation_context.require_operation_id("send"),
            target_session_id=target_session_id, target_workspace_id=target_workspace_id, payload=payload,
        )
        return {**receipt.model_dump(mode="json"), "kind": kind, "reply_required": kind == "question", "reply_to_communication_id": reply_to_communication_id, "delivery_policy": delivery_policy}
    return send_message_to_session
