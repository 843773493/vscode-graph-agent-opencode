"""请求计划的 owner 校验、封存绑定与规范哈希。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.itemized.enums import (
    ReplacementPolicy,
    SelectionKind,
    SelectionRole,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.plan_hash import context_plan_hash
from app.domain.itemized.hashing import (
    copy_json_value,
)
from app.domain.itemized.identity.refs import (
    ContextRef,
    ToolSetIdentityRef,
    ToolSetRef,
    ref_identity,
    selection_ref_identity,
)
from app.domain.itemized.plans.contributions import (
    ContextContribution,
    resolve_contribution_for_ref,
    validate_included_overlay_chain,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import ordered_selection
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class ContextRequestPlan:
    session_id: str
    thread_id: str
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

    def __getattribute__(self, name: str) -> object:
        value = object.__getattribute__(self, name)
        return copy_json_value(value) if name == "tool_snapshot" else value

    def __post_init__(self) -> None:
        for name in ("refs", "contributions", "tool_set_refs", "selection"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        tools = object.__getattribute__(self, "tool_snapshot")
        if any(not isinstance(tool, Mapping) for tool in tools):
            raise ItemSchemaError("ContextRequestPlan.tool_snapshot 元素必须是 object")
        object.__setattr__(self, "tool_snapshot", copy_json_value(tuple(tools)))
        _non_empty_string(self.session_id, "ContextRequestPlan.session_id")
        _non_empty_string(self.thread_id, "ContextRequestPlan.thread_id")
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
            if ref.ref_type == "request_only" and ref.thread_id != self.thread_id:
                raise ItemSchemaError("source-mismatch: request-only ContextRef 不属于当前 thread")
        # 复用thread-qualified identity建selection查找表并完成唯一性校验。
        # sibling thread 的同名ref不能互相覆盖。
        ref_registry: dict[tuple[str, ...], ContextRef] = {}
        for ref in self.refs:
            identity = ref_identity(ref)
            if identity in ref_registry:
                raise ItemSchemaError(
                    "context ref registry 存在重复 ref_type/ref_id/thread_id"
                )
            ref_registry[identity] = ref
        for tool_ref in self.tool_set_refs:
            if not isinstance(tool_ref, ToolSetRef):
                raise ItemSchemaError("ContextRequestPlan.tool_set_refs 元素非法")
            if (tool_ref.session_id, tool_ref.thread_id, tool_ref.plan_id) != (
                self.session_id,
                self.thread_id,
                self.plan_id,
            ):
                raise ItemSchemaError("source-mismatch: ToolSetRef 不属于当前 session/thread/plan")
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
        contribution_registry: dict[str, ContextContribution] = {}
        for contribution in self.contributions:
            contribution_id = contribution.contribution_id
            if contribution_id in contribution_registry:
                raise ItemSchemaError("ContextRequestPlan.contributions 存在重复 identity")
            contribution_registry[contribution_id] = contribution
        if self.plan_state == "sealed":
            contribution_ordinals = [
                int(contribution.contribution_ordinal)
                for contribution in self.contributions
            ]
            if contribution_ordinals != list(range(len(contribution_ordinals))):
                raise ItemSchemaError(
                    "sealed plan contribution_ordinal 必须从 0 连续递增"
                )
        tool_registry: dict[str, ToolSetRef] = {}
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
            if ref.ref_id in tool_registry:
                raise ItemSchemaError(f"ContextRequestPlan 重复 ToolSetRef: {ref.ref_id}")
            tool_registry[ref.ref_id] = ref
            if not any(
                not entry.included and entry.ref == ref.identity_ref()
                for entry in self.selection
            ):
                ref.validate_manifest()
        ordered_selection(self.selection)
        if any(entry.assembly_id != self.assembly_id for entry in self.selection):
            raise ItemSchemaError("selection entry 必须绑定当前 plan assembly")
        for entry in self.selection:
            if not isinstance(entry, ContextSelectionEntry):
                raise ItemSchemaError("ContextRequestPlan.selection 元素非法")
            ref = entry.ref
            if ref.session_id != self.session_id:
                raise ItemSchemaError("source-mismatch: selection ref 不属于当前 session")
            identity = selection_ref_identity(ref)
            if isinstance(ref, ToolSetIdentityRef):
                registered_tool_ref = tool_registry.get(ref.ref_id)
                if ref.thread_id != self.thread_id or ref.plan_id != self.plan_id or ref.assembly_id != self.assembly_id:
                    raise ItemSchemaError("selection ToolSetRef scope 不一致")
                if registered_tool_ref is None and entry.included:
                    raise ItemSchemaError("selection ToolSetRef 不在 plan registry")
                if registered_tool_ref is not None:
                    expected_ref = registered_tool_ref if entry.included else registered_tool_ref.identity_ref()
                    if expected_ref != ref:
                        raise ItemSchemaError(
                            "plan-order-integrity: selection ToolSetRef manifest 不一致"
                        )
                if entry.contribution_id is not None or entry.detail_ref is not None:
                    raise ItemSchemaError(
                        "plan-order-integrity: tool_set selection 不得绑定 contribution/detail"
                    )
                continue
            if identity not in ref_registry:
                raise ItemSchemaError("selection ContextRef 不在 plan registry")
            if ref_registry[identity] != ref:
                raise ItemSchemaError(
                    "plan-order-integrity: selection ContextRef manifest 不一致"
                )
            resolved_contribution = resolve_contribution_for_ref(
                ref,
                self.contributions,
            ) if entry.included or entry.contribution_id in contribution_registry else None
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
                ) and entry.contribution_id not in contribution_registry:
                    raise ItemSchemaError(
                        "plan-order-integrity: selection contribution_id 不在 manifest"
                    )
                if entry.contribution_id not in contribution_registry:
                    # optional omitted entry 可以携带已经存在的 registry
                    # identity stub；它不分配 assembly contribution，也不读
                    # 正文。storage restore 会再用 session registry 校验该 ID。
                    continue
                contribution = contribution_registry[entry.contribution_id]
                if contribution.source_revision != ref.source_revision:
                    raise ItemSchemaError("contribution/ref source_revision 不一致")
                if contribution.content_length != ref.content_length:
                    raise ItemSchemaError("contribution/ref content_length 不一致")
                if contribution.content_hash != ref.content_hash:
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
                thread_id=tool_ref.thread_id,
                ref_id=tool_ref.ref_id,
                plan_id=tool_ref.plan_id,
                source_revision=tool_ref.source_revision,
                tool_set_schema=tool_ref.tool_set_schema,
                tool_set_schema_version=tool_ref.tool_set_schema_version,
                tool_policy_version=tool_ref.tool_policy_version,
                content_length=tool_ref.content_length,
                content_hash=tool_ref.content_hash,
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
                extensions=contribution.extensions,
                source_binding=contribution.source_binding,
                contribution_kind=contribution.contribution_kind,
                body=contribution.body,
                content_length=contribution.content_length,
                visibility=contribution.visibility,
                protection=contribution.protection,
                root_placement=contribution.root_placement,
                # selection_role/replacement_policy 是 registry 作用域的选择与
                # 替换策略：它们只决定独立候选资格和同一 owner slot 的未封存
                # registry 记录能否被新 revision 覆盖。已封存 contribution 永久
                # 不可变，两个字段在 seal 后不再拥有任何决策权，因此与
                # source_ordinal 同理不进入 sealed 形态。
                selection_role=SelectionRole.DIRECT,
                replacement_policy=ReplacementPolicy.IMMUTABLE,
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
            thread_id=self.thread_id,
            plan_id=self.plan_id,
            refs=self.refs,
            contributions=bound_contributions,
            tool_snapshot=object.__getattribute__(self, "tool_snapshot"),
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

    def to_dict(self, *, include_plan_hash: bool = True) -> dict[str, object]:
        result = {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
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
                item.to_dict()
                for item in self.contributions
            ],
            "selection": [entry.to_dict() for entry in self.selection],
            "compiler_version": self.compiler_version,
            "plan_creation_idempotency_key": self.plan_creation_idempotency_key,
        }
        if include_plan_hash:
            result["plan_hash"] = self.plan_hash()
        return result
