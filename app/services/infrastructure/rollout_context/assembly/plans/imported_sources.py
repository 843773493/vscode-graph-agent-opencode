"""显式 import 的 target-local 来源清单；不是 draft，也不解析正文。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    resolve_contribution_for_ref,
)
from app.domain.itemized.serde.registry import parse_context_ref, parse_contribution
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    json_text,
)
from app.services.infrastructure.rollout_context.assembly.plans.privacy import (
    _validate_metadata,
)

_FIELDS = {"schema", "session_id", "plan_id", "refs", "contributions"}
_INTEGRITY_FIELDS = (
    "source_revision", "content_length", "content_hash", "redacted_stable_digest",
)


@dataclass(frozen=True, slots=True)
class ParsedSourceManifest:
    manifest: dict[str, object]
    refs: tuple[ContextRef, ...]
    contributions: tuple[ContextContribution, ...]


def _contribution_value(item: ContextContribution) -> dict[str, object]:
    # ContextContribution 没有独立 to_dict；复用其 dataclass 字段和严格 domain parser。
    # 必须在提取字段前拒绝正文，不能经 plan.to_dict 静默删掉 protected body。
    if not isinstance(item, ContextContribution) or item.body is not None:
        raise ValueError("source-mismatch: source manifest 必须是无正文 contribution")
    return {field.name: getattr(item, field.name) for field in fields(ContextContribution)}


def _parse(value: object, session_id: str, plan_id: str) -> ParsedSourceManifest:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ValueError("source manifest 字段非法")
    if value["schema"] != "context-plan-source:v1":
        raise ValueError("source manifest schema 非法")
    if any(not isinstance(owner, str) or not owner.strip() for owner in (session_id, plan_id)):
        raise ValueError("source manifest owner 非法")
    if (value["session_id"], value["plan_id"]) != (session_id, plan_id):
        raise ValueError("source manifest owner 不一致")
    if not isinstance(value["refs"], list) or not isinstance(value["contributions"], list):
        raise TypeError("source manifest registry 必须是 array")
    refs = tuple(parse_context_ref(ref) for ref in value["refs"])
    if len({(ref.ref_type, ref.ref_id) for ref in refs}) != len(refs):
        raise ValueError("source manifest 重复 ref")
    for ref in refs:
        if ref.session_id != session_id or ref.plan_id != (
            plan_id if ref.ref_type == "request_only" else None
        ):
            raise ValueError("source manifest ref owner 不一致")
    contributions = []
    for raw in value["contributions"]:
        if not isinstance(raw, Mapping) or raw.get("body") is not None:
            raise ValueError("source manifest 不得包含 inline body")
        item = parse_contribution(raw, sealed=False)
        if item.assembly_id is not None or item.contribution_ordinal is not None:
            raise ValueError("source manifest 不得包含最终 assembly/ordinal binding")
        _validate_metadata(item.metadata, field="contribution.metadata")
        contributions.append(item)
    if len({item.contribution_id for item in contributions}) != len(contributions):
        raise ValueError("source manifest 重复 contribution")
    # JCS 同时拒绝非法 number/Unicode，往返只隔离可变容器，不删字段或改 hash 语义。
    manifest = json.loads(json_text(value))
    return ParsedSourceManifest(manifest, refs, tuple(contributions))


def parse_source_manifest(
    value: object, *, session_id: str, plan_id: str
) -> ParsedSourceManifest:
    """严格解析显式清单；安全边界不将不可信 metadata 放进异常链。"""
    try:
        result = _parse(value, session_id, plan_id)
    except Exception:  # noqa: BLE001 - domain 异常可能携带敏感原值
        failed = True
    else:
        failed = False
    if failed:
        raise ValueError("source-mismatch: imported source manifest schema/owner/privacy 非法")
    return result


def read_source_manifest(
    raw: object, *, session_id: str, plan_id: str
) -> ParsedSourceManifest:
    """持久字段必须是严格 JCS；拒绝重复 key、空值和隐式格式归一化。"""
    try:
        if not isinstance(raw, str):
            raise TypeError("source manifest JSON 必须是字符串")
        value = json.loads(raw)
        if raw != json_text(value):
            raise ValueError("source manifest 不是规范 JCS")
        result = _parse(value, session_id, plan_id)
    except Exception:  # noqa: BLE001 - 恢复错误不能保留正文/凭据
        failed = True
    else:
        failed = False
    if failed:
        raise ValueError("source-mismatch: imported source manifest JCS/schema/owner/privacy 非法")
    return result


def build_source_manifest(
    *, session_id: str, plan_id: str, refs: Sequence[ContextRef],
    contributions: Sequence[ContextContribution],
) -> dict[str, object]:
    """调用方显式提供已验证、无正文且尚未绑定 assembly 的来源。"""
    value = {
        "schema": "context-plan-source:v1",
        "session_id": session_id,
        "plan_id": plan_id,
        "refs": [ref.to_dict() for ref in refs],
        "contributions": [_contribution_value(item) for item in contributions],
    }
    return parse_source_manifest(value, session_id=session_id, plan_id=plan_id).manifest


def validate_source_bindings(
    snapshot: ContextAssemblySnapshot, sources: ParsedSourceManifest
) -> None:
    """含 omitted 的所有显式 binding 都由同一来源清单证明，不访问 detail/body。"""
    refs = {(ref.ref_type, ref.ref_id): ref for ref in sources.refs}
    for ref in snapshot.refs:
        if refs.get((ref.ref_type, ref.ref_id)) != ref:
            raise ValueError("source-mismatch: snapshot ref 与 imported source manifest 不一致")
    included = {item.contribution_id: item for item in snapshot.contributions}
    bound_ids = set()
    for entry in snapshot.selection:
        if not isinstance(entry.ref, ContextRef):
            continue
        ref = refs.get((entry.ref.ref_type, entry.ref.ref_id))
        if ref != entry.ref:
            raise ValueError("source-mismatch: selection 缺少相同 source ref")
        if ref.ref_type != "request_only" or (not entry.included and entry.contribution_id is None):
            continue
        item = resolve_contribution_for_ref(ref, sources.contributions)
        if item is None:
            if entry.contribution_id is not None:
                raise ValueError("plan-order-integrity: contribution binding 缺少 source manifest")
            continue
        if item.contribution_id != entry.contribution_id:
            raise ValueError("plan-order-integrity: contribution binding 与 source ref 不一致")
        for field in _INTEGRITY_FIELDS:
            if any(
                getattr(value, field) is not None
                and getattr(value, field) != getattr(item, field)
                for value in (entry, ref)
            ):
                raise ValueError("source-mismatch: contribution binding 已知 metadata 不一致")
        if (entry.visibility, entry.protection) != (item.visibility, item.protection):
            raise ValueError("source-mismatch: contribution visibility/protection 不一致")
        if not entry.included:
            continue
        actual = included.get(item.contribution_id)
        if actual is None:
            raise ValueError("plan-order-integrity: included source 缺少 sealed contribution")
        # domain seal 只移除独立 source_ordinal，并新分配 assembly/贡献 ordinal。
        expected = _contribution_value(item)
        expected.update(
            assembly_id=snapshot.assembly_id,
            contribution_ordinal=entry.contribution_ordinal,
            metadata={key: value for key, value in item.metadata.items() if key != "source_ordinal"},
        )
        if json_text(expected) != json_text(_contribution_value(actual)):
            raise ValueError("source-mismatch: sealed contribution 与来源清单不一致")
        bound_ids.add(item.contribution_id)
    if set(included) != bound_ids:
        raise ValueError("plan-order-integrity: sealed contribution 存在无来源 binding")
