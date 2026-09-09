"""三种 itemized projection 共用的 manifest/loss 证据。

这里不读取 storage，也不参与正文编码。证据只由 Saver 已经校验过的 sealed
plan 生成，供 LangChain、native provider 和 Web history 的结果做一致性比较。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.mapping.itemized.selection import validate_projection_selection


@dataclass(frozen=True, slots=True)
class ProjectionEvidence:
    """一次 projection 使用的 immutable selection、两个 revision 轴和 loss。"""

    projection: str
    session_id: str
    plan_id: str
    assembly_id: str
    plan_hash: str
    history_view_revision: int
    source_overlay_epoch: int
    selection: tuple[Mapping[str, object], ...]
    selection_manifest_hash: str
    losses: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "itemized-projection-evidence:v1",
            "projection": self.projection,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "assembly_id": self.assembly_id,
            "plan_hash": self.plan_hash,
            "history_view_revision": self.history_view_revision,
            "source_overlay_epoch": self.source_overlay_epoch,
            "selection": [dict(entry) for entry in self.selection],
            "selection_manifest_hash": self.selection_manifest_hash,
            "losses": list(self.losses),
        }


def build_projection_evidence(
    plan: ContextRequestPlan,
    *,
    projection: str,
    losses: Iterable[str] = (),
) -> ProjectionEvidence:
    """从同一个 sealed plan 建立可跨 projection 比较的证据。

    ``selection`` 以 Saver 的原始顺序序列化；函数不排序、不读取正文，也不
    根据 contribution/ref id 重新选择。sealed plan 的 domain 校验在此之前已
    完成，这里再检查两个 owner identity，避免把证据挂到错误的读取面。
    """
    if plan.plan_state != "sealed" or not plan.assembly_id:
        raise ValueError("context plan 未 sealed，不能建立 projection evidence")
    if not plan.session_id or not plan.plan_id:
        raise ValueError("projection evidence 缺少 session_id/plan_id")
    entries = validate_projection_selection(plan.selection)
    for entry in entries:
        if entry.assembly_id != plan.assembly_id:
            raise ValueError(
                "plan-order-integrity: selection evidence assembly owner 不一致"
            )
        if entry.ref.session_id != plan.session_id:
            raise ValueError(
                "source-mismatch: selection evidence session owner 不一致"
            )
        if getattr(entry.ref, "plan_id", None) not in (None, plan.plan_id):
            raise ValueError("source-mismatch: selection evidence plan owner 不一致")
    selection = tuple(entry.to_dict() for entry in entries)
    selection_manifest_hash = sha256_jcs(
        {
            "schema": "itemized-selection-manifest:v1",
            "session_id": plan.session_id,
            "plan_id": plan.plan_id,
            "assembly_id": plan.assembly_id,
            "history_view_revision": plan.history_view_revision,
            "source_overlay_epoch": plan.source_overlay_epoch,
            "selection": list(selection),
        }
    )
    entry_losses = tuple(loss for entry in entries for loss in entry.loss)
    combined_losses = tuple(
        dict.fromkeys(
            loss
            for loss in (*entry_losses, *tuple(losses))
            if isinstance(loss, str) and loss
        )
    )
    return ProjectionEvidence(
        projection=projection,
        session_id=plan.session_id,
        plan_id=plan.plan_id,
        assembly_id=plan.assembly_id,
        plan_hash=plan.plan_hash(),
        history_view_revision=plan.history_view_revision,
        source_overlay_epoch=plan.source_overlay_epoch,
        selection=selection,
        selection_manifest_hash=selection_manifest_hash,
        losses=combined_losses,
    )


__all__ = ["ProjectionEvidence", "build_projection_evidence"]
