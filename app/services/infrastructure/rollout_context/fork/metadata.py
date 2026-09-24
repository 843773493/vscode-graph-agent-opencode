"""v2 fork lineage/retention metadata owner。

本模块只保存和查询 fork 的审计、source retention 与 target identity metadata；
不提供 v1 fallback，也不复制第二份 canonical 内容。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.core.session_catalog_store import (
    ForkRetentionClaim,
    SessionCatalogStore,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    json_mapping,
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ForkMetadataMixin:
    """fork identity mapping、lineage 和 source retention owner。"""

    def _catalog_store(self) -> SessionCatalogStore | None:
        """返回共享 workspace catalog store（pinned claim 的唯一权威载体）。

        crawler 侧通过 path resolver 取得 store；resolver 缺失（低层存储单元
        测试无 catalog 装配）时返回 None，claim 生命周期整体跳过。
        """
        resolver = getattr(self, "_path_resolver", None)
        store = getattr(resolver, "catalog_store", None)
        return store if isinstance(store, SessionCatalogStore) else None

    def begin_pinned_fork_retention_claim(
        self,
        *,
        claim_id: str,
        source_session_id: str,
        target_session_id: str,
        source_lifecycle_generation: int,
    ) -> ForkRetentionClaim | None:
        """在 source capture 前建立 ``preparing`` pinned claim（8.1-D）。

        准入侧与整树删除在同一 workspace catalog 的 ``BEGIN IMMEDIATE`` 事务
        序列上竞争：source 已 deleting 时本调用零副作用失败。返回 None 表示
        当前装配无 catalog（claim 生命周期不适用）。
        """
        store = self._catalog_store()
        if store is None:
            return None
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
    ) -> ForkRetentionClaim | None:
        """target ``target_committed`` 后把同一 claim CAS 为 ``active``（8.1-D）。

        只激活既有 claim，不首次补写；claim 缺失即 KeyError（fail closed）。
        """
        store = self._catalog_store()
        if store is None:
            return None
        return store.activate_fork_retention_claim(
            claim_id, expected_generation=expected_generation
        )

    def _workspace_id(self) -> str:
        """从共享 resolver 取得 workspace_id（claim 行归属键）。"""
        resolver = getattr(self, "_path_resolver", None)
        workspace_id = getattr(resolver, "workspace_id", None)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise RuntimeError(
                "共享 path resolver 缺少 workspace_id，无法建立 pinned "
                "retention claim"
            )
        return workspace_id

    def list_fork_identity_mappings(
        self,
        target_thread_id: str,
        *,
        fork_id: str | None = None,
        entity_type: str | None = None,
        checkpoint_ns: str = "",
    ) -> list[dict[str, object]]:
        """读取跨 session fork 的复合 identity mapping 审计记录。"""
        target_thread_id = required_text(
            target_thread_id, field="identity_mappings.target_session_id"
        )
        fork_id = optional_text(fork_id, field="identity_mappings.fork_id")
        entity_type = optional_text(entity_type, field="identity_mappings.entity_type")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("identity_mappings.checkpoint_ns 必须是字符串")
        self.initialize(target_thread_id, checkpoint_ns)
        query = "SELECT mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at FROM fork_identity_mappings WHERE target_session_id = ?"
        params: list[object] = [target_thread_id]
        if fork_id is not None:
            query += " AND fork_id = ?"
            params.append(fork_id)
        if entity_type is not None:
            query += " AND entity_type = ?"
            params.append(entity_type)
        query += " ORDER BY created_at, mapping_id"
        with self._connect(
            target_thread_id, checkpoint_ns, read_only=True
        ) as connection:
            self._require_v2_runtime(connection)
            rows = connection.execute(query, tuple(params)).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            if len(row) != 11:
                raise RuntimeError("fork_identity_mappings 行字段数非法")
            (
                mapping_id,
                stored_fork_id,
                source_session_id,
                target_session_id,
                stored_entity_type,
                source_local_id,
                target_local_id,
                source_offset,
                target_offset,
                lineage_json,
                created_at,
            ) = row
            result.append(
                {
                    "mapping_id": required_text(mapping_id, field="mapping_id"),
                    "fork_id": required_text(stored_fork_id, field="fork_id"),
                    "source_session_id": required_text(
                        source_session_id, field="source_session_id"
                    ),
                    "target_session_id": required_text(
                        target_session_id, field="target_session_id"
                    ),
                    "entity_type": required_text(
                        stored_entity_type, field="entity_type"
                    ),
                    "source_local_id": required_text(
                        source_local_id, field="source_local_id"
                    ),
                    "target_local_id": required_text(
                        target_local_id, field="target_local_id"
                    ),
                    "source_offset": (
                        non_negative_int(source_offset, field="source_offset")
                        if source_offset is not None
                        else None
                    ),
                    "target_offset": (
                        non_negative_int(target_offset, field="target_offset")
                        if target_offset is not None
                        else None
                    ),
                    "lineage_json": required_text(lineage_json, field="lineage_json"),
                    "created_at": required_text(created_at, field="created_at"),
                }
            )
            json_mapping(lineage_json, field="lineage_json")
        return result

    def fork_target_identity(
        self,
        target_thread_id: str,
        *,
        fork_id: str,
        source_session_id: str,
        entity_type: str,
        source_local_id: str,
        checkpoint_ns: str = "",
    ) -> str | None:
        """解析一次 fork 的 target-local identity，不扫描 source rollout。"""
        target_thread_id = required_text(
            target_thread_id, field="fork_target_identity.target_session_id"
        )
        fork_id = required_text(fork_id, field="fork_target_identity.fork_id")
        source_session_id = required_text(
            source_session_id, field="fork_target_identity.source_session_id"
        )
        entity_type = required_text(
            entity_type, field="fork_target_identity.entity_type"
        )
        source_local_id = required_text(
            source_local_id, field="fork_target_identity.source_local_id"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork_target_identity.checkpoint_ns 必须是字符串")
        self.initialize(target_thread_id, checkpoint_ns)
        with self._connect(
            target_thread_id, checkpoint_ns, read_only=True
        ) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id = ? AND source_session_id = ? AND entity_type = ? AND source_local_id = ?",
                (fork_id, source_session_id, entity_type, source_local_id),
            ).fetchone()
        return (
            required_text(row[0], field="fork_identity_mappings.target_local_id")
            if row is not None
            else None
        )

    def record_fork_origin(
        self,
        *,
        target_thread_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str = "detached",
        checkpoint_ns: str = "",
    ) -> str:
        target_thread_id = required_text(
            target_thread_id, field="fork_origin.target_session_id"
        )
        source_session_id = required_text(
            source_session_id, field="fork_origin.source_session_id"
        )
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="fork_origin.source_checkpoint_id"
        )
        source_view_id = optional_text(
            source_view_id, field="fork_origin.source_view_id"
        )
        fork_mode = one_of_text(
            fork_mode,
            {"context_fork", "history_prefix_fork", "full_rollout_copy"},
            field="fork_origin.fork_mode",
        )
        relationship = one_of_text(
            relationship, {"detached", "pinned"}, field="fork_origin.relationship"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork_origin.checkpoint_ns 必须是字符串")
        if source_view_id is None:
            source_view_id = self._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
            )
        fork_id = uuid4().hex
        with self._lock(target_thread_id, checkpoint_ns):
            self.initialize(target_thread_id, checkpoint_ns)
            with self._connect(target_thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                cursor = connection.execute(
                    "INSERT INTO fork_origins(fork_id, child_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, copied_message_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COUNT(*) FROM messages), ?)",
                    (
                        fork_id,
                        target_thread_id,
                        source_session_id,
                        source_checkpoint_id,
                        source_view_id,
                        fork_mode,
                        relationship,
                        _now(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"fork origin 未写入: {fork_id}")
        if relationship == "pinned":
            self._retain_fork_source(
                source_session_id=source_session_id,
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                owner_session_id=target_thread_id,
                checkpoint_ns=checkpoint_ns,
            )
        return fork_id

    def _fork_source_view_id(
        self,
        source_session_id: str,
        source_checkpoint_id: str | None,
        checkpoint_ns: str,
    ) -> str | None:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        source_session_id = required_text(source_session_id, field="source_session_id")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork source checkpoint_ns 必须是字符串")
        source_root = self.root(source_session_id, checkpoint_ns)
        if not source_root.is_dir():
            return None
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                row = (
                    connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_checkpoint_id
                    else connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                )
                if source_checkpoint_id is not None and row is None:
                    raise KeyError(source_checkpoint_id)
                return (
                    required_text(row[0], field="fork source view_id")
                    if row is not None and row[0] is not None
                    else None
                )
        finally:
            source_lock.release()

    def _retain_fork_source(
        self,
        *,
        source_session_id: str,
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
            return
        # 8.1-D：pinned fork 的 durable retention admit 同时写 workspace catalog
        # claim（与整树删除竞争同一 DB 写事务序列）。create-or-get 幂等，崩溃
        # 重入不重复建 claim；source 已 deleting 时此处零副作用失败。
        if self._catalog_store() is not None:
            fence = self._source_lifecycle_generation(source_session_id)
            self.begin_pinned_fork_retention_claim(
                claim_id=fork_id,
                source_session_id=source_session_id,
                target_session_id=owner_session_id,
                source_lifecycle_generation=fence,
            )
            self.activate_pinned_fork_retention_claim(
                fork_id, expected_generation=fence
            )
        with self._lock(source_session_id, checkpoint_ns):
            self.initialize(source_session_id, checkpoint_ns)
            with self._connect(source_session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT 1 FROM retention_refs WHERE reference_kind = 'fork' AND reference_id = ? AND owner_session_id = ? AND status = 'active' LIMIT 1",
                    (fork_id, owner_session_id),
                ).fetchone()
                if existing is not None:
                    return
                now = _now()
                cursor = connection.execute(
                    "INSERT INTO retention_refs(retention_id, reference_kind, reference_id, target_view_id, target_message_sequence, owner_session_id, expires_at, status, created_at) VALUES (?, 'fork', ?, ?, NULL, ?, NULL, 'active', ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        source_view_id,
                        owner_session_id,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"fork retention 未写入: {fork_id}")

    def _source_lifecycle_generation(self, session_id: str) -> int:
        """读取 source session-control fence generation（claim 准入冻结值）。

        控制库或 fence 缺失 → fail closed（claim 的 generation 必须来自真实
        durable fence，绝不用虚假默认值）。
        """
        from app.core.session_control_store import SessionControlStore

        session_dir = self.root(session_id).parent
        control_path = session_dir / "session-control.sqlite"
        if not control_path.is_file():
            raise RuntimeError(
                "source session 缺少 session-control.sqlite，无法读取 "
                f"lifecycle generation（fail closed）: session_id={session_id}"
            )
        control = SessionControlStore(control_path)
        try:
            _state, generation = control.get_fence()
        finally:
            control.close()
        return int(generation)

    def release_fork_retentions(self, child_session_id: str) -> None:
        """释放 child（target）引用的全部 source retention 与 pinned claim。"""
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        # 8.1-D：pinned claim 住在 workspace catalog（nodes 同库），与 rollout
        # 的 retention_refs 是**同一** retention 事实的两种投影：catalog 侧
        # claim 承担删除准入 blocker，rollout retention_refs 承担 checkpoint
        # pruning 保护。两者必须一起释放，否则删除准入会永久卡住。
        catalog_store = self._catalog_store()
        if catalog_store is not None:
            catalog_store.release_fork_retention_claims_for_target(
                child_session_id, "target session 删除"
            )

        child_session_id = required_text(child_session_id, field="child_session_id")
        child_root = self.root(child_session_id)
        if (
            not child_root.is_dir()
            or self._is_removed_rollout_layout(child_root)
            or not self.index_path(child_session_id).is_file()
        ):
            # 删除旧 rollout 时没有可读取的 pinned provenance；整个会话目录
            # 会由 SessionService 随后删除，不能在此处初始化旧布局。
            return
        child_lock = _RolloutFileLock(
            child_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        child_lock.acquire()
        try:
            with self._connect(child_session_id) as connection:
                self._require_v2_runtime(connection)
                origins = connection.execute(
                    "SELECT fork_id, source_session_id FROM fork_origins WHERE child_session_id = ? AND relationship = 'pinned'",
                    (child_session_id,),
                ).fetchall()
        finally:
            child_lock.release()
        for raw_fork_id, raw_source_session_id in origins:
            fork_id = required_text(raw_fork_id, field="fork_origins.fork_id")
            source_session_id = required_text(
                raw_source_session_id, field="fork_origins.source_session_id"
            )
            source_root = self.root(source_session_id)
            if (
                not source_root.is_dir()
                or self._is_removed_rollout_layout(source_root)
                or not self.index_path(source_session_id).is_file()
            ):
                continue
            with (
                self._lock(source_session_id, ""),
                self._connect(source_session_id) as connection,
            ):
                self._require_v2_runtime(connection)
                cursor = connection.execute(
                    "UPDATE retention_refs SET status = 'released' WHERE reference_kind = 'fork' AND reference_id = ? AND owner_session_id = ? AND status = 'active'",
                    (fork_id, child_session_id),
                )
                if cursor.rowcount < 0:
                    raise RuntimeError("fork retention 释放影响行数非法")

    def pinned_fork_children(
        self, source_thread_id: str, checkpoint_ns: str = ""
    ) -> tuple[str, ...]:
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("pinned_fork_children.checkpoint_ns 必须是字符串")
        root = self.root(source_thread_id, checkpoint_ns)
        if self._is_removed_rollout_layout(root):
            # 旧 rollout 没有新 schema 的 retention_refs，删除整棵会话目录时
            # 不应触发 initialize()，否则用户无法清理原型阶段遗留的会话。
            return ()
        self.initialize(source_thread_id, checkpoint_ns)
        with self._connect(source_thread_id, checkpoint_ns) as connection:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT DISTINCT owner_session_id FROM retention_refs WHERE reference_kind = 'fork' AND status = 'active' AND owner_session_id IS NOT NULL ORDER BY owner_session_id"
            ).fetchall()
        return tuple(
            required_text(row[0], field="retention_refs.owner_session_id")
            for row in rows
        )

    def list_thread_ids(self) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in self._path_resolver.list_nodes()
            if node.kind == "session"
        )
