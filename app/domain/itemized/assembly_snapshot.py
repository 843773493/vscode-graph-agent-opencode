"""v2 sealed ContextAssemblySnapshot 与 provider request hash。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.epoch.tool_set import tool_set_manifest_compatibility_key
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.request_hash import _context_request_hash_preimage
from app.domain.itemized.hashing import (
    _ensure_json_value,
    canonical_json_bytes,
    copy_json_value,
    sha256_jcs,
    validate_hash_token,
)
from app.domain.itemized.identity.refs import (
    ContextRef,
    ToolSetIdentityRef,
    ToolSetRef,
    selection_ref_identity,
)
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
from app.domain.itemized.serde.epoch import context_seal_binding_to_dict
from app.domain.itemized.serialization import normalize_wire_request
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class ContextAssemblySnapshot:
    """sealed 组装快照保留原 seal 的 owner 与历史坐标。

    ``session_id``/``thread_id`` 与 ``history_view_revision``/``source_overlay_epoch`` 都描述来源 seal；
    fork 后这些坐标不会改写为 target 坐标，也不代表目标 storage owner/view。
    ``source_overlay_epoch`` 不是内容项的 ``source_revision``，也不等于任一 ``contribution.source_revision``。
    """

    assembly_id: str
    session_id: str
    thread_id: str
    turn_id: str | None
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
    # prepare 冻结输入身份：只属于 seal 提交记录，不参与 request_hash。
    request_input_hash: str | None = None
    seal_binding: ContextSealBinding | None = None

    def __getattribute__(self, name: str) -> object:
        value = object.__getattribute__(self, name)
        return copy_json_value(value) if name in {"tool_snapshot", "request_hash_preimage"} else value

    def __post_init__(self) -> None:
        tools = object.__getattribute__(self, "tool_snapshot")
        tools = copy_json_value(tools)
        request_hash_preimage = copy_json_value(
            object.__getattribute__(self, "request_hash_preimage")
        )
        object.__setattr__(self, "tool_snapshot", tools)
        object.__setattr__(self, "request_hash_preimage", request_hash_preimage)
        # 构造内校验共用本次已隔离的 JSON；对外 getter 仍逐次返回副本。
        for name in (
            "assembly_id",
            "session_id",
            "thread_id",
            "execution_id",
            "plan_id",
            "plan_hash",
            "request_hash",
            "compiler_version",
            "provider_version",
        ):
            _non_empty_string(getattr(self, name), f"ContextAssemblySnapshot.{name}")
        if self.turn_id is not None:
            _non_empty_string(self.turn_id, "ContextAssemblySnapshot.turn_id")
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
        if request_hash_preimage is not None:
            _ensure_json_value(
                request_hash_preimage,
                "ContextAssemblySnapshot.request_hash_preimage",
            )
            if normalize_wire_request(request_hash_preimage) != request_hash_preimage:
                raise ItemSchemaError(
                    "ContextAssemblySnapshot.request_hash_preimage 必须已规范化，"
                    "不得包含认证、传输或易变字段"
                )
        if self.request_input_hash is not None:
            validate_hash_token(
                self.request_input_hash,
                "ContextAssemblySnapshot.request_input_hash",
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
            ("tool_snapshot", tools),
            ("tool_set_refs", self.tool_set_refs),
            ("selection", self.selection),
        ):
            if not isinstance(value, tuple):
                raise ItemSchemaError(f"ContextAssemblySnapshot.{name} 必须是 tuple")
        if not isinstance(self.loss, tuple) or not all(
            isinstance(value, str) and value for value in self.loss
        ):
            raise ItemSchemaError("ContextAssemblySnapshot.loss 必须是非空字符串 tuple")
        if not all(isinstance(tool, Mapping) for tool in tools):
            raise ItemSchemaError(
                "ContextAssemblySnapshot.tool_snapshot 元素必须是 object"
            )
        _ensure_json_value(
            tools,
            "ContextAssemblySnapshot.tool_snapshot",
        )
        for ref in self.refs:
            if not isinstance(ref, ContextRef):
                raise ItemSchemaError("ContextAssemblySnapshot.refs 元素非法")
            if ref.session_id != self.session_id or (
                ref.ref_type == "request_only" and ref.plan_id != self.plan_id
            ):
                raise ItemSchemaError("source-mismatch: ContextRef 与 assembly owner 不一致")
            if ref.ref_type == "request_only" and ref.thread_id != self.thread_id:
                raise ItemSchemaError("source-mismatch: request-only ContextRef 与 assembly thread 不一致")
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
                or tool_ref.thread_id != self.thread_id
                or tool_ref.assembly_id != self.assembly_id
            ):
                raise ItemSchemaError("ToolSetRef 与 assembly/plan 不一致")
            if not any(
                not entry.included and entry.ref == tool_ref.identity_ref()
                for entry in self.selection
            ):
                tool_ref.validate_manifest()
        for entry in self.selection:
            if not isinstance(entry, ContextSelectionEntry):
                raise ItemSchemaError("ContextAssemblySnapshot.selection 元素非法")
            if entry.assembly_id != self.assembly_id:
                raise ItemSchemaError("selection entry 不属于当前 assembly")
            if entry.detail_ref is not None:
                entry.detail_ref.require_owner(
                    self.session_id, self.thread_id, self.assembly_id
                )
        ordinals = [entry.plan_ordinal for entry in self.selection]
        if ordinals != list(range(len(ordinals))):
            raise ItemSchemaError("assembly selection.plan_ordinal 必须连续")
        ref_registry = {selection_ref_identity(ref): ref for ref in self.refs}
        tool_registry = {ref.ref_id: ref for ref in self.tool_set_refs}
        contribution_registry = {
            item.contribution_id: item for item in self.contributions
        }
        if len(ref_registry) != len(self.refs):
            raise ItemSchemaError("assembly refs 存在重复 identity")
        if len(tool_registry) != len(self.tool_set_refs):
            raise ItemSchemaError("assembly ToolSetRef 存在重复 identity")
        for entry in self.selection:
            if isinstance(entry.ref, ToolSetIdentityRef):
                if (
                    entry.ref.session_id != self.session_id
                    or entry.ref.thread_id != self.thread_id
                    or entry.ref.plan_id != self.plan_id
                    or entry.ref.assembly_id != self.assembly_id
                ):
                    raise ItemSchemaError("selection ToolSetRef scope 不一致")
                registered_tool = tool_registry.get(entry.ref.ref_id)
                expected_ref = (
                    registered_tool.identity_ref()
                    if not entry.included and registered_tool is not None
                    else registered_tool
                )
                if (entry.included or registered_tool is not None) and expected_ref != entry.ref:
                    raise ItemSchemaError("selection ToolSetRef manifest 不一致")
                continue
            if ref_registry.get(selection_ref_identity(entry.ref)) != entry.ref:
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
                    )
                    actual_manifest = (
                        contribution.source_revision,
                        contribution.content_length,
                        contribution.content_hash,
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

        self._validate_seal_binding()
        validate_included_overlay_chain(self.selection, self.contributions)
        expected_tool_snapshot = tuple(
            tool for ref in self._selected_tool_refs() for tool in ref.tools
        )
        if canonical_json_bytes(tools) != canonical_json_bytes(expected_tool_snapshot):
            raise ItemSchemaError(
                "ContextAssemblySnapshot.tool_snapshot 与已选 ToolSet manifest 不一致"
            )

    def _validate_seal_binding(self) -> None:
        """联校同一次 seal 的 epoch 与真正被选择的 ToolSet manifest。"""
        if self.seal_binding is not None:
            binding = self.seal_binding
            if not isinstance(binding, ContextSealBinding):
                raise ItemSchemaError("ContextAssemblySnapshot.seal_binding 类型非法")
            if binding.prefix.assembly_id != self.assembly_id:
                raise ItemSchemaError("sealed prefix 不属于当前 assembly")
            selected = tuple(
                ref for ref in self.tool_set_refs
                if any(
                    entry.included and entry.ref.ref_id == ref.ref_id
                    and isinstance(entry.ref, ToolSetRef)
                    for entry in self.selection
                )
            )
            if len(selected) != 1 or (
                selected[0].ref_id != binding.tool_set_snapshot_id
                or selected[0].source_revision != binding.applied_toolset_revision
            ):
                raise ItemSchemaError("sealed ToolSet 与 applied binding 不一致")
            tool_ref = selected[0]
            expected_key = tool_set_manifest_compatibility_key(tool_ref)
            if binding.prefix.state.tool_compatibility_key != expected_key:
                raise ItemSchemaError(
                    "sealed prefix compatibility key 与 ToolSet manifest/policy 不一致"
                )
            if binding.root is not None and binding.root.tool_compatibility_key != expected_key:
                raise ItemSchemaError(
                    "sealed root compatibility key 与 ToolSet manifest/policy 不一致"
                )

    def seal(self) -> ContextAssemblySnapshot:
        if self.sealed:
            return self
        return ContextAssemblySnapshot(
            assembly_id=self.assembly_id,
            session_id=self.session_id,
            thread_id=self.thread_id,
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
            request_input_hash=self.request_input_hash,
            seal_binding=self.seal_binding,
        )

    def as_sealed_plan(self) -> ContextRequestPlan:
        """将 sealed snapshot 还原为同一份不可变请求计划。"""
        return ContextRequestPlan(
            session_id=self.session_id,
            thread_id=self.thread_id,
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
        self._validate_seal_binding()
        plan = self.as_sealed_plan()
        expected_plan_hash = plan.plan_hash()
        if self.plan_hash != expected_plan_hash:
            raise ItemSchemaError(
                "context assembly plan_hash 与 sealed selection/manifest 不一致"
            )
        expected_request_hash = sha256_jcs(_context_request_hash_preimage(
            plan,
            self.provider_version,
            projector_id=self.projector_id,
            projector_version=self.projector_version,
            target_format=self.target_format,
            wire_request=self.request_hash_preimage,
            recomputed_plan_hash=expected_plan_hash,
        ))
        if self.request_hash != expected_request_hash:
            raise ItemSchemaError(
                "context assembly request_hash 与 sealed selection/manifest 不一致"
            )

    def _selected_tool_refs(self) -> tuple[ToolSetRef, ...]:
        """构造校验与序列化共用已选 manifest 的稳定顺序。"""
        selected_tool_ref_ids = {
            entry.ref.ref_id
            for entry in self.selection
            if entry.included and isinstance(entry.ref, ToolSetRef)
        }
        return tuple(
            sorted(
                (
                    tool_ref
                    for tool_ref in self.tool_set_refs
                    if tool_ref.ref_id in selected_tool_ref_ids
                ),
                key=lambda ref: ref.ref_id,
            )
        )

    def to_dict(self) -> dict[str, object]:
        """以规范 envelope 序列化，避免 dataclasses.asdict 泄漏旧别名。"""
        # ToolSetRef 是工具定义的唯一 sealed manifest；tool_snapshot 从已选
        # manifest 完整派生。访问分类只影响对外检查投影，不删改内部封存正文。
        persisted_tool_refs = self._selected_tool_refs()
        persisted_tool_snapshot = [
            dict(tool)
            for tool_ref in persisted_tool_refs
            for tool in tool_ref.tools
        ]
        return {
            "format_version": 2,
            "assembly_id": self.assembly_id,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "request_hash": self.request_hash,
            "history_view_revision": self.history_view_revision,
            "source_overlay_epoch": self.source_overlay_epoch,
            "refs": [ref.to_dict() for ref in self.refs],
            "contributions": [
                item.to_dict()
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
            "request_input_hash": self.request_input_hash,
            "active_view_id": self.active_view_id,
            "selection_policy": self.selection_policy,
            "model_call_id": self.model_call_id,
            "hash_algorithm": self.hash_algorithm,
            "loss": list(self.loss),
            "sealed": self.sealed,
            "plan_state": self.plan_state,
            "seal_binding": (
                context_seal_binding_to_dict(self.seal_binding)
                if self.seal_binding is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ContextAssemblySnapshot:
        """从 SQLite snapshot_json 恢复不可变 assembly 视图。"""
        return context_assembly_snapshot_from_dict(cls, value)
