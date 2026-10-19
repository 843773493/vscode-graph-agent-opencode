"""向两协议发送边界传递 Saver 已提交 assembly，并校验实际发送请求。

ContextVar 只承载本次 handler 的不可变绑定，不重建上下文、不读 storage、
不维护第二份持久账本。非 Agent 调用只能由构建方显式标注用途。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
)
from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.serialization import normalize_wire_request

ProviderInvocationPurpose = Literal["agent", "tool_test"]


@dataclass(frozen=True, slots=True)
class SealedAssemblyDispatch:
    """同一已提交 snapshot 与它的可选 Responses 原生投影。"""

    reference: SealedContextDispatchRef
    snapshot: ContextAssemblySnapshot
    native_projection: dict[str, object] | None = None

    @property
    def callback_model_call_id(self) -> str:
        return self.reference.callback_model_call_id

_SEALED_ASSEMBLY_DISPATCH: ContextVar[SealedAssemblyDispatch | None] = ContextVar(
    "sealed_assembly_dispatch",
    default=None,
)


@contextmanager
def sealed_assembly_dispatch_scope(
    reference: SealedContextDispatchRef,
    snapshot: ContextAssemblySnapshot,
    *,
    native_projection: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """仅在本次 handler 内绑定 Saver snapshot；两协议都必须有真实封存。"""
    if not isinstance(reference, SealedContextDispatchRef):
        raise TypeError("sealed-assembly binding violation: 缺少 Saver dispatch reference")
    if not isinstance(snapshot, ContextAssemblySnapshot):
        raise TypeError("source-mismatch: dispatch 需要已提交 ContextAssemblySnapshot")
    if not isinstance(snapshot.request_hash_preimage, Mapping):
        raise TypeError("source-mismatch: sealed assembly 缺少完整 wire preimage")
    try:
        reference.validate_snapshot(snapshot)
    except ItemSchemaError as error:
        raise ValueError("source-mismatch: sealed assembly hash/manifest 非法") from error
    if snapshot.target_format == "responses":
        if not isinstance(native_projection, Mapping):
            raise ValueError("source-mismatch: Responses binding 缺少 sealed native projection")
        required = ("session_id", "assembly_id", "plan_hash", "selection", "request")
        if any(key not in native_projection for key in required):
            raise TypeError("source-mismatch: sealed native projection 缺少 manifest 字段")
        if (
            native_projection["session_id"] != snapshot.session_id
            or native_projection["assembly_id"] != snapshot.assembly_id
            or native_projection["plan_hash"] != snapshot.plan_hash
            or canonical_json_bytes(native_projection["selection"])
            != canonical_json_bytes([entry.to_dict() for entry in snapshot.selection])
        ):
            raise ValueError("source-mismatch: native projection 与 sealed assembly 不一致")
        request = native_projection["request"]
        if not isinstance(request, Mapping) or not all(
            key in request for key in ("input", "tools")
        ):
            raise TypeError("source-mismatch: native request 缺少 sealed input/tools")
    elif snapshot.target_format != "chat_completions" or native_projection is not None:
        raise ValueError("source-mismatch: sealed dispatch protocol/native projection 不一致")
    binding = SealedAssemblyDispatch(
        reference=reference, snapshot=snapshot,
        native_projection=dict(native_projection) if native_projection is not None else None,
    )
    token = _SEALED_ASSEMBLY_DISPATCH.set(copy.deepcopy(binding))
    try:
        yield
    finally:
        _SEALED_ASSEMBLY_DISPATCH.reset(token)


def read_sealed_assembly_dispatch(
    *, invocation_purpose: ProviderInvocationPurpose,
) -> SealedAssemblyDispatch | None:
    """用途由真实构建方给出；缺 binding/admission 不产生直连豁免。"""
    value = _SEALED_ASSEMBLY_DISPATCH.get()
    if invocation_purpose == "tool_test":
        if value is not None:
            raise ValueError("source-mismatch: tool_test 调用不得消费 Agent assembly binding")
        return None
    if invocation_purpose != "agent":
        raise ValueError("source-mismatch: Provider invocation_purpose 非法")
    if value is None:
        raise ValueError("source-mismatch: Agent Provider 发送缺少 sealed assembly binding")
    value.reference.validate_snapshot(value.snapshot)
    admission = require_current_execution_admission(langgraph_thread_id=value.reference.thread_id)
    if (admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id) != (
        value.reference.session_id, value.reference.thread_id,
        value.reference.execution_id, value.reference.turn_id,
    ):
        raise ValueError("sealed-assembly binding violation: reference 与当前 execution 不一致")
    return copy.deepcopy(value)


def validate_sealed_assembly_dispatch(
    *,
    invocation_purpose: ProviderInvocationPurpose,
    profile: ChatWireProfile | ResponsesWireProfile,
    wire_request: Mapping[str, object],
) -> None:
    """在真实网络入口前校 profile、完整 finalized body 与同一 request hash。"""
    binding = read_sealed_assembly_dispatch(invocation_purpose=invocation_purpose)
    if binding is None:
        return
    snapshot = binding.snapshot
    target_format = "responses" if isinstance(profile, ResponsesWireProfile) else "chat_completions"
    if (
        profile.identity() != snapshot.provider_version
        or target_format != snapshot.target_format
        or snapshot.projector_id != WIRE_PROJECTOR_ID
        or snapshot.projector_version != WIRE_PROJECTOR_VERSION
    ):
        raise ValueError("source-mismatch: 当前 Provider profile/projector 与 sealed assembly 不一致")
    try:
        preimage = normalize_wire_request(wire_request)
        actual_body = canonical_json_bytes(preimage)
        sealed_body = canonical_json_bytes(snapshot.request_hash_preimage)
        actual_hash = context_request_hash(
            snapshot.as_sealed_plan(),
            profile.identity(),
            projector_id=WIRE_PROJECTOR_ID,
            projector_version=WIRE_PROJECTOR_VERSION,
            target_format=target_format,
            wire_request=preimage,
        )
    except ItemSchemaError as error:
        raise ValueError("source-mismatch: 实际 Provider wire preimage 非法") from error
    if actual_body != sealed_body or actual_hash != snapshot.request_hash:
        raise ValueError("source-mismatch: 实际 Provider wire 与 sealed request_hash/preimage 不一致")


__all__ = [
    "ProviderInvocationPurpose",
    "SealedAssemblyDispatch",
    "read_sealed_assembly_dispatch",
    "sealed_assembly_dispatch_scope",
    "validate_sealed_assembly_dispatch",
]
