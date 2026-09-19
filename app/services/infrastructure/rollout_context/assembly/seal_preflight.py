"""seal/dispatch 边界的纯 manifest 验证合同；只读 typed 字段，不做任何 I/O。

OpenSpec add-context-injection-lifecycle 2.5 的第一步落盘：把同一 assembly 的
plan ordinal、source identity/revision 唯一性、base→delta 顺序和 canonical
tool 配对收敛为唯一验证入口。冲突 fail closed，错误码是闭合的控制分类，
禁止把含正文的异常信息写进持久失败记录。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import BaseDeltaRole, SemanticKind
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.tool_call_identity import provider_tool_call_id
from app.services.mapping.itemized.carrier_dedup import superseded_stream_item_ids

_ERROR_CODES = {
    "seal-dispatch-plan-ordinal-conflict",
    "seal-dispatch-source-identity-conflict",
    "seal-dispatch-overlay-order-conflict",
    "seal-dispatch-tool-pairing-conflict",
}

_TOOL_KINDS = {SemanticKind.TOOL_CALL.value, SemanticKind.TOOL_RESULT.value}


class ContextAssemblySealPreflightError(RuntimeError):
    """seal/dispatch preflight 冲突；code 是闭合控制分类。"""

    def __init__(self, code: str, detail: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"未登记的 seal preflight 错误码: {code}")
        self.code = code
        super().__init__(f"{code}: {detail}")


def validate_seal_dispatch_invariants(
    snapshot: ContextAssemblySnapshot,
    *,
    tool_pairings: Sequence[tuple[str, str]] | None = None,
) -> None:
    """在 seal 提交与 dispatch 复用前验证同一 assembly 的结构性不变量。

    tool_pairings 是 seal owner 从 canonical item metadata 解析出的
    (tool_call_ref_id, tool_result_ref_id) 显式配对；assembly 含 tool 条目
    而不提供配对时必须 fail closed，不允许用猜测关系放行 dispatch。
    """
    _validate_plan_ordinals(snapshot)
    _validate_source_identity_uniqueness(snapshot)
    _validate_overlay_order(snapshot)
    _validate_tool_pairing(snapshot, tool_pairings)


def canonical_tool_pairings(
    items: Iterable[CanonicalItemRecord],
) -> tuple[tuple[str, str], ...]:
    """从已提交 canonical items 的显式 tool_call_id 派生 (call, result) 配对。

    这是唯一 seal owner 边界的 typed 配对来源：tool call 正文携带
    codec 落盘约定内的显式 call id（缺失时回退 item_id，与 codec 投影
    逐字节一致），tool result 正文携带显式 tool_call_id。stream carrier
    可能保存 model-call scoped ID，先经 provider_tool_call_id 还原为 provider
    call 身份；stream 影子 carrier 与 checkpoint carrier 属同一次调用，全部
    与同一 result 配对。任何缺少显式链接或身份无法消歧的条目直接 fail
    closed，不做顺序/名称猜测。
    """
    item_tuple = tuple(items)
    for item in item_tuple:
        if not isinstance(item, CanonicalItemRecord):
            raise TypeError("canonical tool pairing 只接受 CanonicalItemRecord")
    try:
        superseded_ids = superseded_stream_item_ids(item_tuple)
    except ValueError as error:
        raise ContextAssemblySealPreflightError(
            "seal-dispatch-tool-pairing-conflict",
            f"工具 carrier 身份无法消歧: {error}",
        ) from error
    call_carriers: dict[tuple[str | None, str], list[str]] = {}
    result_ids: dict[tuple[str | None, str], str] = {}
    for item in item_tuple:
        payload = item.payload if isinstance(item.payload, Mapping) else {}
        if item.semantic_kind == SemanticKind.TOOL_CALL.value:
            raw_calls = payload.get("tool_calls")
            calls = raw_calls if isinstance(raw_calls, list) else [payload]
            for call in calls:
                if not isinstance(call, Mapping):
                    raise ContextAssemblySealPreflightError(
                        "seal-dispatch-tool-pairing-conflict",
                        f"tool call item {item.item_id!r} 正文不是显式 call mapping",
                    )
                explicit = call.get("id") or call.get("tool_call_id") or item.item_id
                if not isinstance(explicit, str) or not explicit:
                    raise ContextAssemblySealPreflightError(
                        "seal-dispatch-tool-pairing-conflict",
                        f"tool call item {item.item_id!r} 缺少显式 call id，禁止猜测配对",
                    )
                key = (
                    _model_call_scope(item),
                    provider_tool_call_id(item.metadata, explicit),
                )
                carriers = call_carriers.setdefault(key, [])
                if item.item_id not in carriers:
                    carriers.append(item.item_id)
        elif item.semantic_kind == SemanticKind.TOOL_RESULT.value:
            if item.item_id in superseded_ids:
                # stream 影子 result：由 checkpoint result carrier 参与配对。
                continue
            explicit = payload.get("tool_call_id")
            if not isinstance(explicit, str) or not explicit:
                explicit = item.metadata.get("tool_call_id")
            if not isinstance(explicit, str) or not explicit:
                raise ContextAssemblySealPreflightError(
                    "seal-dispatch-tool-pairing-conflict",
                    f"tool result item {item.item_id!r} 缺少显式 tool_call_id，"
                    "禁止猜测配对",
                )
            key = (
                _model_call_scope(item),
                provider_tool_call_id(item.metadata, explicit),
            )
            prior = result_ids.get(key)
            if prior is not None and prior != item.item_id:
                raise ContextAssemblySealPreflightError(
                    "seal-dispatch-tool-pairing-conflict",
                    f"显式 tool_call_id {explicit!r} 在同一 model call 内被多个"
                    f"result item 声明: {prior!r}, {item.item_id!r}",
                )
            result_ids[key] = item.item_id
    pairings: list[tuple[str, str]] = []
    scopeless_results: dict[str, str] = {}
    for (scope, provider_id), result in result_ids.items():
        if scope is None:
            scopeless_results[provider_id] = result
    for key, carriers in call_carriers.items():
        scope, provider_id = key
        result = result_ids.get(key)
        if result is None and scope is not None:
            # 无 provenance 的旧 result 只在该 provider 的 call scope 唯一
            # 时可复用；provider 重用原始 ID 的多个 scope 必须显式消歧。
            result = scopeless_results.get(provider_id)
            if result is not None:
                scoped_call_scopes = {
                    call_scope
                    for call_scope, call_provider in call_carriers
                    if call_provider == provider_id and call_scope is not None
                }
                if len(scoped_call_scopes) > 1:
                    raise ContextAssemblySealPreflightError(
                        "seal-dispatch-tool-pairing-conflict",
                        f"tool_call_id {provider_id!r} 被多个 model call 复用，"
                        "旧 result 缺少 provenance 无法消歧",
                    )
        if result is None:
            continue
        survivors = [item_id for item_id in carriers if item_id not in superseded_ids]
        if len(survivors) > 1:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                f"显式 call id {key[1]!r} 被多个 call item 声明: {survivors!r}",
            )
        # 影子 stream carrier 与 checkpoint carrier 是同一次调用，全部
        # 与同一 result 配对，保证 preflight 覆盖所有 included tool entry。
        pairings.extend((item_id, result) for item_id in carriers)
    return tuple(pairings)


def _model_call_scope(item: CanonicalItemRecord) -> str | None:
    """读取 tool 条目的可验证 model-call provenance。"""

    model_call_id = item.metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        return f"model-call:{model_call_id}"
    return None


def _included_entries(
    snapshot: ContextAssemblySnapshot,
) -> list[tuple[int, ContextRef]]:
    return [
        (ordinal, entry.ref)
        for ordinal, entry in enumerate(snapshot.selection)
        if entry.included and isinstance(entry.ref, ContextRef)
    ]


def _validate_plan_ordinals(snapshot: ContextAssemblySnapshot) -> None:
    ordinals = [entry.plan_ordinal for entry in snapshot.selection]
    if ordinals != list(range(len(ordinals))):
        raise ContextAssemblySealPreflightError(
            "seal-dispatch-plan-ordinal-conflict",
            f"selection plan_ordinal 必须从 0 连续递增: {ordinals}",
        )


def _validate_source_identity_uniqueness(snapshot: ContextAssemblySnapshot) -> None:
    seen: set[tuple[str, str]] = set()
    registry = {(ref.ref_type, ref.ref_id): ref for ref in snapshot.refs}
    included = _included_entries(snapshot)
    entry_by_identity = {
        (ref.ref_type, ref.ref_id): snapshot.selection[ordinal]
        for ordinal, ref in included
    }
    for _, ref in included:
        identity = (ref.ref_type, ref.ref_id)
        if identity in seen:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-source-identity-conflict",
                f"included selection 重复 source identity: {identity}",
            )
        seen.add(identity)
        source = registry.get(identity)
        if source is None:
            continue
        entry = entry_by_identity[identity]
        for field in ("source_revision", "content_hash", "content_length"):
            source_value = getattr(source, field)
            entry_value = getattr(entry, field)
            if (
                source_value is not None
                and entry_value is not None
                and source_value != entry_value
            ):
                raise ContextAssemblySealPreflightError(
                    "seal-dispatch-source-identity-conflict",
                    f"selection 与 ref manifest 不一致: {identity} {field}",
                )


def _validate_overlay_order(snapshot: ContextAssemblySnapshot) -> None:
    overlay_ids = {
        contribution.contribution_id: contribution.metadata.get("overlay_id")
        or contribution.contribution_id
        for contribution in snapshot.contributions
    }
    chain_tail: dict[tuple[str, int | None], str] = {}
    for ordinal, ref in _included_entries(snapshot):
        entry = snapshot.selection[ordinal]
        if ref.base_delta_role == BaseDeltaRole.NONE.value:
            continue
        overlay_id = (
            overlay_ids.get(entry.contribution_id)
            if entry.contribution_id is not None
            else None
        )
        if overlay_id is None:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-overlay-order-conflict",
                f"included overlay entry 缺少唯一 overlay identity: {ref.ref_id}",
            )
        key = (overlay_id, entry.source_overlay_epoch)
        if ref.base_delta_role == BaseDeltaRole.BASE.value:
            if key in chain_tail:
                raise ContextAssemblySealPreflightError(
                    "seal-dispatch-overlay-order-conflict",
                    f"overlay chain 重复 base: {key}",
                )
            if ref.source_revision is None:
                raise ContextAssemblySealPreflightError(
                    "seal-dispatch-overlay-order-conflict",
                    f"overlay base 缺少 source_revision: {ref.ref_id}",
                )
            chain_tail[key] = ref.source_revision
            continue
        tail = chain_tail.get(key)
        if tail is None:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-overlay-order-conflict",
                f"overlay delta 缺少 included base/chain: {key}",
            )
        if entry.overlay_from_revision != tail:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-overlay-order-conflict",
                f"overlay delta from_revision 与链尾不一致: {ref.ref_id}",
            )
        if entry.overlay_to_revision is None:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-overlay-order-conflict",
                f"overlay delta 缺少 to_revision: {ref.ref_id}",
            )
        chain_tail[key] = entry.overlay_to_revision


def _validate_tool_pairing(
    snapshot: ContextAssemblySnapshot,
    tool_pairings: Sequence[tuple[str, str]] | None,
) -> None:
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    for ordinal, ref in _included_entries(snapshot):
        if ref.ref_type != "canonical_item" or ref.semantic_kind not in _TOOL_KINDS:
            continue
        target = calls if ref.semantic_kind == SemanticKind.TOOL_CALL.value else results
        if ref.ref_id in target:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                f"included tool entry 重复 ref_id: {ref.ref_id}",
            )
        target[ref.ref_id] = ordinal
    if not calls and not results:
        if tool_pairings:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                "assembly 不含 tool entry，不能提供 tool 配对",
            )
        return
    if tool_pairings is None:
        raise ContextAssemblySealPreflightError(
            "seal-dispatch-tool-pairing-conflict",
            "assembly 含 tool entry 但缺少显式 tool 配对，禁止 dispatch",
        )
    seen_calls: set[str] = set()
    for call_ref_id, result_ref_id in tool_pairings:
        if call_ref_id not in calls or result_ref_id not in results:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                f"tool 配对引用了不存在的 entry: {call_ref_id} -> {result_ref_id}",
            )
        if call_ref_id in seen_calls:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                f"tool entry 被重复配对: {call_ref_id} -> {result_ref_id}",
            )
        # 同一次调用的多个 carrier（stream 影子 + checkpoint）允许共享同一
        # result；call 侧仍必须唯一配对。
        if results[result_ref_id] <= calls[call_ref_id]:
            raise ContextAssemblySealPreflightError(
                "seal-dispatch-tool-pairing-conflict",
                f"tool result 必须位于对应 call 之后: {call_ref_id} -> {result_ref_id}",
            )
        seen_calls.add(call_ref_id)
    unpaired = (set(calls) - seen_calls) | (
        set(results) - {result_ref_id for _call, result_ref_id in tool_pairings}
    )
    if unpaired:
        raise ContextAssemblySealPreflightError(
            "seal-dispatch-tool-pairing-conflict",
            f"tool entry 缺少配对: {sorted(unpaired)}",
        )


__all__ = [
    "ContextAssemblySealPreflightError",
    "validate_seal_dispatch_invariants",
]
