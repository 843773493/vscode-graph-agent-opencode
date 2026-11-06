"""三种 itemized projection 共用的 manifest/loss 证据。

这里不读取 storage，也不参与正文编码。证据只由 Saver 本次生成或校验过的 sealed
assembly 生成，供 LangChain、native provider 和 Web history 的结果做一致性比较。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.session_context import SessionContextResourceDTO


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
    resource_refs: tuple[SessionContextResourceDTO, ...] = ()

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
            "resource_refs": [ref.model_dump(mode="json") for ref in self.resource_refs],
        }


def build_projection_evidence(
    snapshot: ContextAssemblySnapshot,
    *,
    projection: str,
    losses: Iterable[str] = (),
    resource_refs: tuple[SessionContextResourceDTO, ...] = (),
) -> ProjectionEvidence:
    """从本次已验证的 assembly 建立证据，复用其已计算的 plan 摘要。

    selection 保留原始顺序；这里不重建 plan、重算同一摘要或重新选择正文。
    冷读的 plan/request hash 仍由 Saver 在进入映射前独立校验。
    """
    # snapshot 构造已保证 sealed、顺序及 session/assembly 归属；这里只补 plan 约束。
    for entry in snapshot.selection:
        if getattr(entry.ref, "plan_id", None) not in (None, snapshot.plan_id):
            raise ValueError("source-mismatch: selection evidence plan owner 不一致")
    selection = tuple(entry.to_dict() for entry in snapshot.selection)
    selection_manifest_hash = sha256_jcs(
        {
            "schema": "itemized-selection-manifest:v1",
            "session_id": snapshot.session_id,
            "plan_id": snapshot.plan_id,
            "assembly_id": snapshot.assembly_id,
            "history_view_revision": snapshot.history_view_revision,
            "source_overlay_epoch": snapshot.source_overlay_epoch,
            "selection": list(selection),
        }
    )
    entry_losses = tuple(loss for entry in snapshot.selection for loss in entry.loss)
    combined_losses = tuple(
        dict.fromkeys(
            loss
            for loss in (*entry_losses, *tuple(losses))
            if isinstance(loss, str) and loss
        )
    )
    return ProjectionEvidence(
        projection=projection,
        session_id=snapshot.session_id,
        plan_id=snapshot.plan_id,
        assembly_id=snapshot.assembly_id,
        plan_hash=snapshot.plan_hash,
        history_view_revision=snapshot.history_view_revision,
        source_overlay_epoch=snapshot.source_overlay_epoch,
        selection=selection,
        selection_manifest_hash=selection_manifest_hash,
        losses=combined_losses,
        resource_refs=resource_refs,
    )


__all__ = ["ProjectionEvidence", "build_projection_evidence"]
