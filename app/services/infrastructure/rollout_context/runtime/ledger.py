"""实时 context ledger 的稳定 domain 记录与排序。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from app.domain.itemized.request_plan import ContextContribution
from app.domain.itemized.runtime import ItemDraft, ProvenanceEdge


def ordered_contributions(
    contributions: Iterable[object],
    *,
    pending_ids: Iterable[str] = (),
) -> tuple[object, ...]:
    """只按显式 source/selection ordinal 排序，identity 仅作 tie-breaker。"""
    pending = set(pending_ids)

    def key(value: object) -> tuple[int, int, str]:
        # source_ordinal 只由 itemized registry 分配并经 typed 字段承载；
        # 自由 metadata 的同名键不再参与排序。
        raw = getattr(value, "source_ordinal", None)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ValueError(
                "context contribution 缺少 registry 分配的 typed source_ordinal；"
                f" contribution_id={getattr(value, 'contribution_id', '')}"
            )
        identifier = str(getattr(value, "contribution_id", ""))
        return (0 if identifier in pending else 1, raw, identifier)

    return tuple(sorted(contributions, key=key))

@dataclass(slots=True)
class RuntimeContextLedger:
    """当前 execution 的内存 provenance ledger；重启后以 assembly snapshot 为准。"""

    drafts: dict[str, ItemDraft]
    contributions: dict[str, ContextContribution]
    edges: list[ProvenanceEdge]
    pending_notice_ids: list[str]

    def __init__(self) -> None:
        self.drafts = {}
        self.contributions = {}
        self.edges = []
        self.pending_notice_ids = []

    def _with_source_ordinal(
        self,
        contribution: ContextContribution,
        *,
        preserve_from: ContextContribution | None = None,
    ) -> ContextContribution:
        """保留 itemized registry 已分配的 typed source_ordinal slot。

        source_ordinal 只由 registry（SQLite context_contributions 列）
        分配；ledger 不从事件到达顺序、内存计数或 metadata/extensions
        补造序号，只做 slot 保留：replacement 沿用原 slot，避免 source
        revision 更新导致排序漂移。没有 registry 序号的 contribution 在
        这里 fail closed。
        """
        raw = contribution.source_ordinal
        if raw is None and preserve_from is not None:
            raw = preserve_from.source_ordinal
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ValueError(
                "context contribution 缺少 registry 分配的 typed source_ordinal: "
                f"{contribution.contribution_id}"
            )
        if contribution.source_ordinal == raw:
            return contribution
        return replace(contribution, source_ordinal=raw)

    def add_draft(self, draft: ItemDraft) -> None:
        if draft.item_id in self.drafts:
            raise ValueError(f"重复 item draft identity: {draft.item_id}")
        self.drafts[draft.item_id] = draft

    def add_contribution(self, contribution: ContextContribution) -> None:
        existing = self.contributions.get(contribution.contribution_id)
        contribution = self._with_source_ordinal(
            contribution,
            preserve_from=existing,
        )
        if existing is not None and existing != contribution:
            raise ValueError(f"context contribution identity 冲突: {contribution.contribution_id}")
        self.contributions[contribution.contribution_id] = contribution

    def replace_contribution(self, contribution: ContextContribution) -> None:
        """更新同一可变 source slot 的当前 revision。

        只有明确标记为 replaceable_source 的 middleware source 才应使用此
        入口；普通 provenance contribution 仍由 add_contribution 保持不可变。
        """
        existing = self.contributions.get(contribution.contribution_id)
        self.contributions[contribution.contribution_id] = self._with_source_ordinal(
            contribution,
            preserve_from=existing,
        )

    def reconcile_contributions(
        self,
        contributions: Iterable[ContextContribution],
    ) -> None:
        """用同一已提交 registry 重建当前内存 contribution view。

        assembly/overlay 状态会淘汰旧的 source contribution。逐条 replace
        会把已经失效的 overlay 留在内存里，随后按 ``overlay_ref`` 产生多重
        映射；这里以 SQLite 已提交集合为唯一输入，既清理 stale entry，
        也保留 registry 分配的 source ordinal。
        """
        reconciled: dict[str, ContextContribution] = {}
        for contribution in contributions:
            if contribution.contribution_id in reconciled:
                raise ValueError(
                    "context contribution registry 存在重复 identity: "
                    f"{contribution.contribution_id}"
                )
            reconciled[contribution.contribution_id] = self._with_source_ordinal(
                contribution,
                preserve_from=self.contributions.get(contribution.contribution_id),
            )
        self.contributions = reconciled

    def add_edge(self, edge: ProvenanceEdge) -> None:
        if any(existing.edge_id == edge.edge_id and existing != edge for existing in self.edges):
            raise ValueError(f"provenance edge identity 冲突: {edge.edge_id}")
        if not any(existing.edge_id == edge.edge_id for existing in self.edges):
            self.edges.append(edge)

    def add_pending_notice(self, contribution_id: str) -> None:
        if contribution_id not in self.pending_notice_ids:
            self.pending_notice_ids.append(contribution_id)

    def snapshot_contributions(self) -> tuple[ContextContribution, ...]:
        pending = set(self.pending_notice_ids)

        return tuple(
            ordered_contributions(self.contributions.values(), pending_ids=pending)
        )

__all__ = ["RuntimeContextLedger", "ordered_contributions"]
