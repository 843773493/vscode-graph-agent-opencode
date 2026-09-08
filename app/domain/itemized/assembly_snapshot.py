"""v2 sealed ContextAssemblySnapshot 与 provider request hash。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.itemized.enums import (
    DetailProtection,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    _ensure_json_value,
)
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    resolve_contribution_for_ref,
    validate_included_overlay_chain,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.assembly import (
    context_assembly_snapshot_from_dict,
)
from app.domain.itemized.serialization import _non_empty_string, normalize_wire_request


@dataclass(frozen=True, slots=True)
class ContextAssemblySnapshot:
    assembly_id: str
    session_id: str
    turn_id: str
    execution_id: str
    plan_id: str
    plan_hash: str
    request_hash: str
    history_view_revision: int
    source_overlay_epoch: int
    refs: tuple[ContextRef, ...]
    contributions: tuple[ContextContribution, ...]
    tool_snapshot: tuple[Mapping[str, object], ...]
    compiler_version: str
    provider_version: str
    active_view_id: str | None = None
    selection_policy: str = "active_view"
    model_call_id: str | None = None
    hash_algorithm: str = "sha256:jcs:v1"
    loss: tuple[str, ...] = ()
    sealed: bool = False
    tool_set_refs: tuple[ToolSetRef, ...] = ()
    selection: tuple[ContextSelectionEntry, ...] = ()
    plan_state: str = "sealed"
    projector_id: str = "itemized-context-provider"
    projector_version: str = "v1"
    target_format: str = "unknown"
    request_hash_preimage: object | None = None

    def __post_init__(self) -> None:
        for name in (
            "assembly_id",
            "session_id",
            "turn_id",
            "execution_id",
            "plan_id",
            "plan_hash",
            "request_hash",
            "compiler_version",
            "provider_version",
        ):
            _non_empty_string(getattr(self, name), f"ContextAssemblySnapshot.{name}")
        if self.active_view_id is not None:
            _non_empty_string(
                self.active_view_id, "ContextAssemblySnapshot.active_view_id"
            )
        _non_empty_string(
            self.selection_policy, "ContextAssemblySnapshot.selection_policy"
        )
        _non_empty_string(self.projector_id, "ContextAssemblySnapshot.projector_id")
        _non_empty_string(
            self.projector_version, "ContextAssemblySnapshot.projector_version"
        )
        _non_empty_string(self.target_format, "ContextAssemblySnapshot.target_format")
        if self.request_hash_preimage is not None:
            _ensure_json_value(
                self.request_hash_preimage,
                "ContextAssemblySnapshot.request_hash_preimage",
            )
            if normalize_wire_request(self.request_hash_preimage) != (
                self.request_hash_preimage
            ):
                raise ItemSchemaError(
                    "ContextAssemblySnapshot.request_hash_preimage 必须已规范化，"
                    "不得包含认证、传输或易变字段"
                )
        if self.model_call_id is not None:
            _non_empty_string(
                self.model_call_id, "ContextAssemblySnapshot.model_call_id"
            )
        if self.hash_algorithm != "sha256:jcs:v1":
            raise ItemSchemaError(
                "ContextAssemblySnapshot 只支持 sha256:jcs:v1 hash algorithm"
            )
        for name, value in (
            ("history_view_revision", self.history_view_revision),
            ("source_overlay_epoch", self.source_overlay_epoch),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ItemSchemaError(f"ContextAssemblySnapshot.{name} 必须是非负整数")
        if not isinstance(self.sealed, bool):
            raise ItemSchemaError("ContextAssemblySnapshot.sealed 必须是 boolean")
        if self.plan_state != "sealed":
            raise ItemSchemaError("ContextAssemblySnapshot.plan_state 必须为 sealed")
        if not self.sealed:
            raise ItemSchemaError(
                "ContextAssemblySnapshot 只能以 sealed 形态构造和恢复"
            )
        for name, value in (
            ("refs", self.refs),
            ("contributions", self.contributions),
            ("tool_snapshot", self.tool_snapshot),
            ("tool_set_refs", self.tool_set_refs),
            ("selection", self.selection),
        ):
            if not isinstance(value, tuple):
                raise ItemSchemaError(f"ContextAssemblySnapshot.{name} 必须是 tuple")
        if not isinstance(self.loss, tuple) or not all(
            isinstance(value, str) and value for value in self.loss
        ):
            raise ItemSchemaError("ContextAssemblySnapshot.loss 必须是非空字符串 tuple")
        if not all(isinstance(tool, Mapping) for tool in self.tool_snapshot):
            raise ItemSchemaError(
                "ContextAssemblySnapshot.tool_snapshot 元素必须是 object"
            )
        _ensure_json_value(
            self.tool_snapshot,
            "ContextAssemblySnapshot.tool_snapshot",
        )
        for ref in self.refs:
            if not isinstance(ref, ContextRef):
                raise ItemSchemaError("ContextAssemblySnapshot.refs 元素非法")
            if ref.session_id != self.session_id or (
                ref.ref_type == "request_only" and ref.plan_id != self.plan_id
            ):
                raise ItemSchemaError("source-mismatch: ContextRef 与 assembly owner 不一致")
        for contribution in self.contributions:
            if not isinstance(contribution, ContextContribution):
                raise ItemSchemaError("ContextAssemblySnapshot.contributions 元素非法")
            if contribution.contribution_ordinal is None:
                raise ItemSchemaError(
                    "sealed ContextContribution 必须绑定 assembly contribution_ordinal"
                )
            if contribution.assembly_id != self.assembly_id:
                raise ItemSchemaError("ContextContribution 不属于当前 assembly")
        contribution_ordinals = [
            int(contribution.contribution_ordinal)
            for contribution in self.contributions
            if contribution.contribution_ordinal is not None
        ]
        if contribution_ordinals != list(range(len(self.contributions))):
            raise ItemSchemaError(
                "assembly contribution_ordinal 必须唯一且从 0 连续递增"
            )
        for tool_ref in self.tool_set_refs:
            if not isinstance(tool_ref, ToolSetRef):
                raise ItemSchemaError("ContextAssemblySnapshot.tool_set_refs 元素非法")
            if (
                tool_ref.plan_id != self.plan_id
                or tool_ref.session_id != self.session_id
                or tool_ref.assembly_id != self.assembly_id
            ):
                raise ItemSchemaError("ToolSetRef 与 assembly/plan 不一致")
            if not any(
                not entry.included and entry.ref == tool_ref for entry in self.selection
            ):
                tool_ref.validate_manifest()
        for entry in self.selection:
            if not isinstance(entry, ContextSelectionEntry):
                raise ItemSchemaError("ContextAssemblySnapshot.selection 元素非法")
            if entry.assembly_id != self.assembly_id:
                raise ItemSchemaError("selection entry 不属于当前 assembly")
        ordinals = [entry.plan_ordinal for entry in self.selection]
        if ordinals != list(range(len(ordinals))):
            raise ItemSchemaError("assembly selection.plan_ordinal 必须连续")
        ref_registry = {(ref.ref_type, ref.ref_id): ref for ref in self.refs}
        tool_registry = {ref.ref_id: ref for ref in self.tool_set_refs}
        contribution_registry = {
            item.contribution_id: item for item in self.contributions
        }
        if len(ref_registry) != len(self.refs):
            raise ItemSchemaError("assembly refs 存在重复 identity")
        if len(tool_registry) != len(self.tool_set_refs):
            raise ItemSchemaError("assembly ToolSetRef 存在重复 identity")
        for entry in self.selection:
            if isinstance(entry.ref, ToolSetRef):
                if (
                    entry.ref.session_id != self.session_id
                    or entry.ref.plan_id != self.plan_id
                    or entry.ref.assembly_id != self.assembly_id
                ):
                    raise ItemSchemaError("selection ToolSetRef scope 不一致")
                registered_tool = tool_registry.get(entry.ref.ref_id)
                if (entry.included or registered_tool is not None) and registered_tool != entry.ref:
                    raise ItemSchemaError("selection ToolSetRef manifest 不一致")
                continue
            if ref_registry.get((entry.ref.ref_type, entry.ref.ref_id)) != entry.ref:
                raise ItemSchemaError("selection ContextRef manifest 不一致")
            resolved_contribution = resolve_contribution_for_ref(
                entry.ref,
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
                    entry.contribution_ordinal
                    != resolved_contribution.contribution_ordinal
                ):
                    raise ItemSchemaError(
                        "plan-order-integrity: selection contribution_ordinal "
                        "与 contribution manifest 不一致"
                    )
            elif entry.included and entry.contribution_id is not None:
                raise ItemSchemaError(
                    "plan-order-integrity: included selection contribution_id "
                    "无法解析到 ref provenance"
                )
            if entry.contribution_id is not None:
                contribution = contribution_registry.get(entry.contribution_id)
                if contribution is None:
                    if entry.included or entry.contribution_ordinal is not None:
                        raise ItemSchemaError(
                            "plan-order-integrity: selection contribution manifest 缺失"
                        )
                    # optional omitted request-only/overlay 可以只保留已经
                    # 存在的 contribution identity stub；它不能让 snapshot
                    # 新建 assembly ordinal 或携带正文。
                    continue
                if entry.included and (
                    contribution.source_revision != entry.source_revision
                    or contribution.content_length != entry.content_length
                    or contribution.content_hash != entry.content_hash
                    or contribution.redacted_stable_digest
                    != entry.redacted_stable_digest
                    or contribution.visibility != entry.visibility
                    or contribution.protection != entry.protection
                    or contribution.contribution_ordinal != entry.contribution_ordinal
                ):
                    raise ItemSchemaError("selection contribution manifest 不一致")
                if not entry.included:
                    known_manifest = (
                        entry.source_revision,
                        entry.content_length,
                        entry.content_hash,
                        entry.redacted_stable_digest,
                    )
                    actual_manifest = (
                        contribution.source_revision,
                        contribution.content_length,
                        contribution.content_hash,
                        contribution.redacted_stable_digest,
                    )
                    if any(
                        expected is not None and expected != actual
                        for expected, actual in zip(
                            known_manifest,
                            actual_manifest,
                            strict=True,
                        )
                    ):
                        raise ItemSchemaError(
                            "omitted selection contribution manifest 不一致"
                        )

        validate_included_overlay_chain(self.selection, self.contributions)

    def seal(self) -> ContextAssemblySnapshot:
        if self.sealed:
            return self
        return ContextAssemblySnapshot(
            assembly_id=self.assembly_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            execution_id=self.execution_id,
            plan_id=self.plan_id,
            plan_hash=self.plan_hash,
            request_hash=self.request_hash,
            history_view_revision=self.history_view_revision,
            source_overlay_epoch=self.source_overlay_epoch,
            refs=self.refs,
            contributions=self.contributions,
            tool_snapshot=self.tool_snapshot,
            compiler_version=self.compiler_version,
            provider_version=self.provider_version,
            active_view_id=self.active_view_id,
            selection_policy=self.selection_policy,
            model_call_id=self.model_call_id,
            hash_algorithm=self.hash_algorithm,
            loss=self.loss,
            sealed=True,
            tool_set_refs=self.tool_set_refs,
            selection=self.selection,
            plan_state="sealed",
            projector_id=self.projector_id,
            projector_version=self.projector_version,
            target_format=self.target_format,
            request_hash_preimage=self.request_hash_preimage,
        )

    def as_sealed_plan(self) -> ContextRequestPlan:
        """将 sealed snapshot 还原为同一份不可变请求计划。"""
        return ContextRequestPlan(
            session_id=self.session_id,
            plan_id=self.plan_id,
            refs=self.refs,
            contributions=self.contributions,
            tool_snapshot=self.tool_snapshot,
            history_view_revision=self.history_view_revision,
            source_overlay_epoch=self.source_overlay_epoch,
            compiler_version=self.compiler_version,
            format_version=2,
            active_view_id=self.active_view_id,
            selection_policy=self.selection_policy,
            plan_state="sealed",
            assembly_id=self.assembly_id,
            tool_set_refs=self.tool_set_refs,
            selection=self.selection,
        )

    def validate_hashes(self) -> None:
        """重算 sealed plan/request fingerprint，拒绝篡改或半重写 snapshot。"""
        plan = self.as_sealed_plan()
        expected_plan_hash = plan.plan_hash()
        if self.plan_hash != expected_plan_hash:
            raise ItemSchemaError(
                "context assembly plan_hash 与 sealed selection/manifest 不一致"
            )
        expected_request_hash = context_request_hash(
            plan,
            self.provider_version,
            projector_id=self.projector_id,
            projector_version=self.projector_version,
            target_format=self.target_format,
            wire_request=self.request_hash_preimage,
        )
        if self.request_hash != expected_request_hash:
            raise ItemSchemaError(
                "context assembly request_hash 与 sealed selection/manifest 不一致"
            )

    def to_dict(self) -> dict[str, object]:
        """以规范 envelope 序列化，避免 dataclasses.asdict 泄漏旧别名。"""
        # ToolSetRef 是工具定义的唯一 sealed registry。旧的
        # ``tool_snapshot`` 字段仅保留兼容投影，并且只能从已绑定的 public
        # manifest 派生；不能把未选中的候选工具或受保护 manifest 的原文
        # 随 assembly snapshot 写入 SQLite。
        selected_tool_ref_ids = {
            entry.ref.ref_id
            for entry in self.selection
            if entry.included and isinstance(entry.ref, ToolSetRef)
        }
        persisted_tool_refs = tuple(
            sorted(
                (
                    tool_ref
                    for tool_ref in self.tool_set_refs
                    if tool_ref.ref_id in selected_tool_ref_ids
                ),
                key=lambda ref: ref.ref_id,
            )
        )
        persisted_tool_snapshot = [
            dict(tool)
            for tool_ref in persisted_tool_refs
            if tool_ref.protection == DetailProtection.PUBLIC
            for tool in tool_ref.tools
        ]
        return {
            "format_version": 2,
            "assembly_id": self.assembly_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "request_hash": self.request_hash,
            "history_view_revision": self.history_view_revision,
            "source_overlay_epoch": self.source_overlay_epoch,
            "refs": [ref.to_dict() for ref in self.refs],
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
                    "body": (
                        item.body
                        if item.body is not None
                        and item.protection == DetailProtection.PUBLIC
                        else None
                    ),
                    "assembly_id": item.assembly_id,
                    "contribution_ordinal": item.contribution_ordinal,
                    "metadata": dict(item.metadata),
                }
                for item in self.contributions
            ],
            "tool_snapshot": persisted_tool_snapshot,
            "tool_set_refs": [ref.to_dict() for ref in persisted_tool_refs],
            "selection": [entry.to_dict() for entry in self.selection],
            "compiler_version": self.compiler_version,
            "provider_version": self.provider_version,
            "projector_id": self.projector_id,
            "projector_version": self.projector_version,
            "target_format": self.target_format,
            "request_hash_preimage": self.request_hash_preimage,
            "active_view_id": self.active_view_id,
            "selection_policy": self.selection_policy,
            "model_call_id": self.model_call_id,
            "hash_algorithm": self.hash_algorithm,
            "loss": list(self.loss),
            "sealed": self.sealed,
            "plan_state": self.plan_state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ContextAssemblySnapshot:
        """从 SQLite snapshot_json 恢复不可变 assembly 视图。"""
        return context_assembly_snapshot_from_dict(cls, value)
