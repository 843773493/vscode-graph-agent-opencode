"""Saver 侧的 activation snapshot 持久化 port（9.3）。

本 mixin 只把 coordinator 冻结的内存 snapshot 交给唯一 storage owner，并在
``assembly_sealed`` 事务里原子绑定 activation snapshot 与 assembly 的
selection/plan hash，同时提供幂等重试的只读核对。它不做 stat/scan/read/HTTP
fetch/memory-provider lookup，也不回退当前 Registry。

未注入 ``ResourceActivationStore`` 时（尚未接入 activation 的部署/测试），
``_resource_activation_store`` 为 None，seal 行为与接入前完全一致。
"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    ResourceActivationStore,
)


class _ActivationBinding:
    """assembly 事务内的 activation 绑定参与者（唯一 Saver 准备）。"""

    def __init__(
        self,
        *,
        store: ResourceActivationStore,
        snapshot: ResourceActivationSnapshotRef,
        lineage_detail_ref: DetailRef,
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._lineage_detail_ref = lineage_detail_ref

    @property
    def snapshot(self) -> ResourceActivationSnapshotRef:
        return self._snapshot

    def bind_assembly(
        self,
        connection: sqlite3.Connection,
        *,
        assembly_id: str,
        plan_id: str,
        plan_hash: str,
        request_hash: str,
        selection_manifest_hash: str,
    ) -> None:
        self._store.persist_snapshot(
            connection,
            self._snapshot,
            lineage_detail_ref=self._lineage_detail_ref,
        )
        self._store.bind_assembly(
            connection,
            snapshot=self._snapshot,
            assembly_id=assembly_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            request_hash=request_hash,
            selection_manifest_hash=selection_manifest_hash,
        )


class ResourceActivationOwnerMixin:
    """Saver 对 activation catalog 的 owner 边界。"""

    #: 未注入时为 None；由 ``attach_resource_activation_store`` 或测试显式赋予。
    _resource_activation_store: ResourceActivationStore | None = None

    def attach_resource_activation_store(self, store: ResourceActivationStore) -> None:
        """显式注入 activation catalog owner；不在构造期隐式建 schema。"""

        if not isinstance(store, ResourceActivationStore):
            raise TypeError(
                "attach_resource_activation_store 需要 ResourceActivationStore"
            )
        self._resource_activation_store = store

    @property
    def supports_resource_activation(self) -> bool:
        return self._resource_activation_store is not None

    def _activation_store(self) -> ResourceActivationStore:
        store = self._resource_activation_store
        if store is None:
            raise RuntimeError(
                "resource-activation-schema-unavailable: Saver 未注入 "
                "ResourceActivationStore"
            )
        return store

    def bootstrap_resource_activation_schema(
        self, session_id: str, *, checkpoint_ns: str = ""
    ) -> None:
        """显式为全新会话库建立 activation schema；普通 seal 路径不得调用。"""

        store = self._activation_store()
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._storage._lock(
            session_id, checkpoint_ns
        ), self._storage._connect(session_id, checkpoint_ns) as connection:
            self._storage._require_v2_runtime(connection)
            connection.execute("BEGIN IMMEDIATE")
            store.bootstrap_schema(connection)
            connection.commit()

    def prepare_resource_activation_binding(
        self,
        snapshot: ResourceActivationSnapshotRef,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[DetailRef, _ActivationBinding]:
        """在 seal 前准备 activation 正文与事务绑定闭包。

        返回 (lineage_detail_ref, binding)；binding 必须在 ``assembly_sealed``
        事务内以 SQLite connection 调用一次，使 activation 行与 assembly 行
        原子提交。
        """

        if not isinstance(snapshot, ResourceActivationSnapshotRef):
            raise TypeError(
                "prepare_resource_activation_binding 需要 domain "
                "ResourceActivationSnapshotRef"
            )
        store = self._activation_store()
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        lineage_detail_ref = store.prepare_lineage(
            snapshot, checkpoint_ns=checkpoint_ns
        )
        return lineage_detail_ref, _ActivationBinding(
            store=store,
            snapshot=snapshot,
            lineage_detail_ref=lineage_detail_ref,
        )

    async def save_resource_activation_snapshot(
        self, snapshot: ResourceActivationSnapshotRef
    ) -> None:
        """独立事务保存；seal 路径应改用 prepare + 原子绑定。"""

        self._activation_store().save_snapshot(snapshot)

    def read_resource_activation_snapshot(
        self,
        session_id: str,
        *,
        thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        return self._activation_store().read_snapshot(
            session_id,
            thread_id=thread_id,
            activation_snapshot_id=activation_snapshot_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def read_assembly_activation_binding(
        self, session_id: str, *, assembly_id: str, checkpoint_ns: str = ""
    ):
        return self._activation_store().read_assembly_binding(
            session_id,
            assembly_id=assembly_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def verify_resource_activation_binding(
        self,
        session_id: str,
        *,
        assembly_id: str,
        activation_snapshot: ResourceActivationSnapshotRef,
        checkpoint_ns: str = "",
    ) -> None:
        """幂等重试：只读核对已提交 assembly 的 activation 绑定，不写任何行。"""

        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._storage._connect(
            session_id, checkpoint_ns, read_only=True
        ) as connection:
            self._storage._require_v2_runtime(connection)
            self._activation_store().require_schema(connection)
            row = connection.execute(
                "SELECT session_id, thread_id, activation_snapshot_id, "
                "bindings_hash, activation_provenance_hash "
                "FROM resource_activation_assembly_bindings WHERE assembly_id = ?",
                (assembly_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "resource-activation-unavailable: 已提交 assembly 缺少 activation "
                f"绑定: {assembly_id}"
            )
        expected = (
            activation_snapshot.owner_session_id,
            activation_snapshot.owner_thread_id,
            activation_snapshot.activation_snapshot_id,
            activation_snapshot.bindings_hash,
            activation_snapshot.activation_provenance_hash,
        )
        if tuple(row) != expected:
            raise RuntimeError(
                "resource-activation-snapshot-conflict: 已提交 assembly 的 "
                f"activation 绑定与重试 snapshot 不一致: {assembly_id}"
            )


__all__ = ["ResourceActivationOwnerMixin"]
