"""request-only 贡献值对象、来源绑定与 selection 解析规则。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields

from app.domain.itemized.enums import (
    BaseDeltaRole,
    DetailProtection,
    ReplacementPolicy,
    SelectionRole,
)
from app.domain.itemized.epoch.root_compilation import RootPlacement
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    copy_json_value,
    validate_hash_token,
)
from app.domain.itemized.identity.refs import (
    ContextRef,
)
from app.domain.itemized.schema import validate_extensions
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """producer 声明的来源身份；selection 与顺序仍归封存 plan。"""

    # 普通来源 alias 可空；禁止 reader 从正文或 extension 推导。
    source_ref: str | None = None
    # overlay alias、身份、role 和 epoch 必须同时出现，只绑定一个 epoch。
    overlay_ref: str | None = None
    overlay_id: str | None = None
    overlay_role: str = BaseDeltaRole.NONE
    source_overlay_epoch: int | None = None

    def __post_init__(self) -> None:
        for name in ("source_ref", "overlay_ref", "overlay_id"):
            value = getattr(self, name)
            if value is not None:
                _non_empty_string(value, f"SourceBinding.{name}")
        if not isinstance(self.overlay_role, str) or self.overlay_role not in {item.value for item in BaseDeltaRole}:
            raise ItemSchemaError("SourceBinding.overlay_role 必须是 none|base|delta")
        if self.overlay_role == BaseDeltaRole.NONE:
            if any(value is not None for value in (
                self.overlay_ref, self.overlay_id, self.source_overlay_epoch
            )):
                raise ItemSchemaError("非 overlay SourceBinding 不得携带 overlay 字段")
        elif (
            self.source_ref is None or self.overlay_ref is None or self.overlay_id is None
            or type(self.source_overlay_epoch) is not int or self.source_overlay_epoch < 0
        ):
            raise ItemSchemaError("overlay SourceBinding 缺少显式 alias/id/epoch")

    @classmethod
    def from_dict(cls, value: object) -> SourceBinding:
        if not isinstance(value, Mapping):
            raise ItemSchemaError("SourceBinding 必须是 object")
        fields = {"source_ref", "overlay_ref", "overlay_id", "overlay_role", "source_overlay_epoch"}
        if set(value) - fields or "overlay_role" not in value:
            raise ItemSchemaError("SourceBinding 缺少 overlay_role 或含未知字段")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, object]:
        return {name: value for name in (
            "source_ref", "overlay_ref", "overlay_id", "overlay_role", "source_overlay_epoch"
        ) if (value := getattr(self, name)) is not None}


@dataclass(frozen=True, slots=True)
class ContextContribution:
    # owner-thread registry 内稳定的贡献 slot 身份，不是 assembly 顺序。
    contribution_id: str
    # 来源类别与已经接受的语义 revision；不在请求期重新读取来源。
    source_kind: str
    source_revision: str
    # 规范正文的内容摘要；与正文长度共同验证封存来源。
    content_hash: str | None = None
    # 贡献本体始终 request-only，不得混入 canonical item 或 ToolSet registry。
    request_only: bool = True
    # 非决策扩展仅用于审计/展示，不参与选择、替换、归组或恢复。
    extensions: Mapping[str, object] = field(default_factory=dict)
    # 可空代表无 alias；overlay 必须由 producer 完整声明。
    source_binding: SourceBinding | None = None
    # 贡献正文的闭合语义类别，决定 hash 域与 overlay 校验。
    contribution_kind: str = "prompt"
    # producer 提供的冻结正文；封存后由 detail owner 保存和恢复。
    body: object | None = None
    # 正文的逻辑长度，不复用 JSONL 行长度或 detail 文件字节数。
    content_length: int | None = None
    # 展示可见性与正文保护等级分别校验，不从扩展推断。
    visibility: str = "internal"
    protection: str = "public"
    # draft registry 中为空；只有 sealed selection 绑定实际 assembly。
    assembly_id: str | None = None
    # 同一 assembly 内贡献顺序；不复用 registry 的 source_ordinal。
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
    # selection_role 决定该 contribution 是否允许作为独立候选进入 selection：
    # direct 仍需 selection authority 显式选入，backing_only 只能作为其它显式
    # selection ref 的 source backing，不得自行占一个 plan_ordinal。它参与
    # 「选择」决策，因此必须是 typed core 字段，不能由自由 metadata/extensions
    # 的同名 key（含旧 selection_only flag）承载。默认 direct 与迁移前语义一致。
    selection_role: SelectionRole = SelectionRole.DIRECT
    # replacement_policy 决定同一 owner slot 的未封存 registry 记录能否被新
    # revision 原位覆盖：只有显式 replaceable 且 revision/CAS 匹配才可替换，
    # 已提交 contribution/selection/JSONL 永远不原地更新。它参与「替换 / 安全
    # 判定」，因此必须是 typed core 字段，不能由自由 metadata/extensions 的同名
    # key（含旧 replaceable_source flag）承载。默认 immutable 与迁移前语义一致。
    replacement_policy: ReplacementPolicy = ReplacementPolicy.IMMUTABLE
    _body_snapshot: object = field(default=None, init=False, repr=False, compare=False)
    _extensions_snapshot: object = field(default=None, init=False, repr=False, compare=False)

    def __getattribute__(self, name: str) -> object:
        if name in {"body", "extensions"}:
            carrier_name = "_body_snapshot" if name == "body" else "_extensions_snapshot"
            try:
                carrier = object.__getattribute__(self, carrier_name)
            except AttributeError:
                carrier = None
            if carrier is not None:
                return copy_json_value(carrier)
        return object.__getattribute__(self, name)

    def __post_init__(self) -> None:
        for name in ("contribution_id", "source_kind", "source_revision"):
            _non_empty_string(getattr(self, name), f"ContextContribution.{name}")
        if not isinstance(self.contribution_kind, str) or self.contribution_kind not in {
            "prompt",
            "overlay_base",
            "overlay_delta",
            "notice",
            "model_message",
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
        if self.selection_role not in {item.value for item in SelectionRole}:
            raise ItemSchemaError(
                f"未知 ContextContribution.selection_role: {self.selection_role!r}；"
                "closed set 只有 direct|backing_only"
            )
        if self.replacement_policy not in {item.value for item in ReplacementPolicy}:
            raise ItemSchemaError(
                f"未知 ContextContribution.replacement_policy: {self.replacement_policy!r}；"
                "closed set 只有 immutable|replaceable"
            )
        validate_extensions(self.extensions, field="ContextContribution.extensions")
        if self.source_binding is not None and not isinstance(self.source_binding, SourceBinding):
            raise ItemSchemaError("ContextContribution.source_binding 必须是 typed SourceBinding")
        role = self.source_binding.overlay_role if self.source_binding is not None else BaseDeltaRole.NONE
        if self.contribution_kind.startswith("overlay_"):
            if role != self.contribution_kind.removeprefix("overlay_"):
                raise ItemSchemaError("ContextContribution overlay kind 与 source binding role 不一致")
        elif role != BaseDeltaRole.NONE:
            raise ItemSchemaError("非 overlay contribution 不得绑定 overlay SourceBinding")
        if self.body is not None:
            if self.contribution_kind == "model_message":
                if not isinstance(self.body, Mapping) or set(self.body) != {"message", "item_ids", "retry_source_item_ids"}:
                    raise ItemSchemaError("model_message 必须绑定精确 message 与 canonical item IDs")
                message, ids = self.body["message"], self.body["item_ids"]
                retry_ids = self.body["retry_source_item_ids"]
                if (not isinstance(message, Mapping) or set(message) != {"type", "data"}
                    or message["type"] not in {"human", "ai", "tool"} or not isinstance(message["data"], Mapping)
                    or not isinstance(ids, list) or not all(isinstance(value, str) and value for value in ids)
                    or len(ids) != len(set(ids))
                    or not isinstance(retry_ids, list) or not all(isinstance(value, str) and value for value in retry_ids)
                    or len(retry_ids) != len(set(retry_ids))):
                    raise ItemSchemaError("model_message 的角色、消息数据或 canonical refs 非法")
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
        if self.content_hash is not None:
            validate_hash_token(self.content_hash, "ContextContribution.content_hash")
        if self.content_hash is None:
            raise ItemSchemaError('ContextContribution 必须包含 content_hash ')
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

        body = object.__getattribute__(self, "body")
        extensions = object.__getattribute__(self, "extensions")
        canonical_json_bytes(extensions)
        object.__setattr__(
            self,
            "_body_snapshot",
            copy_json_value(body) if body is not None else None,
        )
        object.__setattr__(self, "_extensions_snapshot", copy_json_value(extensions))
        object.__setattr__(self, "body", None)
        object.__setattr__(self, "extensions", {})


    def to_dict(self) -> dict[str, object]:
        """草稿与封存共享唯一字段投影；保护正文与 registry ordinal 不补写。"""
        result = {
            entry.name: getattr(self, entry.name)
            for entry in fields(self)
            if entry.init
        }
        result["extensions"] = dict(self.extensions)
        result["source_binding"] = self.source_binding.to_dict() if self.source_binding is not None else None
        if self.protection != DetailProtection.PUBLIC:
            result["body"] = None
        if self.source_ordinal is None:
            result.pop("source_ordinal")
        return result

    def binding_manifest(self) -> dict[str, object]:
        """SQLite 镜像与封存来源共用的 typed binding 清单，永不携带 registry 序号。"""
        return {
            "root_placement": self.root_placement,
            "source_binding": self.source_binding.to_dict() if self.source_binding is not None else None,
            "extensions": dict(self.extensions),
        }


def parse_contribution_binding_manifest(value: object) -> dict[str, object]:
    """恢复唯一 typed binding 镜像；扩展不能覆盖其它 registry 核心列。"""
    if not isinstance(value, Mapping) or set(value) != {"root_placement", "source_binding", "extensions"}:
        raise ItemSchemaError("contribution binding manifest 字段非法")
    if not isinstance(value["root_placement"], str) or value["root_placement"] not in {"root_eligible", "tail_only"}:
        raise ItemSchemaError("contribution binding manifest 缺少有效 root_placement")
    binding = SourceBinding.from_dict(value["source_binding"]) if value["source_binding"] is not None else None
    validate_extensions(value["extensions"], field="contribution.extensions")
    return {"root_placement": value["root_placement"], "source_binding": binding.to_dict() if binding is not None else None, "extensions": dict(value["extensions"])}


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
            and contribution.source_binding is not None
            and contribution.source_binding.overlay_ref == ref.ref_id
            and contribution.source_binding.source_ref == ref.source_ref
            and contribution.source_binding.overlay_role == role
            and contribution.source_binding.source_overlay_epoch
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
            and contribution.source_binding is not None
            and contribution.source_binding.source_ref == ref.source_ref
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
            contribution.source_binding.overlay_id, "overlay chain SourceBinding.overlay_id"
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
