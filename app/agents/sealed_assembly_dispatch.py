"""向发送边界交付 Saver 已提交 assembly 中的最终正文。

ContextVar 只承载本次 handler 的不可变绑定，不重建上下文、不读 storage、
不维护第二份持久账本。非 Agent 调用只能由构建方显式标注用途。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, cast

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
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.session_context import SessionContextResourceDTO

ProviderInvocationPurpose = Literal["agent", "derived", "compaction", "tool_test", "model_test"]


@dataclass(frozen=True, slots=True)
class SealedAssemblyDispatch:
    """同一 dispatch 的私有封存值；只有读取正文时才复制对应容器。"""

    reference: SealedContextDispatchRef
    _snapshot: ContextAssemblySnapshot
    _resource_refs: tuple[SessionContextResourceDTO, ...]
    _provider_params: Mapping[str, object]

    @property
    def snapshot(self) -> ContextAssemblySnapshot:
        return copy.deepcopy(self._snapshot)

    @property
    def resource_refs(self) -> tuple[SessionContextResourceDTO, ...]:
        return copy.deepcopy(self._resource_refs)

    @property
    def provider_params(self) -> Mapping[str, object]:
        return copy.deepcopy(self._provider_params)

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
    resource_refs: tuple[SessionContextResourceDTO, ...],
    provider_params: Mapping[str, object],
) -> Iterator[None]:
    """仅在本次 handler 内绑定 Saver snapshot；两协议都必须有真实封存。"""
    if not isinstance(reference, SealedContextDispatchRef):
        raise TypeError("sealed-assembly binding violation: 缺少 Saver dispatch reference")
    if not isinstance(snapshot, ContextAssemblySnapshot):
        raise TypeError("source-mismatch: dispatch 需要已提交 ContextAssemblySnapshot")
    if not isinstance(resource_refs, tuple) or any(
        not isinstance(ref, SessionContextResourceDTO) for ref in resource_refs
    ):
        raise TypeError("source-mismatch: dispatch 需要 Saver 安全资源摘要")
    if not isinstance(snapshot.request_hash_preimage, Mapping):
        raise TypeError("source-mismatch: sealed assembly 缺少完整 wire preimage")
    try:
        reference.validate_snapshot(snapshot)
    except ItemSchemaError as error:
        raise ValueError("source-mismatch: sealed assembly hash/manifest 非法") from error
    if snapshot.target_format not in {"responses", "chat_completions"}:
        raise ValueError("source-mismatch: sealed dispatch protocol 非法")
    binding = SealedAssemblyDispatch(
        reference=reference, _snapshot=snapshot, _resource_refs=resource_refs,
        _provider_params=provider_params,
    )
    token = _SEALED_ASSEMBLY_DISPATCH.set(copy.deepcopy(binding))
    try:
        yield
    finally:
        _SEALED_ASSEMBLY_DISPATCH.reset(token)


def read_sealed_assembly_dispatch(
    *, invocation_purpose: ProviderInvocationPurpose,
) -> SealedAssemblyDispatch | None:
    """只读取 dispatch；正文属性各自返回副本，不为身份查询复制全部历史。"""
    return _require_dispatch(invocation_purpose)


def read_sealed_provider_request(
    *, invocation_purpose: ProviderInvocationPurpose,
    profile: ChatWireProfile | ResponsesWireProfile,
    params: Mapping[str, object],
) -> dict[str, object] | None:
    """直接交付已封存的协议正文，不从语义消息重新投影。"""
    binding = _require_dispatch(invocation_purpose)
    if binding is None:
        return None
    _validate_profile(binding, profile)
    if canonical_json_bytes(params) != canonical_json_bytes(binding._provider_params):
        raise ValueError("source-mismatch: 当前 Provider 参数与封存请求不一致")
    # 只复制 JSON 容器，不复制不可变正文字符串。
    return copy.deepcopy(cast(dict[str, object], binding._snapshot.request_hash_preimage))


def _require_dispatch(
    invocation_purpose: ProviderInvocationPurpose,
) -> SealedAssemblyDispatch | None:
    """用途由真实构建方给出；缺 binding/admission 不产生直连豁免。"""
    value = _SEALED_ASSEMBLY_DISPATCH.get()
    if invocation_purpose in {"tool_test", "model_test"}:
        if value is not None:
            raise ValueError(f"source-mismatch: {invocation_purpose} 调用不得消费 Agent assembly binding")
        return None
    if invocation_purpose not in {"agent", "derived", "compaction"}:
        raise ValueError("source-mismatch: Provider invocation_purpose 非法")
    if value is None:
        raise ValueError("source-mismatch: Agent Provider 发送缺少 sealed assembly binding")
    if invocation_purpose in {"derived", "compaction"} and (value.reference.turn_id is not None or value._snapshot.selection_policy != "derived_request"):
        raise ValueError("source-mismatch: 派生 Provider 只能发送独立派生请求")
    # scope 入口已完整校验并私有复制，正文属性各自隔离；内部校验不暴露封存值。
    # 读取时只联校当前执行归属，避免每个 callback、日志和发送步骤重复计算完整历史 hash。
    admission = require_current_execution_admission(langgraph_thread_id=value.reference.thread_id)
    if (admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id) != (
        value.reference.session_id, value.reference.thread_id,
        value.reference.execution_id, value.reference.turn_id,
    ):
        raise ValueError("sealed-assembly binding violation: reference 与当前 execution 不一致")
    return value


def _validate_profile(
    binding: SealedAssemblyDispatch,
    profile: ChatWireProfile | ResponsesWireProfile,
) -> None:
    snapshot = binding._snapshot
    target_format = "responses" if isinstance(profile, ResponsesWireProfile) else "chat_completions"
    if (
        profile.identity() != snapshot.provider_version
        or target_format != snapshot.target_format
        or snapshot.projector_id != WIRE_PROJECTOR_ID
        or snapshot.projector_version != WIRE_PROJECTOR_VERSION
    ):
        raise ValueError("source-mismatch: 当前 Provider profile/projector 与 sealed assembly 不一致")


__all__ = [
    "ProviderInvocationPurpose",
    "SealedAssemblyDispatch",
    "read_sealed_assembly_dispatch",
    "read_sealed_provider_request",
    "sealed_assembly_dispatch_scope",
]
