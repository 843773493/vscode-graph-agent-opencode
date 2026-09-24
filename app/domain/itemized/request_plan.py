"""v2 request-only contribution 与 ContextRequestPlan domain type。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.domain.itemized.enums import (
    BaseDeltaRole,
    DetailProtection,
    SelectionKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    _ensure_json_value,
    canonical_json_bytes,
    contribution_content_hash,
    validate_hash_token,
)
from app.domain.itemized.plan_hash import context_plan_hash
from app.domain.itemized.refs import ContextRef, ToolSetRef, unique_ref_identities
from app.domain.itemized.root_compilation import RootPlacement
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import (
    _non_empty_string,
    ordered_selection,
)


@dataclass(frozen=True, slots=True)
class ContextContribution:
    contribution_id: str
    source_kind: str
    source_revision: str
    content_hash: str | None = None
    request_only: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    contribution_kind: str = "prompt"
    body: object | None = None
    content_length: int | None = None
    redacted_stable_digest: str | None = None
    visibility: str = "internal"
    protection: str = "public"
    assembly_id: str | None = None
    contribution_ordinal: int | None = None
    # source_ordinal 是 itemized registry（SQLite context_contributions 列）
    # 分配的稳定 slot 序号；producer、CSM、watch reaction 和 ledger 都不得
    # 从事件顺序、内存计数或 metadata/extensions 补造。sealed assembly 使用
    # contribution_ordinal，本字段归 None。
    source_ordinal: int | None = None
    # root_placement 是 source owner 显式声明的根资格（E1 合同）。默认
    # tail_only 对齐规范“默认外部内容与未受信指引恒为 tail_only”；只有
    # root producer（如 sealed system slot）显式声明 root_eligible。
    root_placement: RootPlacement = "tail_only"
    # replaceable_source 声明当前 contribution 占据一个由 producer 拥有、
    # 可在同一 owner slot 内原位更新 revision 的可替换 source slot。它参与
    # 「替换 / 安全判定」——决定同一 contribution_id 的未封存 registry 记录
    # 是否允许被新 revision 覆盖，因此必须是 typed core 字段，不能由自由
    # metadata/extensions 的同名 key 承载。默认 False 与迁移前语义一致：
    # 只有 middleware 提供的 system slot 显式声明为 True，其余 provenance
    # contribution 恒为 False，且不得被任何扩展值补造。
    replaceable_source: bool = False

    def __post_init__(self) -> None:
        for name in ("contribution_id", "source_kind", "source_revision"):
            _non_empty_string(getattr(self, name), f"ContextContribution.{name}")
        if not isinstance(self.contribution_kind, str) or self.contribution_kind not in {
            "prompt",
            "overlay_base",
            "overlay_delta",
            "notice",
        }:
            raise ItemSchemaError(
                f"contribution-kind-unsupported: 不支持的 ContextContribution.contribution_kind: {self.contribution_kind}"
            )
        if self.request_only is not True:
            raise ItemSchemaError(
                "ContextContribution.request_only 必须为 true；工具定义使用 ToolSetRef"
            )
        if not isinstance(self.visibility, str) or self.visibility not in {
            "public",
            "internal",
            "private",
        }:
            raise ItemSchemaError(
                f"未知 ContextContribution.visibility: {self.visibility}"
            )
        if not isinstance(self.protection, str) or self.protection not in {
            item.value for item in DetailProtection
        }:
            raise ItemSchemaError(
                f"未知 ContextContribution.protection: {self.protection}"
            )
        if self.source_kind == "tool_set":
            raise ItemSchemaError("tool_set 只能通过 ToolSetSnapshot/ToolSetRef 表达")
        if self.root_placement not in ("root_eligible", "tail_only"):
            raise ItemSchemaError(
                f"未知 ContextContribution.root_placement: {self.root_placement!r}；"
                "root 资格只能由 owner 显式声明为 root_eligible|tail_only"
            )
        if not isinstance(self.replaceable_source, bool):
            raise ItemSchemaError(
                "ContextContribution.replaceable_source 必须是 boolean"
            )
        if not isinstance(self.metadata, Mapping):
            raise ItemSchemaError("ContextContribution.metadata 必须是 object")
        if self.body is not None:
            expected_hash = contribution_content_hash(self.contribution_kind, self.body)
            if self.content_hash != expected_hash:
                raise ItemSchemaError("ContextContribution.content_hash 与 body 不一致")
            expected_length = len(canonical_json_bytes(self.body))
            if self.content_length is not None and self.content_length != expected_length:
                raise ItemSchemaError("ContextContribution.content_length 与 body 不一致")
            object.__setattr__(self, "content_length", expected_length)
        if self.content_length is None:
            raise ItemSchemaError(
                "ContextContribution.content_length 必须存在，正文缺失时也必须保留 source manifest"
            )
        if (
            not isinstance(self.content_length, int)
            or isinstance(self.content_length, bool)
            or self.content_length < 0
        ):
            raise ItemSchemaError("ContextContribution.content_length 必须是非负整数")
        if self.redacted_stable_digest is not None:
            validate_hash_token(
                self.redacted_stable_digest,
                "ContextContribution.redacted_stable_digest",
                redacted=True,
            )
            if self.protection == DetailProtection.PUBLIC:
                raise ItemSchemaError(
                    "ContextContribution.redacted_stable_digest 不得用于 public protection"
                )
        if self.content_hash is not None:
            validate_hash_token(self.content_hash, "ContextContribution.content_hash")
        if (self.content_hash is None) == (self.redacted_stable_digest is None):
            raise ItemSchemaError(
                "ContextContribution 必须恰好包含 content_hash 或 redacted_stable_digest"
            )
        if self.body is not None and self.redacted_stable_digest is not None:
            raise ItemSchemaError("带正文的 ContextContribution 不得使用 redacted digest")
        if self.contribution_ordinal is not None and (
            self.assembly_id is None
            or not isinstance(self.contribution_ordinal, int)
            or isinstance(self.contribution_ordinal, bool)
            or self.contribution_ordinal < 0
        ):
            raise ItemSchemaError(
                "contribution_ordinal 只能是 assembly scope 内的非负整数"
            )
        if self.source_ordinal is not None and (
            not isinstance(self.source_ordinal, int)
            or isinstance(self.source_ordinal, bool)
            or self.source_ordinal < 0
        ):
            raise ItemSchemaError(
                "ContextContribution.source_ordinal 只能是 registry 分配的非负整数"
            )
        _ensure_json_value(self.metadata, "ContextContribution.metadata")


def resolve_contribution_for_ref(
    ref: ContextRef,
    contributions: Sequence[ContextContribution],
) -> ContextContribution | None:
    """解析一个 request-only ref 的唯一 contribution binding。

    普通 request-only ref 以显式 ``contribution_id == ref_id`` 为首选，
    ``source_ref`` 只用于有明确 provenance 的 source alias。overlay 则必须
    同时匹配 role、source ref 和 source overlay epoch；不能把不同 epoch 的
    历史 contribution 通过一个源 item id 合并。任何多重或缺失的 active
    binding 都在这里 fail-closed，调用方不应自行实现第二套匹配规则。
    """
    if ref.ref_type != "request_only":
        return None

    if ref.base_delta_role in {BaseDeltaRole.BASE.value, BaseDeltaRole.DELTA.value}:
        role = ref.base_delta_role
        matches = tuple(
            contribution
            for contribution in contributions
            if contribution.contribution_kind == f"overlay_{role}"
            and contribution.metadata.get("overlay_ref") == ref.ref_id
            and contribution.metadata.get("overlay_role") == role
            and contribution.metadata.get("source_overlay_epoch")
            == ref.source_overlay_epoch
        )
        if len(matches) > 1:
            raise ValueError(
                "plan-order-integrity: ref 映射到多个 contribution: "
                f"{ref.ref_id}"
            )
        if not matches:
            if ref.availability == "available":
                raise ValueError(
                    "plan-order-integrity: overlay ref 缺少唯一 contribution: "
                    f"{ref.ref_id}"
                )
            return None
        return matches[0]

    candidates_by_id = {
        contribution.contribution_id: contribution
        for contribution in contributions
        if contribution.contribution_id == ref.ref_id
        or (
            ref.source_ref is not None
            and contribution.metadata.get("source_ref") == ref.source_ref
        )
    }
    if len(candidates_by_id) > 1:
        raise ValueError(
            "plan-order-integrity: ref 映射到多个 contribution: "
            f"{ref.ref_id}"
        )
    contribution = next(iter(candidates_by_id.values()), None)
    if contribution is not None and contribution.contribution_kind in {
        "overlay_base",
        "overlay_delta",
    }:
        raise ValueError(
            "plan-order-integrity: overlay contribution 必须绑定 overlay selection: "
            f"{ref.ref_id}"
        )
    return contribution


def validate_included_overlay_chain(
    selection: Sequence[ContextSelectionEntry],
    contributions: Sequence[ContextContribution],
) -> None:
    """只用 included manifest 的显式 overlay identity/revision 校验顺序。"""
    registry = {item.contribution_id: item for item in contributions}
    revisions: dict[tuple[str, int], str] = {}
    for entry in selection:
        if not entry.included or entry.base_delta_role == "none":
            continue
        contribution = registry.get(entry.contribution_id)
        if contribution is None:
            raise ItemSchemaError("plan-order-integrity: included overlay 缺少 contribution")
        overlay_id = _non_empty_string(
            contribution.metadata.get("overlay_id"), "overlay chain metadata.overlay_id"
        )
        key = (overlay_id, entry.source_overlay_epoch)
        if entry.base_delta_role == "base":
            if key in revisions:
                raise ItemSchemaError("plan-order-integrity: overlay chain 重复 base")
            revisions[key] = entry.source_revision
        else:
            if revisions.get(key) != entry.overlay_from_revision:
                raise ItemSchemaError("plan-order-integrity: overlay delta 缺少 included base/chain")
            revisions[key] = entry.overlay_to_revision


@dataclass(frozen=True, slots=True)
class ContextRequestPlan:
    session_id: str
    plan_id: str
    refs: tuple[ContextRef, ...]
    contributions: tuple[ContextContribution, ...] = ()
    tool_snapshot: tuple[Mapping[str, object], ...] = ()
    history_view_revision: int = 0
    source_overlay_epoch: int = 0
    compiler_version: str = "itemized-context-v1"
    format_version: int = 2
    active_view_id: str | None = None
    selection_policy: str = "active_view"
    plan_state: str = "unsealed"
    assembly_id: str | None = None
    tool_set_refs: tuple[ToolSetRef, ...] = ()
    selection: tuple[ContextSelectionEntry, ...] = ()
    plan_creation_idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.session_id, "ContextRequestPlan.session_id")
        _non_empty_string(self.plan_id, "plan_id")
        if type(self.format_version) is not int or self.format_version != 2:
            raise ItemSchemaError("ContextRequestPlan.format_version 必须为 2")
        for name, value in (
            ("history_view_revision", self.history_view_revision),
            ("source_overlay_epoch", self.source_overlay_epoch),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ItemSchemaError(
                    f"ContextRequestPlan.{name} 必须是非负整数"
                )
        if not self.compiler_version:
            raise ItemSchemaError("ContextRequestPlan.compiler_version 不能为空")
        if self.active_view_id is not None:
            _non_empty_string(self.active_view_id, "ContextRequestPlan.active_view_id")
        _non_empty_string(self.selection_policy, "ContextRequestPlan.selection_policy")
        if not isinstance(self.plan_state, str) or self.plan_state not in {
            "unsealed",
            "sealed",
        }:
            raise ItemSchemaError(f"未知 ContextRequestPlan.plan_state: {self.plan_state}")
        if self.assembly_id is not None:
            _non_empty_string(self.assembly_id, "ContextRequestPlan.assembly_id")
        if self.plan_state == "unsealed" and (self.assembly_id is not None or self.selection):
            raise ItemSchemaError("unsealed plan 不得带 assembly_id 或 selection")
        if self.plan_state == "sealed" and self.assembly_id is None:
            raise ItemSchemaError("sealed plan 必须带 assembly_id")
        if self.plan_creation_idempotency_key is not None:
            _non_empty_string(
                self.plan_creation_idempotency_key,
                "ContextRequestPlan.plan_creation_idempotency_key",
            )
        for ref in self.refs:
            if not isinstance(ref, ContextRef):
                raise ItemSchemaError("ContextRequestPlan.refs 元素非法")
            if ref.session_id != self.session_id:
                raise ItemSchemaError("source-mismatch: ContextRef 不属于当前 session")
            if ref.ref_type == "request_only" and ref.plan_id != self.plan_id:
                raise ItemSchemaError("source-mismatch: ContextRef 不属于当前 plan")
        unique_ref_identities(self.refs)
        ref_ids = {(ref.ref_type, ref.ref_id) for ref in self.refs}
        for contribution in self.contributions:
            if not isinstance(contribution, ContextContribution):
                raise ItemSchemaError("ContextRequestPlan.contributions 元素非法")
            if self.plan_state == "unsealed" and (
                contribution.assembly_id is not None
                or contribution.contribution_ordinal is not None
            ):
                raise ItemSchemaError(
                    "unsealed plan 的 contribution 不得带 assembly binding"
                )
            # source_ordinal 只能由 itemized registry 分配；metadata 中
            # 的同名历史键不再拥有任何解释权（不做兼容读取）。
            if self.plan_state == "unsealed" and (
                not isinstance(contribution.source_ordinal, int)
                or isinstance(contribution.source_ordinal, bool)
                or contribution.source_ordinal < 0
            ):
                raise ItemSchemaError(
                    "unsealed plan 的 contribution 必须带 registry 分配的 "
                    "typed source_ordinal"
                )
            if self.plan_state == "sealed" and (
                contribution.assembly_id != self.assembly_id
                or contribution.contribution_ordinal is None
            ):
                raise ItemSchemaError(
                    "sealed plan 的 contribution 必须绑定当前 assembly 和 ordinal"
                )
        contribution_ids_list = [
            contribution.contribution_id for contribution in self.contributions
        ]
        if len(contribution_ids_list) != len(set(contribution_ids_list)):
            raise ItemSchemaError("ContextRequestPlan.contributions 存在重复 identity")
        if self.plan_state == "sealed":
            contribution_ordinals = [
                int(contribution.contribution_ordinal)
                for contribution in self.contributions
            ]
            if contribution_ordinals != list(range(len(contribution_ordinals))):
                raise ItemSchemaError(
                    "sealed plan contribution_ordinal 必须从 0 连续递增"
                )
        tool_ids: set[str] = set()
        for ref in self.tool_set_refs:
            if not isinstance(ref, ToolSetRef):
                raise ItemSchemaError("ContextRequestPlan.tool_set_refs 元素非法")
            if ref.session_id != self.session_id:
                raise ItemSchemaError("source-mismatch: ToolSetRef 不属于当前 session")
            if ref.plan_id != self.plan_id:
                raise ItemSchemaError("ToolSetRef 必须属于当前 ContextRequestPlan")
            if self.plan_state == "unsealed" and ref.assembly_id is not None:
                raise ItemSchemaError("unsealed plan 的 ToolSetRef 不得绑定 assembly")
            if self.plan_state == "sealed" and ref.assembly_id != self.assembly_id:
                raise ItemSchemaError("sealed plan 的 ToolSetRef 必须绑定当前 assembly")
            if ref.ref_id in tool_ids:
                raise ItemSchemaError(f"ContextRequestPlan 重复 ToolSetRef: {ref.ref_id}")
            tool_ids.add(ref.ref_id)
            if not any(
                not entry.included and entry.ref == ref for entry in self.selection
            ):
                ref.validate_manifest()
        contribution_ids = set(contribution_ids_list)
        ordered_selection(self.selection)
        if any(entry.assembly_id != self.assembly_id for entry in self.selection):
            raise ItemSchemaError("selection entry 必须绑定当前 plan assembly")
        selected_keys: set[tuple[str, str]] = set()
        for entry in self.selection:
            if not isinstance(entry, ContextSelectionEntry):
                raise ItemSchemaError("ContextRequestPlan.selection 元素非法")
            ref = entry.ref
            if ref.session_id != self.session_id:
                raise ItemSchemaError("source-mismatch: selection ref 不属于当前 session")
            identity = (ref.ref_type, ref.ref_id)
            if identity in selected_keys:
                raise ItemSchemaError(f"selection 重复 ref: {identity}")
            selected_keys.add(identity)
            if isinstance(ref, ToolSetRef):
                registered_tool_ref = next(
                    (
                        candidate
                        for candidate in self.tool_set_refs
                        if candidate.ref_id == ref.ref_id
                    ),
                    None,
                )
                if ref.plan_id != self.plan_id or ref.assembly_id != self.assembly_id:
                    raise ItemSchemaError("selection ToolSetRef scope 不一致")
                if registered_tool_ref is None and entry.included:
                    raise ItemSchemaError("selection ToolSetRef 不在 plan registry")
                if registered_tool_ref is not None and registered_tool_ref != ref:
                    raise ItemSchemaError(
                        "plan-order-integrity: selection ToolSetRef manifest 不一致"
                    )
                if entry.contribution_id is not None or entry.detail_ref is not None:
                    raise ItemSchemaError(
                        "plan-order-integrity: tool_set selection 不得绑定 contribution/detail"
                    )
                continue
            if identity not in ref_ids:
                raise ItemSchemaError("selection ContextRef 不在 plan registry")
            registered_ref = next(
                (
                    candidate
                    for candidate in self.refs
                    if (candidate.ref_type, candidate.ref_id) == identity
                ),
                None,
            )
            if registered_ref != ref:
                raise ItemSchemaError(
                    "plan-order-integrity: selection ContextRef manifest 不一致"
                )
            resolved_contribution = resolve_contribution_for_ref(
                ref,
                self.contributions,
            ) if entry.included or entry.contribution_id in contribution_ids else None
            if resolved_contribution is not None:
                if entry.contribution_id != resolved_contribution.contribution_id:
                    if entry.included:
                        raise ItemSchemaError(
                            "plan-order-integrity: included contribution-backed "
                            "selection 必须绑定匹配的 contribution_id"
                        )
                    if entry.contribution_id is not None:
                        raise ItemSchemaError(
                            "plan-order-integrity: omitted selection contribution_id "
                            "与 ref provenance 不一致"
                        )
                elif entry.included and (
                    self.plan_state == "sealed"
                    and entry.contribution_ordinal
                    != resolved_contribution.contribution_ordinal
                ):
                    raise ItemSchemaError(
                        "plan-order-integrity: selection contribution_ordinal "
                        "与 contribution manifest 不一致"
                    )
            if entry.selection_kind in {
                SelectionKind.REQUEST_ONLY,
                SelectionKind.OVERLAY_BASE,
                SelectionKind.OVERLAY_DELTA,
            } and entry.contribution_id is not None:
                if (
                    entry.included or entry.contribution_ordinal is not None
                ) and entry.contribution_id not in contribution_ids:
                    raise ItemSchemaError(
                        "plan-order-integrity: selection contribution_id 不在 manifest"
                    )
                if entry.contribution_id not in contribution_ids:
                    # optional omitted entry 可以携带已经存在的 registry
                    # identity stub；它不分配 assembly contribution，也不读
                    # 正文。storage restore 会再用 session registry 校验该 ID。
                    continue
                contribution = next(
                    item
                    for item in self.contributions
                    if item.contribution_id == entry.contribution_id
                )
                if contribution.source_revision != ref.source_revision:
                    raise ItemSchemaError("contribution/ref source_revision 不一致")
                if contribution.content_length != ref.content_length:
                    raise ItemSchemaError("contribution/ref content_length 不一致")
                if contribution.content_hash != ref.content_hash or (
                    contribution.redacted_stable_digest
                    != ref.redacted_stable_digest
                ):
                    raise ItemSchemaError("contribution/ref hash token 不一致")
                if (
                    contribution.visibility != ref.visibility
                    or contribution.protection != ref.protection
                ):
                    raise ItemSchemaError(
                        "contribution/ref visibility/protection 不一致"
                    )
                if entry.included and (
                    entry.contribution_ordinal != contribution.contribution_ordinal
                ):
                    raise ItemSchemaError(
                        "plan-order-integrity: selection contribution_ordinal 不一致"
                    )

        validate_included_overlay_chain(self.selection, self.contributions)

    def seal_for_assembly(
        self,
        assembly_id: str,
        *,
        selection: Sequence[ContextSelectionEntry],
    ) -> ContextRequestPlan:
        """生成 assembly scope 内不可变的 sealed plan。"""
        if self.plan_state != "unsealed":
            if self.plan_state == "sealed" and self.assembly_id == assembly_id:
                if tuple(selection) != self.selection:
                    raise ItemSchemaError(
                        "assembly-idempotency-conflict: sealed plan 的 selection 不得改变"
                    )
                return self
            raise ItemSchemaError("ContextRequestPlan 只能从 unsealed seal 一次")
        for entry in selection:
            if not isinstance(entry.ref, ContextRef):
                continue
            # omission 不解析 active source；只有已知的既有 binding 才校验 provenance。
            if not entry.included and not any(
                item.contribution_id == entry.contribution_id
                for item in self.contributions
            ):
                continue
            resolved_contribution = resolve_contribution_for_ref(
                entry.ref,
                self.contributions,
            )
            if resolved_contribution is None:
                if entry.included and entry.contribution_id is not None:
                    raise ItemSchemaError(
                        "plan-order-integrity: included selection contribution_id "
                        "无法解析到 ref provenance"
                    )
                continue
            if entry.included and (
                entry.contribution_id != resolved_contribution.contribution_id
            ):
                raise ItemSchemaError(
                    "plan-order-integrity: included contribution-backed selection "
                    "必须绑定匹配的 contribution_id"
                )
            if (
                not entry.included
                and entry.contribution_id is not None
                and entry.contribution_id != resolved_contribution.contribution_id
            ):
                raise ItemSchemaError(
                    "plan-order-integrity: omitted selection contribution_id "
                    "与 ref provenance 不一致"
                )
        bound_tool_refs = tuple(
            ToolSetRef(
                session_id=tool_ref.session_id,
                ref_id=tool_ref.ref_id,
                plan_id=tool_ref.plan_id,
                source_revision=tool_ref.source_revision,
                tool_set_schema=tool_ref.tool_set_schema,
                tool_set_schema_version=tool_ref.tool_set_schema_version,
                tool_policy_version=tool_ref.tool_policy_version,
                content_length=tool_ref.content_length,
                content_hash=tool_ref.content_hash,
                redacted_stable_digest=tool_ref.redacted_stable_digest,
                protection=tool_ref.protection,
                availability=tool_ref.availability,
                tool_policy=tool_ref.tool_policy,
                tools=tool_ref.tools,
                assembly_id=assembly_id,
            )
            for tool_ref in self.tool_set_refs
        )
        selected_contribution_ids = {
            entry.contribution_id
            for entry in selection
            if entry.included and entry.contribution_id is not None
        }
        bound_contributions = tuple(
            ContextContribution(
                contribution_id=contribution.contribution_id,
                source_kind=contribution.source_kind,
                source_revision=contribution.source_revision,
                content_hash=contribution.content_hash,
                request_only=contribution.request_only,
                # source_ordinal 是 registry 表的独立排序列，不属于 sealed
                # contribution provenance metadata；assembly 的顺序由下面
                # 新分配的 contribution_ordinal 固化。
                metadata={
                    key: value
                    for key, value in contribution.metadata.items()
                    if key != "source_ordinal"
                },
                contribution_kind=contribution.contribution_kind,
                body=contribution.body,
                content_length=contribution.content_length,
                redacted_stable_digest=contribution.redacted_stable_digest,
                visibility=contribution.visibility,
                protection=contribution.protection,
                root_placement=contribution.root_placement,
                # replaceable_source 是 registry 作用域的替换策略：它只决定
                # 同一 owner slot 的未封存 registry 记录能否被新 revision 覆盖。
                # 已封存 contribution 永久不可变，该 flag 在 seal 后不再拥有任何
                # 决策权，因此与 source_ordinal 同理不进入 sealed 形态。
                replaceable_source=False,
                assembly_id=assembly_id,
                contribution_ordinal=ordinal,
            )
            for ordinal, contribution in enumerate(
                contribution
                for contribution in self.contributions
                if contribution.contribution_id in selected_contribution_ids
            )
        )
        return ContextRequestPlan(
            session_id=self.session_id,
            plan_id=self.plan_id,
            refs=self.refs,
            contributions=bound_contributions,
            tool_snapshot=self.tool_snapshot,
            history_view_revision=self.history_view_revision,
            source_overlay_epoch=self.source_overlay_epoch,
            compiler_version=self.compiler_version,
            format_version=self.format_version,
            active_view_id=self.active_view_id,
            selection_policy=self.selection_policy,
            plan_state="sealed",
            assembly_id=assembly_id,
            tool_set_refs=bound_tool_refs,
            selection=tuple(selection),
            plan_creation_idempotency_key=self.plan_creation_idempotency_key,
        )

    def plan_hash(self) -> str:
        return context_plan_hash(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "format_version": self.format_version,
            "plan_id": self.plan_id,
            "plan_state": self.plan_state,
            "assembly_id": self.assembly_id,
            "history_view_revision": self.history_view_revision,
            "source_overlay_epoch": self.source_overlay_epoch,
            "active_view_id": self.active_view_id,
            "selection_policy": self.selection_policy,
            "refs": [ref.to_dict() for ref in self.refs],
            "tool_set_refs": [ref.to_dict() for ref in self.tool_set_refs],
            "contributions": [
                {
                    "contribution_id": item.contribution_id,
                    "source_kind": item.source_kind,
                    "source_revision": item.source_revision,
                    "content_hash": item.content_hash,
                    "content_length": item.content_length,
                    "redacted_stable_digest": item.redacted_stable_digest,
                    "request_only": item.request_only,
                    "contribution_kind": item.contribution_kind,
                    "visibility": item.visibility,
                    "protection": item.protection,
                    "root_placement": item.root_placement,
                    "replaceable_source": item.replaceable_source,
                    "body": (
                        item.body
                        if item.body is not None
                        and item.protection == DetailProtection.PUBLIC
                        else None
                    ),
                    "assembly_id": item.assembly_id,
                    "contribution_ordinal": item.contribution_ordinal,
                    "source_ordinal": item.source_ordinal,
                    "metadata": dict(item.metadata),
                }
                for item in self.contributions
            ],
            "selection": [entry.to_dict() for entry in self.selection],
            "compiler_version": self.compiler_version,
            "plan_creation_idempotency_key": self.plan_creation_idempotency_key,
            "plan_hash": self.plan_hash(),
        }
