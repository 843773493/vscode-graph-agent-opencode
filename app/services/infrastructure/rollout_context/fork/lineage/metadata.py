"""v2 fork lineage/retention metadata owner。

本模块只保存和查询 fork 的 source retention 与 lifecycle metadata；
不提供 v1 fallback，也不复制第二份 canonical 内容。
"""

from __future__ import annotations

from uuid import uuid4

from app.core.session_catalog_store import (
    ForkRetentionClaim,
    SessionCatalogStore,
)
from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.fork.validation import (
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)


class ForkMetadataMixin:
    """source retention claim 与生命周期 owner。"""

    def _catalog_store(self) -> SessionCatalogStore:
        """pinned claim 必须由当前 workspace 的唯一 catalog 承载。"""
        store = self._storage._path_resolver.catalog_store
        if not isinstance(store, SessionCatalogStore):
            raise TypeError("fork retention 缺少权威 workspace catalog store")
        return store

    def begin_pinned_fork_retention_claim(
        self,
        *,
        claim_id: str,
        source_session_id: str,
        target_session_id: str,
        source_lifecycle_generation: int,
    ) -> ForkRetentionClaim:
        """在 source capture 前建立 ``preparing`` pinned claim（8.1-D）。

        准入侧与整树删除在同一 workspace catalog 的 ``BEGIN IMMEDIATE`` 事务
        序列上竞争：source 已 deleting 时本调用零副作用失败。
        """
        store = self._catalog_store()
        return store.create_or_get_fork_retention_claim(
            claim_id=claim_id,
            workspace_id=self._workspace_id(),
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            source_lifecycle_generation=source_lifecycle_generation,
        )

    def activate_pinned_fork_retention_claim(
        self,
        claim_id: str,
        *,
        expected_generation: int,
    ) -> ForkRetentionClaim:
        """target ``target_committed`` 后把同一 claim CAS 为 ``active``（8.1-D）。

        只激活既有 claim，不首次补写；claim 缺失即 KeyError（fail closed）。
        """
        store = self._catalog_store()
        return store.activate_fork_retention_claim(
            claim_id, expected_generation=expected_generation
        )

    def _workspace_id(self) -> str:
        """从共享 resolver 取得 workspace_id（claim 行归属键）。"""
        resolver = self._storage._path_resolver
        workspace_id = getattr(resolver, "workspace_id", None)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise RuntimeError(
                "共享 path resolver 缺少 workspace_id，无法建立 pinned retention claim"
            )
        return workspace_id

    def _retain_fork_source(
        self,
        *,
        source_session_id: str,
        source_thread_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_id: str,
        owner_session_id: str,
        checkpoint_ns: str,
    ) -> None:
        source_session_id = required_text(source_session_id, field="source_session_id")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_id = required_text(fork_id, field="fork_id")
        owner_session_id = required_text(owner_session_id, field="owner_session_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork retention checkpoint_ns 必须是字符串")
        if source_view_id is None and source_checkpoint_id is None:
            raise RuntimeError("pinned fork retention 缺少真实 source view/checkpoint")
        if self._storage.fork_paths.source_snapshot(source_session_id) is not None:
            raise RuntimeError("fork retention 不能写入冻结 source snapshot")
        # 只消费 capture 前已建立的同一 claim；完成/恢复均禁止首次补写。
        claim = self._catalog_store().get_fork_retention_claim(fork_id)
        if (claim.workspace_id, claim.source_session_id, claim.target_session_id) != (
            self._workspace_id(), source_session_id, owner_session_id,
        ):
            raise RuntimeError("fork retention claim 与 source/target scope 不一致")
        if self._source_lifecycle_generation(source_session_id) != claim.source_lifecycle_generation:
            raise RuntimeError("fork retention source lifecycle generation 已漂移")
        if claim.state not in {"preparing", "active"}:
            raise RuntimeError("fork retention 已 released，禁止迟到 activation")
        source_owner_thread_id = self._storage.fork_paths.thread_identity(source_session_id, source_thread_id)
        index = self._storage.index_path(source_session_id, checkpoint_ns, thread_id=source_owner_thread_id)
        if not index.is_file():
            raise RuntimeError(f"fork retention source index 缺失: {index}")
        with (
            self._storage._lock(source_owner_thread_id, checkpoint_ns, session_id=source_session_id),
            self._storage._connect(source_owner_thread_id, checkpoint_ns, session_id=source_session_id) as connection,
        ):
            self._storage._require_v2_runtime(connection)
            existing = connection.execute(
                "SELECT owner_session_id, status, target_view_id FROM retention_refs WHERE reference_kind = 'fork' AND reference_id = ?",
                (fork_id,),
            ).fetchall()
            if existing and existing != [(owner_session_id, "active", source_view_id)]:
                raise RuntimeError("fork retention source ref 与冻结 target/view 不一致")
            if not existing:
                cursor = connection.execute(
                    "INSERT INTO retention_refs(retention_id, reference_kind, reference_id, target_view_id, target_message_sequence, owner_session_id, expires_at, status, created_at) VALUES (?, 'fork', ?, ?, NULL, ?, NULL, 'active', ?)",
                    (uuid4().hex, fork_id, source_view_id, owner_session_id, _now()),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"fork retention 未写入: {fork_id}")
        self.activate_pinned_fork_retention_claim(
            fork_id, expected_generation=claim.source_lifecycle_generation,
        )

    def _source_lifecycle_generation(self, session_id: str) -> int:
        """读取 source session-control fence generation（claim 准入冻结值）。

        控制库或 fence 缺失 → fail closed（claim 的 generation 必须来自真实
        durable fence，绝不用虚假默认值）。
        """
        from app.core.session_control_store import SessionControlStore

        session_dir = self._storage._path_resolver.resolve_session_node(session_id)
        control_path = session_dir / "session-control.sqlite"
        if not control_path.is_file():
            raise RuntimeError(
                "source session 缺少 session-control.sqlite，无法读取 "
                f"lifecycle generation（fail closed）: session_id={session_id}"
            )
        control = SessionControlStore(control_path, read_only=True)
        try:
            state, generation = control.get_fence()
            if state != "active":
                from app.core.session_lifecycle_gate import SessionDeletionPendingError

                raise SessionDeletionPendingError("session_deletion_pending: fork retention source fence 已关闭")
        finally:
            control.close()
        return int(generation)

    def pinned_fork_children(
        self, source_session_id: str, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        source_session_id = required_text(source_session_id, field="source_session_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("pinned_fork_children.checkpoint_ns 必须是字符串")
        source_owner_thread_id = self._storage.main_thread_id(source_session_id)
        root = self._storage.root(
            source_session_id, checkpoint_ns, thread_id=source_owner_thread_id
        )
        if self._storage._is_removed_rollout_layout(root):
            # 旧 rollout 没有新 schema 的 retention_refs，删除整棵会话目录时
            # 不应触发 initialize()，否则用户无法清理原型阶段遗留的会话。
            return ()
        try:
            with self._storage.read_connection(source_session_id, source_owner_thread_id, checkpoint_ns) as connection:
                self._storage._require_v2_runtime(connection)
                rows = connection.execute(
                    "SELECT DISTINCT owner_session_id FROM retention_refs WHERE reference_kind = 'fork' AND status = 'active' AND owner_session_id IS NOT NULL ORDER BY owner_session_id"
                ).fetchall()
        except RolloutNotMaterializedError:
            return ()
        return tuple(
            required_text(row[0], field="retention_refs.owner_session_id")
            for row in rows
        )
