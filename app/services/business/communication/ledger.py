"""跨 Session 通信 ledger facade：唯一 gate → lease → store 链路。

每个 facade 实例绑定一个 Session（该 session-control.sqlite 的唯一 owner
连接 + 该 session 的 SessionLifecycleGate）。source outbox 与 target inbox
分别由各自 Session 的 facade 在各自 gate 内建立 communication-side lease；
任何方法内绝不获取第二个 Session gate（design.md 锁序固定，双端是两次
独立的 gate 进入，组成可恢复 saga，不构成分布式事务）。
冲突裁决由 SessionControlStore 的 communication API 在同一事务内完成
（create-or-get / preimage fail closed / main binding fresh 校验 / reply
因果证明），本层只做 typed 入参校验、lease 建立与 sha256 前缀互转，
不复制第二套幂等逻辑。
"""

from __future__ import annotations

from app.core.session_control_store import (
    CommunicationInboxRecord,
    SessionControlStore,
)
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.services.business.communication.addresses import (
    GlobalThreadAddress,
)
from app.services.business.communication.admission import (
    CommunicationAdmissionDecision,
    CommunicationPayload,
    CommunicationSendRequest,
    communication_preimage_hash,
    resolve_request_preimage,
)

__all__ = ["CommunicationLedgerService", "bare_sha256"]


def bare_sha256(preimage: str) -> str:
    """typed 合同的 sha256: 前缀形式转为数据库内的裸 hex 形态。"""
    if not preimage.startswith("sha256:"):
        raise ValueError(f"preimage 必须是 sha256 前缀形式: {preimage!r}")
    return preimage.removeprefix("sha256:")


class CommunicationLedgerService:
    """单 Session 的通信 ledger 门面：gate 内短事务建立 lease 并落库。"""

    def __init__(
        self,
        *,
        session_id: str,
        store: SessionControlStore,
        gate: SessionLifecycleGate,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._gate = gate

    async def admit_outgoing_send(
        self,
        *,
        request: CommunicationSendRequest,
        communication_id: str,
    ) -> CommunicationAdmissionDecision:
        """source 侧 admission：gate 内建立 communication_source lease 并
        create-or-get outbox；同 operation 不同 preimage 或同
        communication_id 不同 payload/target 在 store 层 fail closed。
        """
        preimage = resolve_request_preimage(request)
        async with self._gate.exclusive(self._session_id):
            self._store.create_or_get_lease(
                operation_kind="communication_source",
                operation_identity=(
                    f"communication-source|{request.send_operation_id}"
                ),
                preimage_hash=bare_sha256(preimage),
            )
            record, created = self._store.create_or_get_communication_outbox(
                session_id=self._session_id,
                send_operation_id=request.send_operation_id,
                communication_id=communication_id,
                source_gateway_id=request.source.gateway_id,
                source_workspace_id=request.source.workspace_id,
                source_thread_id=request.source.thread_id,
                target_gateway_id=request.target_binding.target.gateway_id,
                target_workspace_id=request.target_binding.target.workspace_id,
                target_session_id=request.target_binding.target.session_id,
                target_thread_id=request.target_binding.target.thread_id,
                kind=request.payload.kind,
                reply_to_communication_id=(
                    request.payload.reply_to_communication_id
                ),
                payload_hash=bare_sha256(preimage),
            )
        return CommunicationAdmissionDecision(
            communication_id=record.communication_id,
            payload_hash=preimage,
            is_duplicate=not created,
        )

    async def accept_incoming_send(
        self,
        *,
        communication_id: str,
        source: GlobalThreadAddress,
        target: GlobalThreadAddress,
        payload: CommunicationPayload,
    ) -> CommunicationInboxRecord:
        """target 侧 acceptance：gate 内建立 communication_target lease 并
        create-or-get inbox。

        main binding 由 typed 地址校验与 store 同事务 fresh 校验双重把关
        （target.session_id 必须等于本 facade 绑定的 Session，main 一致性
        由 store 读取 thread_catalog 唯一 main row 裁决）；payload hash
        与 source 侧 outbox 同口径（communication_preimage_hash）。
        """
        if target.session_id != self._session_id:
            raise ValueError(
                "accept_incoming_send 的 target.session_id 必须等于 facade "
                f"绑定的 Session: {target.session_id!r} != {self._session_id!r}"
            )
        preimage = communication_preimage_hash(target=target, payload=payload)
        async with self._gate.exclusive(self._session_id):
            self._store.create_or_get_lease(
                operation_kind="communication_target",
                operation_identity=f"communication-target|{communication_id}",
                preimage_hash=bare_sha256(preimage),
            )
            record, _created = self._store.create_or_get_communication_inbox(
                session_id=self._session_id,
                communication_id=communication_id,
                source_gateway_id=source.gateway_id,
                source_workspace_id=source.workspace_id,
                source_session_id=source.session_id,
                source_thread_id=source.thread_id,
                target_thread_id=target.thread_id,
                kind=payload.kind,
                reply_to_communication_id=payload.reply_to_communication_id,
                payload_hash=bare_sha256(preimage),
            )
        return record

    def list_unbound_inboxes(self) -> tuple[CommunicationInboxRecord, ...]:
        """startup 恢复用只读状态索引（不扫目录、不依赖内存 future）。"""
        return self._store.list_target_accepted_communication_inboxes()

    def get_inbox(self, communication_id: str) -> CommunicationInboxRecord:
        """读取单条 inbox 投影。"""
        return self._store.get_communication_inbox(communication_id)
