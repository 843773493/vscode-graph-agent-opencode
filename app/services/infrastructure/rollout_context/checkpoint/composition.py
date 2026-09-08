"""ContextRequestPlan 的 runtime composition 与 contribution registry owner。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.domain.itemized.enums import BaseDeltaRole, SemanticKind
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)


def _manifest_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"plan-order-integrity: manifest {field} 非法")
    return value


def _manifest_optional_string(value: object, *, field: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"plan-order-integrity: manifest {field} 非法")
    return value


def _manifest_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"plan-order-integrity: manifest {field} 非法")
    return value


def _manifest_optional_non_negative_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _manifest_non_negative_int(value, field=field)


def _manifest_sql_bool(value: object, *, field: str) -> bool:
    if value not in (0, 1) or isinstance(value, bool):
        raise ValueError(f"plan-order-integrity: manifest {field} 非法")
    return value == 1


class ContextPlanCompositionMixin:
    def _composer_for(
        self,
        session_id: str,
        checkpoint_ns: str = "",
    ) -> ContextPlanComposer:
        key = (_manifest_string(session_id, field="session_id"), checkpoint_ns)
        with self._lock:
            composer = self._context_plan_composers.get(key)
            if composer is None:
                composer = ContextPlanComposer()
                self._context_plan_composers[key] = composer
            return composer

    def compose_context_plan(
        self,
        *,
        session_id: str,
        plan_id: str,
        refs: Sequence[ContextRef],
        tool_snapshot: Sequence[Mapping[str, object]] = (),
        history_view_revision: int = 0,
        source_overlay_epoch: int = 0,
        compiler_version: str = "itemized-context-v1",
    ) -> ContextRequestPlan:
        """业务层唯一的 ContextRequestPlan 入口。"""
        return self._composer_for(session_id).compose(
            session_id=session_id,
            plan_id=plan_id,
            refs=refs,
            tool_snapshot=tool_snapshot,
            history_view_revision=history_view_revision,
            source_overlay_epoch=source_overlay_epoch,
            compiler_version=compiler_version,
        )

    def register_context_contribution(
        self,
        session_id: str,
        contribution: ContextContribution,
        *,
        checkpoint_ns: str = "",
        request_content: object | None = None,
    ) -> None:
        """登记 middleware/source provenance；贡献正文仍由 request-only source 持有。"""
        composer = self._composer_for(session_id, checkpoint_ns)
        replaceable = contribution.metadata.get("replaceable_source") is True
        with self._lock:
            existing = composer.ledger.contributions.get(contribution.contribution_id)
            if (
                existing is not None
                and replace(
                    existing,
                    metadata={
                        key: value
                        for key, value in existing.metadata.items()
                        if key != "source_ordinal"
                    },
                )
                != replace(
                    contribution,
                    metadata={
                        key: value
                        for key, value in contribution.metadata.items()
                        if key != "source_ordinal"
                    },
                )
                and not replaceable
            ):
                raise ValueError(
                    f"context contribution identity 冲突: {contribution.contribution_id}"
                )

            # SQLite registry 是提交边界；先完成持久化，再更新运行时 ledger/body
            # cache，避免 storage 失败后出现仅内存可见的 source。
            self._storage.register_context_contribution(
                session_id,
                contribution,
                checkpoint_ns=checkpoint_ns,
            )
            # source_ordinal 是 SQLite registry 分配的稳定顺序。把它复制到
            # 当前 execution 的内存 view，避免重启前后分别按 dict/created_at
            # 推测 contribution 顺序；seal 时会把它从 assembly provenance
            # metadata 中剥离，assembly 顺序由 contribution_ordinal 固化。
            persisted = next(
                (
                    row
                    for row in self._storage.list_context_contributions(
                        session_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                    if row.get("contribution_id") == contribution.contribution_id
                ),
                None,
            )
            source_ordinal = (
                persisted.get("source_ordinal") if persisted is not None else None
            )
            if isinstance(source_ordinal, int) and not isinstance(source_ordinal, bool):
                contribution = replace(
                    contribution,
                    metadata={
                        **dict(contribution.metadata),
                        "source_ordinal": source_ordinal,
                    },
                )
            if replaceable:
                composer.ledger.replace_contribution(contribution)
            else:
                composer.ledger.add_contribution(contribution)
            if request_content is not None:
                self._request_only_content[
                    (session_id, checkpoint_ns, contribution.contribution_id)
                ] = request_content

    def compose_committed_context_plan(
        self,
        session_id: str,
        *,
        plan_id: str,
        tool_snapshot: Sequence[Mapping[str, object]] = (),
        include_pending_notices: bool = True,
        compiler_version: str = "itemized-context-v1",
        checkpoint_ns: str = "",
    ) -> ContextRequestPlan:
        """由 Saver 从已提交 active view 生成一次 request plan。

        业务层不需要、也不允许直接遍历 RolloutStorage；此方法在 Saver 内部
        持有同一个 read snapshot，同时读取 item refs 和 revision，避免 plan
        在 view 切换过程中拼出跨 snapshot 的混合上下文。
        """
        with self._context_reader.open_snapshot(
            session_id,
            checkpoint_ns,
        ) as snapshot:
            committed_refs = self._storage.committed_context_refs(
                snapshot,
                include_pending_notices=include_pending_notices,
            )
            refs = list(committed_refs)
            seen_overlay_bindings: set[tuple[str, str]] = set()
            active_overlay_contribution_ids: set[str] = set()
            persisted_overlays = self._storage.list_source_overlays(
                session_id,
                checkpoint_ns=checkpoint_ns,
                snapshot=snapshot,
            )
            for overlay in persisted_overlays:
                status = overlay.get("status")
                if status not in {"active", "superseded", "materialized"}:
                    raise ValueError(
                        "plan-order-integrity: source overlay status 非法: "
                        f"{overlay.get('overlay_id')}"
                    )
                _manifest_string(overlay.get("overlay_id"), field="overlay_id")
                _manifest_string(overlay.get("source_revision"), field="source_revision")
                _manifest_non_negative_int(
                    overlay.get("source_overlay_epoch"),
                    field="source_overlay_epoch",
                )
                _manifest_string(overlay.get("created_at"), field="created_at")
            active_overlays = [
                overlay
                for overlay in persisted_overlays
                if overlay.get("status") == "active"
            ]
            # storage 当前已经按 epoch/created_at/overlay_id 返回，但组合器不能
            # 把 SQL 的返回顺序当作领域合同。这里再次建立有显式校验的稳定
            # 顺序，确保重启、不同 SQLite query plan 以及多个同 epoch source
            # overlay 都不会改变 base -> delta 的 request selection。
            for overlay in active_overlays:
                overlay_id = _manifest_string(
                    overlay.get("overlay_id"), field="overlay_id"
                )
                overlay_epoch = _manifest_non_negative_int(
                    overlay.get("source_overlay_epoch"),
                    field=f"source_overlay_epoch:{overlay_id}",
                )
            def overlay_sort_key(overlay: Mapping[str, object]) -> tuple[int, str, str]:
                current_overlay_id = _manifest_string(
                    overlay.get("overlay_id"), field="overlay_id"
                )
                return (
                    _manifest_non_negative_int(
                        overlay.get("source_overlay_epoch"),
                        field=f"source_overlay_epoch:{current_overlay_id}",
                    ),
                    _manifest_string(overlay.get("created_at"), field="created_at"),
                    current_overlay_id,
                )

            active_overlays.sort(key=overlay_sort_key)
            active_overlay_by_id = {
                _manifest_string(overlay.get("overlay_id"), field="overlay_id"): overlay
                for overlay in active_overlays
            }
            for overlay in active_overlays:
                for field_name in ("base_ref", "delta_ref"):
                    source_ref_value = overlay.get(field_name)
                    if source_ref_value is None:
                        continue
                    source_ref = _manifest_string(source_ref_value, field=field_name)
                    role = "base" if field_name == "base_ref" else "delta"
                    binding_key = (role, source_ref)
                    if binding_key in seen_overlay_bindings:
                        raise ValueError(
                            "plan-order-integrity: active overlay ref 映射到多个 overlay: "
                            f"{role}:{source_ref}"
                        )
                    seen_overlay_bindings.add(binding_key)
                    overlay_epoch = _manifest_non_negative_int(
                        overlay.get("source_overlay_epoch"),
                        field="source_overlay_epoch",
                    )
                    active_overlay_contribution_ids.add(
                        f"overlay:{overlay_id}:{role}"
                    )
                    with self._lock:
                        overlay_content = self._request_only_content.get(
                            (session_id, checkpoint_ns, source_ref)
                        )
                    source_revision = _manifest_optional_string(
                        overlay.get(f"{role}_source_revision"),
                        field=f"{role}_source_revision",
                    ) or _manifest_string(
                        overlay.get("source_revision"), field="source_revision"
                    )
                    manifest_length = overlay.get(f"{role}_content_length")
                    manifest_hash = overlay.get(f"{role}_content_hash")
                    manifest_digest = overlay.get(f"{role}_redacted_stable_digest")
                    manifest_length = _manifest_optional_non_negative_int(
                        manifest_length,
                        field=f"{role}_content_length",
                    )
                    manifest_hash = _manifest_optional_string(
                        manifest_hash,
                        field=f"{role}_content_hash",
                    )
                    manifest_digest = _manifest_optional_string(
                        manifest_digest,
                        field=f"{role}_redacted_stable_digest",
                    )
                    if manifest_hash is not None and manifest_digest is not None:
                        raise ValueError(
                            "plan-order-integrity: overlay manifest 不能同时带两种 hash token: "
                            f"{source_ref}"
                        )
                    complete_manifest = manifest_length is not None and (
                        manifest_hash is not None or manifest_digest is not None
                    )
                    if overlay_content is not None and not complete_manifest:
                        raise ValueError(
                            "plan-order-integrity: active overlay 正文缺少完整 manifest: "
                            f"{source_ref}"
                        )
                    # 重启只清空内存 body，不改变已提交 source 的可用性。
                    # 完整 manifest 的正文在 seal 时从持久 detail 恢复并校验；
                    # 若确实丢失，必须报 detail-unavailable，不能默默省略策略。
                    ref_kwargs: dict[str, object] = {
                        "source_ref": source_ref,
                        "content_length": manifest_length,
                        "content_hash_value": manifest_hash,
                        "redacted_stable_digest": manifest_digest,
                        "availability": "available" if complete_manifest else "unavailable",
                    }
                    ref_kwargs.update(
                        {
                            "base_delta_role": (
                                BaseDeltaRole.BASE.value
                                if field_name == "base_ref"
                                else BaseDeltaRole.DELTA.value
                            ),
                            "source_overlay_epoch": overlay_epoch,
                        }
                    )
                    if field_name == "delta_ref":
                        ref_kwargs.update(
                            {
                                "overlay_from_revision": _manifest_optional_string(
                                    overlay.get("delta_from_revision"),
                                    field="delta_from_revision",
                                ),
                                "overlay_to_revision": _manifest_optional_string(
                                    overlay.get("delta_to_revision"),
                                    field="delta_to_revision",
                                ),
                                "overlay_diff_hash": _manifest_optional_string(
                                    overlay.get("delta_diff_hash"),
                                    field="delta_diff_hash",
                                ),
                            }
                        )
                    refs.append(
                        ContextRef.request_only_ref(
                            source_ref,
                            session_id=session_id,
                            plan_id=plan_id,
                            source_revision=source_revision,
                            semantic_kind=SemanticKind.RUNTIME_NOTICE.value,
                            **ref_kwargs,
                        )
                    )
            history_view_revision = snapshot.manifest.history_view_revision
            source_overlay_epoch = snapshot.manifest.source_overlay_epoch
            active_view_id = self._storage.active_view_id(snapshot)
            composer = self._composer_for(session_id, checkpoint_ns)
            loaded_contributions: list[ContextContribution] = []
            for raw in self._storage.list_context_contributions(
                session_id,
                checkpoint_ns=checkpoint_ns,
                snapshot=snapshot,
            ):
                metadata_json = raw.get("metadata_json")
                if not isinstance(metadata_json, str):
                    raise TypeError(
                        "plan-order-integrity: context contribution metadata_json 非法: "
                        f"{raw.get('contribution_id')}"
                    )
                metadata = json.loads(metadata_json)
                if not isinstance(metadata, Mapping):
                    raise TypeError(
                        f"context contribution metadata 非法: {raw['contribution_id']}"
                    )
                contribution_id = _manifest_string(
                    raw.get("contribution_id"), field="contribution_id"
                )
                # overlay contribution 是当前 active overlay 的 manifest backing
                # record。旧 epoch 的同一 source ref 仍可能保留在 registry，
                # 但不能进入当前 plan，否则 alias lookup 会把一个 ref 解析成
                # 多条 contribution。active overlay 的 role/epoch 由 overlay
                # registry 重新绑定，不能相信 stale metadata 自己声明的值。
                overlay_id = metadata.get("overlay_id")
                if overlay_id is not None:
                    if contribution_id not in active_overlay_contribution_ids:
                        continue
                    role = contribution_id.rsplit(":", 1)[-1]
                    if role not in {"base", "delta"}:
                        raise ValueError(
                            "plan-order-integrity: overlay contribution role 非法: "
                            f"{contribution_id}"
                        )
                    active_overlay = active_overlay_by_id.get(
                        _manifest_string(overlay_id, field="overlay_id")
                    )
                    if active_overlay is None:
                        raise ValueError(
                            "plan-order-integrity: active overlay contribution 缺少 overlay: "
                            f"{contribution_id}"
                        )
                    source_ref = active_overlay.get(f"{role}_ref")
                    overlay_epoch = active_overlay.get("source_overlay_epoch")
                    source_ref = _manifest_string(
                        source_ref,
                        field=f"overlay source_ref:{contribution_id}",
                    )
                    overlay_epoch = _manifest_non_negative_int(
                        overlay_epoch,
                        field=f"overlay epoch:{contribution_id}",
                    )
                    metadata = {
                        **dict(metadata),
                        "source_ref": source_ref,
                        "overlay_ref": source_ref,
                        "overlay_role": role,
                        "source_overlay_epoch": overlay_epoch,
                        "selection_only": True,
                    }
                # source_ordinal 是 registry 的稳定持久化顺序，不能在重启后
                # 退回 created_at 排序；assembly 内的 contribution_ordinal
                # 仍由 seal 单独分配，二者不能混用。
                metadata = {
                    **dict(metadata),
                    "source_ordinal": _manifest_non_negative_int(
                        raw.get("source_ordinal"), field="source_ordinal"
                    ),
                }
                loaded_contributions.append(
                    ContextContribution(
                        contribution_id=contribution_id,
                        source_kind=_manifest_string(
                            raw.get("source_kind"), field="source_kind"
                        ),
                        source_revision=_manifest_string(
                            raw.get("source_revision"), field="source_revision"
                        ),
                        content_hash=(
                            _manifest_string(raw["content_hash"], field="content_hash")
                            if raw["content_hash"] is not None
                            else None
                        ),
                        # digest-only manifest 不携带明文；正文能力留在 owner
                        # cache/detail backend，seal 时独立读取并验证 HMAC。
                        body=(
                            self._request_only_content.get(
                                (session_id, checkpoint_ns, contribution_id)
                            )
                            if raw["content_hash"] is not None else None
                        ),
                        request_only=_manifest_sql_bool(
                            raw.get("request_only"), field="request_only"
                        ),
                        metadata=dict(metadata),
                        contribution_kind=_manifest_string(
                            raw.get("contribution_kind"), field="contribution_kind"
                        ),
                        content_length=(
                            _manifest_non_negative_int(
                                raw["content_length"], field="content_length"
                            )
                        ),
                        redacted_stable_digest=(
                            _manifest_string(
                                raw["redacted_stable_digest"],
                                field="redacted_stable_digest",
                            )
                            if raw.get("redacted_stable_digest") is not None
                            else None
                        ),
                        visibility=_manifest_string(
                            raw.get("visibility"), field="visibility"
                        ),
                        protection=_manifest_string(
                            raw.get("protection"), field="protection"
                        ),
                    )
                )
            loaded_ids = {item.contribution_id for item in loaded_contributions}
            missing_overlay_contributions = sorted(
                active_overlay_contribution_ids - loaded_ids
            )
            if missing_overlay_contributions:
                raise ValueError(
                    "plan-order-integrity: active overlay contribution manifest 缺失: "
                    + ",".join(missing_overlay_contributions)
                )
            # storage snapshot 是本次 plan 的唯一 registry 输入；清掉同一
            # session 内已失效的 in-memory overlay，避免重启前后映射集合不同。
            composer.ledger.reconcile_contributions(loaded_contributions)
        return composer.compose(
            session_id=session_id,
            plan_id=plan_id,
            refs=tuple(refs),
            tool_snapshot=tool_snapshot,
            history_view_revision=history_view_revision,
            source_overlay_epoch=source_overlay_epoch,
            compiler_version=compiler_version,
            active_view_id=active_view_id,
        )
