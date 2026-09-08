"""v2 ContextSelectionEntry domain type。"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    BaseDeltaRole,
    DetailAvailability,
    DetailProtection,
    SelectionKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.schema import validate_selection_compatibility
from app.domain.itemized.serialization import _non_empty_string


@dataclass(frozen=True, slots=True)
class ContextSelectionEntry:
    """Saver seal 时冻结的唯一有序 selection entry。"""

    assembly_id: str
    plan_ordinal: int
    ref: ContextRef | ToolSetRef
    selection_kind: str
    included: bool = True
    omission_reason: str | None = None
    loss: tuple[str, ...] = ()
    visibility: str = "internal"
    protection: str = "public"
    availability: str = "available"
    source_revision: str | None = None
    content_length: int | None = None
    content_hash: str | None = None
    redacted_stable_digest: str | None = None
    detail_ref: DetailRef | None = None
    contribution_id: str | None = None
    base_delta_role: str = "none"
    source_overlay_epoch: int | None = None
    overlay_from_revision: str | None = None
    overlay_to_revision: str | None = None
    overlay_diff_hash: str | None = None
    contribution_ordinal: int | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.assembly_id, "ContextSelectionEntry.assembly_id")
        if not isinstance(self.plan_ordinal, int) or isinstance(
            self.plan_ordinal, bool
        ) or self.plan_ordinal < 0:
            raise ItemSchemaError("ContextSelectionEntry.plan_ordinal 必须是非负整数")
        if not isinstance(self.included, bool):
            raise ItemSchemaError("ContextSelectionEntry.included 必须是 boolean")
        for name, value in (
            ("source_overlay_epoch", self.source_overlay_epoch),
            ("contribution_ordinal", self.contribution_ordinal),
        ):
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ItemSchemaError(
                    f"ContextSelectionEntry.{name} 必须是非负整数或 NULL"
                )
        if self.selection_kind not in {item.value for item in SelectionKind}:
            raise ItemSchemaError(f"未知 selection_kind: {self.selection_kind}")
        if self.base_delta_role not in {item.value for item in BaseDeltaRole}:
            raise ItemSchemaError(f"未知 base_delta_role: {self.base_delta_role}")
        for name, value in (
            ("visibility", self.visibility),
            ("protection", self.protection),
            ("availability", self.availability),
        ):
            _non_empty_string(value, f"ContextSelectionEntry.{name}")
        if self.visibility not in {"public", "internal", "private"}:
            raise ItemSchemaError(f"未知 selection visibility: {self.visibility}")
        if self.protection not in {item.value for item in DetailProtection}:
            raise ItemSchemaError(f"未知 selection protection: {self.protection}")
        if self.availability not in {item.value for item in DetailAvailability}:
            raise ItemSchemaError(f"未知 selection availability: {self.availability}")
        if self.included:
            _non_empty_string(self.source_revision, "ContextSelectionEntry.source_revision")
            if not isinstance(self.content_length, int) or isinstance(
                self.content_length, bool
            ) or self.content_length < 0:
                raise ItemSchemaError(
                    "included ContextSelectionEntry.content_length 必须是非负整数"
                )
            if (self.content_hash is None) == (self.redacted_stable_digest is None):
                raise ItemSchemaError(
                    "included ContextSelectionEntry 必须恰好包含一个 hash token"
                )
        elif self.content_length is not None and (
            not isinstance(self.content_length, int)
            or isinstance(self.content_length, bool)
            or self.content_length < 0
        ):
            raise ItemSchemaError(
                "omitted ContextSelectionEntry.content_length 必须是非负整数或 NULL"
            )
        if (
            not self.included
            and self.content_hash is not None
            and self.redacted_stable_digest is not None
        ):
            raise ItemSchemaError(
                "omitted ContextSelectionEntry 不能同时携带 content_hash 和 redacted_stable_digest"
            )
        if self.source_revision is not None:
            _non_empty_string(self.source_revision, "ContextSelectionEntry.source_revision")
        if self.content_hash is not None:
            _non_empty_string(self.content_hash, "ContextSelectionEntry.content_hash")
        if self.redacted_stable_digest is not None:
            _non_empty_string(
                self.redacted_stable_digest,
                "ContextSelectionEntry.redacted_stable_digest",
            )
        if self.omission_reason is not None:
            _non_empty_string(
                self.omission_reason,
                "ContextSelectionEntry.omission_reason",
            )
        if self.detail_ref is not None:
            if not isinstance(self.detail_ref, DetailRef):
                raise ItemSchemaError("ContextSelectionEntry.detail_ref 必须是 typed DetailRef")
            self.detail_ref.require_owner(self.ref.session_id, self.assembly_id)
        if self.contribution_id is not None:
            _non_empty_string(
                self.contribution_id,
                "ContextSelectionEntry.contribution_id",
            )
        if not isinstance(self.loss, tuple) or not all(
            isinstance(value, str) and value for value in self.loss
        ):
            raise ItemSchemaError("ContextSelectionEntry.loss 必须是字符串 tuple")
        if not self.included and not self.omission_reason:
            raise ItemSchemaError("excluded selection 必须有 omission_reason")
        if not self.included and not self.loss:
            raise ItemSchemaError("plan-order-integrity: omitted selection 必须有 loss")
        if not self.included and (
            self.detail_ref is not None or self.contribution_ordinal is not None
        ):
            raise ItemSchemaError(
                "plan-order-integrity: omitted selection 不得分配 detail_ref/contribution_ordinal"
            )
        ref_type = getattr(self.ref, "ref_type", None)
        if not isinstance(ref_type, str):
            raise ItemSchemaError("selection ref 缺少 ref_type")
        validate_selection_compatibility(
            self.selection_kind,
            ref_type,
            self.base_delta_role,
        )
        if self.selection_kind == SelectionKind.TOOL_SET:
            if not isinstance(self.ref, ToolSetRef):
                raise ItemSchemaError("tool_set selection 必须绑定 ToolSetRef")
            if self.ref.assembly_id != self.assembly_id:
                raise ItemSchemaError(
                    "tool_set selection 的 assembly_id 必须与 ToolSetRef 一致"
                )
            if any(
                value is not None
                for value in (self.detail_ref, self.contribution_id, self.contribution_ordinal)
            ):
                raise ItemSchemaError("ToolSetRef selection 不得绑定 detail/contribution")
        else:
            if not isinstance(self.ref, ContextRef):
                raise ItemSchemaError("非 tool_set selection 必须绑定 ContextRef")
            if self.ref.ref_type == "canonical_item":
                if self.selection_kind != SelectionKind.CANONICAL_HISTORY:
                    raise ItemSchemaError(
                        "canonical_item selection 必须使用 canonical_history"
                    )
                if self.base_delta_role != BaseDeltaRole.NONE:
                    raise ItemSchemaError(
                        "canonical history 不得使用 base/delta selection role"
                    )
            else:
                if self.selection_kind not in {
                    SelectionKind.REQUEST_ONLY,
                    SelectionKind.OVERLAY_BASE,
                    SelectionKind.OVERLAY_DELTA,
                }:
                    raise ItemSchemaError(
                        "request_only selection 必须使用 request_only/overlay tag"
                    )
                expected_role = {
                    SelectionKind.REQUEST_ONLY: BaseDeltaRole.NONE,
                    SelectionKind.OVERLAY_BASE: BaseDeltaRole.BASE,
                    SelectionKind.OVERLAY_DELTA: BaseDeltaRole.DELTA,
                }[self.selection_kind]
                if self.base_delta_role != expected_role:
                    raise ItemSchemaError(
                        "request_only selection 的 base_delta_role 与 tag 不一致"
                    )
        ref_base_delta_role = (
            self.ref.base_delta_role if isinstance(self.ref, ContextRef) else BaseDeltaRole.NONE.value
        )
        ref_overlay_epoch = (
            self.ref.source_overlay_epoch if isinstance(self.ref, ContextRef) else None
        )
        if self.base_delta_role != ref_base_delta_role:
            raise ItemSchemaError(
                "selection base_delta_role 与 ContextRef manifest 不一致"
            )
        if self.source_overlay_epoch != ref_overlay_epoch:
            raise ItemSchemaError(
                "selection source_overlay_epoch 与 ref manifest 不一致"
            )
        ref_from_revision = (
            self.ref.overlay_from_revision if isinstance(self.ref, ContextRef) else None
        )
        ref_to_revision = (
            self.ref.overlay_to_revision if isinstance(self.ref, ContextRef) else None
        )
        ref_diff_hash = (
            self.ref.overlay_diff_hash if isinstance(self.ref, ContextRef) else None
        )
        if (
            self.overlay_from_revision != ref_from_revision
            or self.overlay_to_revision != ref_to_revision
            or self.overlay_diff_hash != ref_diff_hash
        ):
            raise ItemSchemaError("selection overlay diff manifest 与 ref 不一致")
        if (
            self.source_revision is not None
            and self.source_revision != self.ref.source_revision
        ):
            raise ItemSchemaError("selection source_revision 与 ref manifest 不一致")
        if (
            self.content_length is not None
            and self.content_length != self.ref.content_length
        ):
            raise ItemSchemaError("selection content_length 与 ref manifest 不一致")
        if self.content_hash is not None and self.content_hash != self.ref.content_hash:
            raise ItemSchemaError("selection content_hash 与 ref manifest 不一致")
        if (
            self.redacted_stable_digest is not None
            and self.redacted_stable_digest != self.ref.redacted_stable_digest
        ):
            raise ItemSchemaError("selection hash token 与 ref manifest 不一致")
        ref_visibility = (
            self.ref.visibility
            if isinstance(self.ref, ContextRef)
            else "internal"
        )
        if (
            self.visibility != ref_visibility
            or self.protection != self.ref.protection
            or self.availability != self.ref.availability
        ):
            raise ItemSchemaError(
                "selection visibility/protection/availability 与 ref manifest 不一致"
            )
        if isinstance(self.ref, ContextRef):
            if self.ref.ref_type == "canonical_item":
                if any(
                    value is not None
                    for value in (self.detail_ref, self.contribution_id, self.contribution_ordinal)
                ):
                    raise ItemSchemaError(
                        "canonical_history selection 不得绑定 detail/contribution"
                    )
            else:
                if self.included and not self.detail_ref:
                    raise ItemSchemaError(
                        "included request-only selection 必须绑定 detail_ref"
                    )
                contribution_backed = self.contribution_id is not None
                if self.contribution_ordinal is not None and not contribution_backed:
                    raise ItemSchemaError(
                        "contribution_ordinal 非空时必须绑定 contribution_id"
                    )
                if self.included and contribution_backed != (
                    self.contribution_ordinal is not None
                ):
                    raise ItemSchemaError(
                        "included contribution-backed selection 必须同时绑定 "
                        "contribution_id 与 contribution_ordinal"
                    )
                if self.selection_kind in {
                    SelectionKind.OVERLAY_BASE,
                    SelectionKind.OVERLAY_DELTA,
                } and self.included and not contribution_backed:
                    raise ItemSchemaError(
                        "included overlay selection 必须绑定 contribution"
                    )

    def to_dict(self) -> dict[str, object]:
        ref_value = self.ref.to_dict()
        if not self.included and isinstance(self.ref, ToolSetRef):
            # omitted tool set 只保存已有 identity，不序列化工具定义和 policy 正文。
            ref_value.pop("tools")
            ref_value.pop("tool_policy")
        return {
            "format_version": 2,
            "assembly_id": self.assembly_id,
            "plan_ordinal": self.plan_ordinal,
            "ref": ref_value,
            "selection_kind": self.selection_kind,
            "included": self.included,
            "omission_reason": self.omission_reason,
            "loss": list(self.loss),
            "visibility": self.visibility,
            "protection": self.protection,
            "availability": self.availability,
            "source_revision": self.source_revision,
            "content_length": self.content_length,
            "content_hash": self.content_hash,
            "redacted_stable_digest": self.redacted_stable_digest,
            "detail_ref": self.detail_ref.to_dict() if self.detail_ref is not None else None,
            "contribution_id": self.contribution_id,
            "base_delta_role": self.base_delta_role,
            "source_overlay_epoch": self.source_overlay_epoch,
            "overlay_from_revision": self.overlay_from_revision,
            "overlay_to_revision": self.overlay_to_revision,
            "overlay_diff_hash": self.overlay_diff_hash,
            "contribution_ordinal": self.contribution_ordinal,
        }
