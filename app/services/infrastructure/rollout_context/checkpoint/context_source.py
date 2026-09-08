"""Saver-owned source overlay runtime owner。

source overlay 的持久化由 storage owner 完成；本 mixin 只负责把提交后的
overlay 正文接入当前 runtime 的 request-only namespace，以及 fork 时复制
已经存在于当前进程的正文能力。reconciliation ledger 保留在单独的 runtime
owner 中，避免 source registration 和 history revision 共用一份生命周期代码。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.request_plan import ContextContribution
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


class ContextSourceOverlayMixin:
    """管理 source overlay 的提交后 runtime 绑定。"""

    def register_source_overlay(
        self,
        overlay: object,
        *,
        checkpoint_ns: str = "",
        base_content: object | None = None,
        delta_content: object | None = None,
    ) -> None:
        if not isinstance(checkpoint_ns, str):
            raise TypeError("source overlay checkpoint namespace 必须是字符串")
        overlay_checkpoint_ns = getattr(overlay, "checkpoint_ns", "")
        if not isinstance(overlay_checkpoint_ns, str):
            raise TypeError("source overlay checkpoint namespace 必须是字符串")
        if overlay_checkpoint_ns != checkpoint_ns:
            raise ValueError("source overlay checkpoint namespace 不一致")
        session_id = getattr(overlay, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("source overlay 缺少 session_id")
        overlay_id = getattr(overlay, "overlay_id", None)
        source_kind = getattr(overlay, "source_kind", None)
        source_revision = getattr(overlay, "source_revision", None)
        source_overlay_epoch = getattr(overlay, "source_overlay_epoch", None)
        if not isinstance(overlay_id, str) or not overlay_id:
            raise ValueError("source overlay 缺少 overlay_id")
        if not isinstance(source_kind, str) or not source_kind:
            raise ValueError("source overlay 缺少 source_kind")
        if not isinstance(source_revision, str) or not source_revision:
            raise ValueError("source overlay 缺少 source_revision")
        if (
            not isinstance(source_overlay_epoch, int)
            or isinstance(source_overlay_epoch, bool)
            or source_overlay_epoch < 0
        ):
            raise ValueError("source overlay source_overlay_epoch 必须是非负整数")
        materializes_overlay_id = getattr(overlay, "materializes_overlay_id", None)
        if materializes_overlay_id is not None and (
            not isinstance(materializes_overlay_id, str) or not materializes_overlay_id
        ):
            raise ValueError(
                "source overlay materializes_overlay_id 必须是非空字符串或 null"
            )
        base_ref = getattr(overlay, "base_ref", None)
        delta_ref = getattr(overlay, "delta_ref", None)
        if base_ref is not None and base_ref == delta_ref:
            raise ValueError("source overlay base_ref 与 delta_ref 不能复用同一 ref")
        # storage 在同一 owner boundary 计算并持久化 base/delta manifest；
        # 内存正文只作为本次注册时的校验输入，不成为重启后的第二事实源。
        self._storage.register_source_overlay(
            overlay,
            base_content=base_content,
            delta_content=delta_content,
        )
        # 只有 manifest 已提交成功后才更新实时正文缓存，避免 storage
        # 拒绝覆盖/哈希冲突时留下无法与 registry 对齐的内存事实。
        for role, ref, content in (
            ("base", base_ref, base_content),
            ("delta", delta_ref, delta_content),
        ):
            if content is None:
                continue
            if not isinstance(ref, str) or not ref:
                raise ValueError("source overlay content 必须绑定非空 ref")
            role_source_revision = getattr(overlay, f"{role}_source_revision", None)
            content_length = getattr(overlay, f"{role}_content_length", None)
            content_hash = getattr(overlay, f"{role}_content_hash", None)
            redacted_digest = getattr(overlay, f"{role}_redacted_stable_digest", None)
            # storage 会在正文输入时规范化 manifest，但不反向修改调用方的
            # overlay 对象。这里必须从同一 canonical preimage 得到 runtime
            # contribution manifest，否则“持久化已成功、内存 registry 却
            # 缺少 content_length”会把合法 source 变成不可 dispatch 的假丢失。
            if content_length is None:
                content_length = len(canonical_json_bytes(content))
            elif (
                not isinstance(content_length, int)
                or isinstance(content_length, bool)
                or content_length < 0
            ):
                raise ValueError(
                    f"source overlay {role} content_length 必须是非负整数或 null: {ref}"
                )
            if content_hash is None:
                content_hash = contribution_content_hash(
                    f"overlay_{role}",
                    content,
                )
            elif not isinstance(content_hash, str) or not content_hash:
                raise ValueError(
                    f"source overlay {role} content_hash 必须是非空字符串或 null: {ref}"
                )
            if role_source_revision is None:
                role_source_revision = source_revision
            elif not isinstance(role_source_revision, str) or not role_source_revision:
                raise ValueError(
                    f"source overlay {role} source_revision 必须是非空字符串或 null: {ref}"
                )
            if redacted_digest is not None and (
                not isinstance(redacted_digest, str) or not redacted_digest
            ):
                raise ValueError(
                    f"source overlay {role} redacted digest 必须是非空字符串或 null: {ref}"
                )
            contribution = ContextContribution(
                contribution_id=f"overlay:{overlay_id}:{role}",
                source_kind=f"overlay:{source_kind}",
                source_revision=role_source_revision,
                content_hash=content_hash,
                redacted_stable_digest=redacted_digest,
                request_only=True,
                metadata={
                    "source_ref": ref,
                    "overlay_ref": ref,
                    "overlay_id": overlay_id,
                    "overlay_role": role,
                    "source_overlay_epoch": source_overlay_epoch,
                    "selection_only": True,
                },
                contribution_kind=f"overlay_{role}",
                body=content,
                content_length=content_length,
                visibility="internal",
                protection="public",
            )
            self.register_context_contribution(
                session_id,
                contribution,
                checkpoint_ns=checkpoint_ns,
                request_content=content,
            )
            with self._lock:
                self._request_only_content[(session_id, checkpoint_ns, ref)] = content
        self.reconcile_context(
            session_id,
            operation="source_edit",
            checkpoint_ns=checkpoint_ns,
            delta_ref=(
                getattr(overlay, "delta_ref", None)
                if materializes_overlay_id is None
                else getattr(overlay, "base_ref", None)
            ),
            source_overlay_epoch=source_overlay_epoch,
            source_revision=source_revision,
            materialize_overlay=materializes_overlay_id is not None,
        )

    def _copy_request_only_context_for_fork(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        checkpoint_ns: str,
    ) -> None:
        """把当前进程可用的 overlay body 绑定到 target-local ref。"""
        mappings = self._storage.list_fork_identity_mappings(
            target_session_id,
            fork_id=fork_id,
            entity_type="source_overlay",
            checkpoint_ns=checkpoint_ns,
        )
        source_contributions = self._storage.list_context_contributions(
            source_session_id,
            checkpoint_ns=checkpoint_ns,
        )
        target_contribution_ids = {
            strict_text(
                row["contribution_id"],
                field="context_contributions.contribution_id",
            )
            for row in self._storage.list_context_contributions(
                target_session_id,
                checkpoint_ns=checkpoint_ns,
            )
        }
        with self._lock:
            for row in source_contributions:
                contribution_id = strict_text(
                    row["contribution_id"],
                    field="context_contributions.contribution_id",
                )
                if contribution_id not in target_contribution_ids:
                    continue
                source_key = (source_session_id, checkpoint_ns, contribution_id)
                target_key = (target_session_id, checkpoint_ns, contribution_id)
                if source_key in self._request_only_content:
                    self._request_only_content[target_key] = self._request_only_content[
                        source_key
                    ]
            for mapping in mappings:
                lineage_json = strict_text(
                    mapping.get("lineage_json"),
                    field="fork_identity_mappings.lineage_json",
                )
                try:
                    lineage = json.loads(lineage_json)
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        "fork source overlay lineage JSON 非法: "
                        f"{mapping['source_local_id']}"
                    ) from error
                if not isinstance(lineage, Mapping):
                    raise TypeError("fork source overlay lineage 必须是 object")
                source = lineage.get("source")
                target = lineage.get("target")
                if not isinstance(source, Mapping) or not isinstance(target, Mapping):
                    raise TypeError("fork source overlay lineage 缺少 source/target")
                for field_name in ("base_ref", "delta_ref"):
                    source_ref = source.get(field_name)
                    target_ref = target.get(field_name)
                    source_ref = strict_optional_text(
                        source_ref,
                        field=f"fork overlay lineage.source.{field_name}",
                    )
                    target_ref = strict_optional_text(
                        target_ref,
                        field=f"fork overlay lineage.target.{field_name}",
                    )
                    if (
                        source_ref is not None
                        and target_ref is not None
                        and (
                            source_session_id,
                            checkpoint_ns,
                            source_ref,
                        )
                        in self._request_only_content
                    ):
                        self._request_only_content[
                            (target_session_id, checkpoint_ns, target_ref)
                        ] = self._request_only_content[
                            (source_session_id, checkpoint_ns, source_ref)
                        ]
                # full_rollout_copy 使用 preserved local overlay mapping，老
                # 版本 lineage 将 ref 放在顶层；读取它可保持同进程副本可用。
                for source_key, target_key in (
                    ("source_base_ref", "target_base_ref"),
                    ("source_delta_ref", "target_delta_ref"),
                ):
                    source_ref = lineage.get(source_key)
                    target_ref = lineage.get(target_key)
                    source_ref = strict_optional_text(
                        source_ref,
                        field=f"fork overlay lineage.{source_key}",
                    )
                    target_ref = strict_optional_text(
                        target_ref,
                        field=f"fork overlay lineage.{target_key}",
                    )
                    if (
                        source_ref is not None
                        and target_ref is not None
                        and (
                            source_session_id,
                            checkpoint_ns,
                            source_ref,
                        )
                        in self._request_only_content
                    ):
                        self._request_only_content[
                            (target_session_id, checkpoint_ns, target_ref)
                        ] = self._request_only_content[
                            (source_session_id, checkpoint_ns, source_ref)
                        ]

    def list_source_overlays(
        self,
        session_id: str,
        *,
        source_overlay_epoch: int | None = None,
        checkpoint_ns: str = "",
    ) -> list[dict[str, object]]:
        return self._storage.list_source_overlays(
            session_id,
            source_overlay_epoch=source_overlay_epoch,
            checkpoint_ns=checkpoint_ns,
        )


__all__ = ["ContextSourceOverlayMixin"]
