"""跨 Session send 的 typed admission 合同。

幂等分两层，均由 ledger owner 持久化后由本层裁决：
- operation 层：同 (source, send_operation_id) create-or-get outbox；
  同 operation 不同 preimage 报 communication-preimage-conflict。
- communication 层：同 (source, communication_id) dedupe；同 key 不同
  payload/target 报 communication-id-conflict。

本层是纯决策，不触 SQLite、不分配随机 ID、不建第二 writer；持久化接线
由后续 CommunicationOutbox/Inbox ledger owner 完成。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.errors import CommunicationContractError

CommunicationKind = Literal["question", "reply", "progress", "result"]


@dataclass(frozen=True, slots=True)
class CommunicationPayload:
    """一次跨 Session send 的业务正文与语义；字段闭合校验。"""

    content: str
    kind: CommunicationKind = "result"
    reply_to_communication_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise CommunicationContractError(
                "communication-payload-invalid",
                "通信 content 必须是非空白字符串",
            )
        if self.kind == "reply":
            if not self.reply_to_communication_id:
                raise CommunicationContractError(
                    "communication-payload-invalid",
                    "kind=reply 必须提供 reply_to_communication_id",
                )
        elif self.reply_to_communication_id is not None:
            raise CommunicationContractError(
                "communication-payload-invalid",
                "只有 kind=reply 可以提供 reply_to_communication_id",
            )


@dataclass(frozen=True, slots=True)
class CommunicationSendRequest:
    """source 侧一次逻辑 send 的完整请求；preimage 由其派生。"""

    source: GlobalThreadAddress
    target_binding: ResolvedMainTargetBinding
    send_operation_id: str
    payload: CommunicationPayload

    def __post_init__(self) -> None:
        if not isinstance(self.send_operation_id, str) or not self.send_operation_id.strip():
            raise CommunicationContractError(
                "communication-operation-id-invalid",
                "send_operation_id 必须是非空字符串（软件持久化的幂等键，模型不可见）",
            )


@dataclass(frozen=True, slots=True)
class ExistingOutboxView:
    """已有 outbox 记录的只读视图；由 ledger owner 查询后传入。"""

    source: GlobalThreadAddress
    send_operation_id: str
    communication_id: str
    target: GlobalThreadAddress
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.payload_hash.startswith("sha256:"):
            raise CommunicationContractError(
                "communication-preimage-conflict",
                f"outbox payload_hash 必须是 sha256 形式: {self.payload_hash!r}",
            )


@dataclass(frozen=True, slots=True)
class ReceivedCommunicationView:
    """本 source 之前收到的 communication 的只读视图，用于 reply 因果方向证明。

    TODO: 当前生产 send_message_to_session 尚未接线收件 ledger 查询；
    后续 E03 ledger 切片接入时必须提供该视图，否则 kind=reply 无法通过 admission。
    """

    communication_id: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress


@dataclass(frozen=True, slots=True)
class CommunicationAdmissionDecision:
    """admission 决策结果；is_duplicate=True 表示按幂等返回既有 outbox。"""

    communication_id: str
    payload_hash: str
    is_duplicate: bool


def resolve_request_preimage(request: CommunicationSendRequest) -> str:
    """冻结 (target, payload) 的稳定 preimage hash；同 preimage 才允许幂等复用。"""
    return communication_preimage_hash(
        target=request.target_binding.target,
        payload=request.payload,
    )


def communication_preimage_hash(
    *,
    target: GlobalThreadAddress,
    payload: CommunicationPayload,
) -> str:
    """send/receive 双端共用的 preimage hash（sha256: 前缀形式）。

    outbox 与 inbox 的 payload_hash 必须同口径：覆盖 resolved target
    四元组 + content + kind + reply_to；同 preimage 才允许幂等复用。
    """
    canonical = json.dumps(
        {
            "target": {
                "gateway_id": target.gateway_id,
                "workspace_id": target.workspace_id,
                "session_id": target.session_id,
                "thread_id": target.thread_id,
            },
            "content": payload.content,
            "kind": payload.kind,
            "reply_to_communication_id": payload.reply_to_communication_id,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide_send_admission(
    *,
    request: CommunicationSendRequest,
    communication_id: str,
    existing: ExistingOutboxView | None,
    reply_context: ReceivedCommunicationView | None = None,
) -> CommunicationAdmissionDecision:
    """裁决一次 send admission；返回绑定的 communication_id 与幂等标记。

    - existing 为空：新建 outbox，绑定调用方提供的候选 communication_id。
    - 同 operation 同 preimage：dedupe，必须返回既有 communication_id。
    - 同 operation 不同 preimage：communication-preimage-conflict。
    - 不同 operation 但同 communication_id：同 preimage dedupe，否则
      communication-id-conflict。
    - kind=reply：必须提供方向相反的 reply_context 证明被回复因果，
      否则 communication-reply-correlation-conflict。
    """
    if not isinstance(communication_id, str) or not communication_id.strip():
        raise CommunicationContractError(
            "communication-id-conflict",
            "communication_id 必须是非空字符串",
        )
    request_preimage = resolve_request_preimage(request)
    if request.payload.kind == "reply":
        _validate_reply_correlation(request, reply_context)
    if existing is None:
        return CommunicationAdmissionDecision(
            communication_id=communication_id,
            payload_hash=request_preimage,
            is_duplicate=False,
        )
    if existing.source != request.source:
        raise ValueError(
            "existing outbox 视图必须按 request.source 命名空间查询: "
            f"{existing.source} != {request.source}"
        )
    existing_preimage = (existing.target, existing.payload_hash)
    request_target = request.target_binding.target
    request_preimage_pair = (request_target, request_preimage)
    if existing.send_operation_id == request.send_operation_id:
        if existing_preimage != request_preimage_pair:
            raise CommunicationContractError(
                "communication-preimage-conflict",
                "同一 send_operation_id 的重试 preimage 不一致；不得改投新目标"
                "或改写 payload",
            )
        if communication_id != existing.communication_id:
            raise ValueError(
                "同一 send_operation_id 幂等复用必须返回既有 communication_id: "
                f"候选 {communication_id!r} != 既有 {existing.communication_id!r}"
            )
        return CommunicationAdmissionDecision(
            communication_id=existing.communication_id,
            payload_hash=existing.payload_hash,
            is_duplicate=True,
        )
    if existing_preimage == request_preimage_pair:
        return CommunicationAdmissionDecision(
            communication_id=existing.communication_id,
            payload_hash=existing.payload_hash,
            is_duplicate=True,
        )
    raise CommunicationContractError(
        "communication-id-conflict",
        "同一 (source, communication_id) 已绑定不同 payload/target；"
        "新逻辑 send 必须分配新的 communication_id",
    )


def _validate_reply_correlation(
    request: CommunicationSendRequest,
    reply_context: ReceivedCommunicationView | None,
) -> None:
    """校验 reply 因果：被回复 communication 的方向必须与本次 send 相反。"""
    reply_to = request.payload.reply_to_communication_id
    if reply_context is None:
        raise CommunicationContractError(
            "communication-reply-correlation-conflict",
            f"kind=reply 必须提供被回复 communication {reply_to!r} 的收件视图",
        )
    if reply_context.communication_id != reply_to:
        raise CommunicationContractError(
            "communication-reply-correlation-conflict",
            f"reply_context 指向 {reply_context.communication_id!r}，"
            f"与 reply_to_communication_id {reply_to!r} 不一致",
        )
    target = request.target_binding.target
    direction_reversed = (
        reply_context.source == target and reply_context.target == request.source
    )
    if not direction_reversed:
        raise CommunicationContractError(
            "communication-reply-correlation-conflict",
            "被回复 communication 的方向必须与本次 send 相反；"
            f"收件视图方向为 {reply_context.source} -> {reply_context.target}",
        )
