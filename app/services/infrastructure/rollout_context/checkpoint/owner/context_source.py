"""Saver-owned source overlay runtime owner。

source overlay 的持久化由 storage owner 完成；本 mixin 只负责把提交后的
overlay 正文接入当前 runtime 的 request-only namespace，；fork 直接复制持久化来源正文。
reconciliation ledger 保留在单独的 runtime
owner 中，避免 source registration 和 history revision 共用一份生命周期代码。
"""

from __future__ import annotations

from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.plans.contributions import ContextContribution, SourceBinding


class ContextSourceOverlayMixin:
    """管理 source overlay 的提交后 runtime 绑定。"""

    def register_source_overlay(
        self,
        overlay: object,
        *,
        session_id: str,
        thread_id: str,
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
        overlay_session_id = getattr(overlay, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("source overlay 缺少显式 session_id")
        if overlay_session_id != session_id:
            raise ValueError("source overlay SessionThread owner 不一致")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("source overlay 缺少显式 thread_id")
        if getattr(overlay, "thread_id", None) != thread_id:
            raise ValueError("source overlay SessionThread owner 不一致")
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
        self._storage.assemblies.register_source_overlay(
            overlay,
            session_id=session_id,
            thread_id=thread_id,
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
            contribution = ContextContribution(
                contribution_id=f"overlay:{overlay_id}:{role}",
                source_kind=f"overlay:{source_kind}",
                source_revision=role_source_revision,
                content_hash=content_hash,
                request_only=True,
                source_binding=SourceBinding(
                    source_ref=ref, overlay_ref=ref, overlay_id=overlay_id,
                    overlay_role=role, source_overlay_epoch=source_overlay_epoch,
                ),
                contribution_kind=f"overlay_{role}",
                body=content,
                content_length=content_length,
                visibility="internal",
                protection="public",
                # overlay base/delta 是其它显式 overlay selection ref 的 source
                # backing，不得自行成为独立 request-only candidate；role 由
                # typed selection_role 承载，不再写自由 selection_only flag。
                selection_role="backing_only",
                # source base/delta 是 post-user source 内容，恒为独立
                # user-role 数据，不得进入 system root。
                root_placement="tail_only",
            )
            self.register_context_contribution(
                session_id,
                thread_id,
                contribution,
                checkpoint_ns=checkpoint_ns,
                request_content=content,
            )
            with self._lock:
                self._request_only_content[
                    (session_id, thread_id, checkpoint_ns, ref)
                ] = content
        self.reconcile_context(
            session_id,
            thread_id=thread_id,
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

    def list_source_overlays(
        self,
        session_id: str,
        thread_id: str,
        *,
        source_overlay_epoch: int | None = None,
        checkpoint_ns: str = "",
    ) -> list[dict[str, object]]:
        return self._storage.assemblies.list_source_overlays(
            session_id,
            thread_id,
            source_overlay_epoch=source_overlay_epoch,
            checkpoint_ns=checkpoint_ns,
        )


__all__ = ["ContextSourceOverlayMixin"]
