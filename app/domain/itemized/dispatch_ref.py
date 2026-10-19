"""Saver 签发的不可变派发引用；不携带运行时模型或可变来源状态。"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from uuid import UUID

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import EpochReason
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ToolSetRef


@dataclass(frozen=True, slots=True)
class SealedContextDispatchRef:
    session_id: str
    thread_id: str
    execution_id: str
    turn_id: str | None
    checkpoint_ns: str
    attempt_key: str
    callback_model_call_id: str
    sealed_model_call_id: str
    assembly_id: str
    plan_hash: str
    request_hash: str
    provider_version: str
    projector_id: str
    projector_version: str
    target_format: str
    prefix_epoch: str
    epoch_reason: EpochReason
    prefix_hash: str
    prefix_byte_length: int
    frame_hashes: tuple[str, ...]
    tool_set_refs: tuple[ToolSetRef, ...]
    tool_policy_hash: str
    reference_hash: str

    @classmethod
    def from_snapshot(
        cls, snapshot: ContextAssemblySnapshot, *, checkpoint_ns: str,
        attempt_key: str, callback_model_call_id: str,
    ) -> SealedContextDispatchRef:
        """签发和只读恢复共用同一绑定投影，禁止从当前请求补造字段。"""
        if attempt_key != snapshot.plan_id or not isinstance(checkpoint_ns, str):
            raise ValueError("sealed-assembly binding violation: attempt/namespace 非法")
        UUID(callback_model_call_id)
        snapshot.validate_hashes()
        binding = snapshot.seal_binding
        if binding is None or snapshot.model_call_id is None:
            raise ValueError("sealed-assembly binding violation: 缺少 prefix/model-call binding")
        prefix = binding.prefix
        reference = cls(
            session_id=snapshot.session_id, thread_id=snapshot.thread_id,
            execution_id=snapshot.execution_id, turn_id=snapshot.turn_id,
            checkpoint_ns=checkpoint_ns, attempt_key=attempt_key,
            callback_model_call_id=callback_model_call_id,
            sealed_model_call_id=snapshot.model_call_id, assembly_id=snapshot.assembly_id,
            plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
            provider_version=snapshot.provider_version, projector_id=snapshot.projector_id,
            projector_version=snapshot.projector_version, target_format=snapshot.target_format,
            prefix_epoch=prefix.state.epoch.token, epoch_reason=prefix.state.epoch.reason,
            prefix_hash=prefix.prefix_hash, prefix_byte_length=prefix.prefix_byte_length,
            frame_hashes=tuple(frame.frame_hash() for frame in prefix.state.frames),
            tool_set_refs=snapshot.tool_set_refs,
            tool_policy_hash=prefix.state.tool_compatibility_key,
            reference_hash="",
        )
        payload = {field.name: getattr(reference, field.name) for field in fields(reference)
                   if field.name not in {"tool_set_refs", "reference_hash"}}
        payload["tool_set_refs"] = [ref.to_dict() for ref in reference.tool_set_refs]
        payload["frame_hashes"] = list(reference.frame_hashes)
        return replace(reference, reference_hash=sha256_jcs(payload))

    def validate_snapshot(self, snapshot: ContextAssemblySnapshot) -> None:
        expected = type(self).from_snapshot(
            snapshot, checkpoint_ns=self.checkpoint_ns, attempt_key=self.attempt_key,
            callback_model_call_id=self.callback_model_call_id,
        )
        if self != expected:
            raise ValueError("sealed-assembly binding violation: reference 与 sealed payload 不一致")
