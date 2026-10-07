"""Saver 侧的 activation snapshot 持久化 port（9.3）。

本 mixin 只把 coordinator 冻结的内存 snapshot 交给唯一 storage owner，并在
``assembly_sealed`` 事务里原子绑定 activation snapshot 与 assembly 的
selection/plan hash，同时提供幂等重试的只读核对。它不做 stat/scan/read/HTTP
fetch/memory-provider lookup，也不回退当前 Registry。

每个 Saver 始终持有基于同一 storage/detail store 的唯一 activation store。
"""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.domain.itemized.records import parse_item_projection_manifest
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
    extension_dispatch_from_activation,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_body import (
    ResourceActivationBodyStore,
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

    _resource_activation_store: ResourceActivationStore

    def _activation_store(self) -> ResourceActivationStore:
        return self._resource_activation_store

    def read_resource_activation_payload(
        self, snapshot: ResourceActivationSnapshotRef, binding: ResourceProvenanceRef, *, checkpoint_ns: str = "",
    ) -> object:
        """正文读取复用同一 storage/detail owner，不读当前 Registry 或来源。"""
        return ResourceActivationBodyStore(self._storage, self._detail_store).read_payload(
            snapshot=snapshot, binding=binding,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def load_tool_call_resource_activation(
        self, session_id: str, *, thread_id: str, tool_call_id: str,
        model_call_id: str | None = None, checkpoint_ns: str = "",
    ) -> tuple[ResourceActivationSnapshotRef, str]:
        """tool_call 只按 canonical assistant provenance 定位原 model-call/assembly。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._storage._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self._storage._require_v2_runtime(connection)
            # raw ID 必须带 emitting assistant 的 model-call scope；重启读取
            # 可直接用精确持久 ID。归一只消费同一 canonical typed owner。
            call_rows = connection.execute(
                "SELECT t.tool_call_id, i.projection_manifest_json "
                "FROM tool_calls t JOIN messages m "
                "ON m.message_sequence = t.assistant_message_sequence "
                "JOIN item_catalog i ON i.jsonl_offset = m.jsonl_offset "
                "AND i.jsonl_length = m.jsonl_length "
                "WHERE m.role = 'assistant' AND "
                "((? IS NULL AND t.tool_call_id = ?) OR "
                "json_extract(i.projection_manifest_json, "
                "'$.projection_identity.model_call_id') = ?)",
                (model_call_id, tool_call_id, model_call_id),
            ).fetchall()
            model_call_ids: set[str] = set()
            for stored_call_id, raw_manifest in call_rows:
                manifest = parse_item_projection_manifest(json.loads(raw_manifest))
                projection = manifest["projection_identity"]
                identity = (
                    ItemProjectionIdentity.from_dict(projection)
                    if projection is not None else None
                )
                if identity is None or identity.model_call_id is None:
                    if stored_call_id == tool_call_id:
                        raise ExtensionCatalogUnavailableError(
                            f"tool_call 缺少 typed model_call provenance: {tool_call_id}"
                        )
                    continue
                raw_call_id = provider_tool_call_id(identity, tool_call_id)
                if not raw_call_id or (model_call_id is None and raw_call_id == tool_call_id):
                    raise ExtensionCatalogUnavailableError(
                        "tool_call 无 model-call scope 时必须提供与 canonical typed owner "
                        f"一致的非空 persisted scoped ID: {tool_call_id}"
                    )
                if matches_tool_call_id(identity, stored_call_id, tool_call_id):
                    model_call_ids.add(identity.model_call_id)
            if len(model_call_ids) != 1:
                raise ExtensionCatalogUnavailableError(
                    f"tool_call canonical assistant/model_call identity 缺失或歧义: {tool_call_id}"
                )
            model_call_id = model_call_ids.pop()
            row = connection.execute(
                "SELECT assembly_id FROM model_calls WHERE session_id = ? "
                "AND thread_id = ? AND model_call_id = ?",
                (session_id, thread_id, model_call_id),
            ).fetchone()
            if row is None or not row[0]:
                raise ExtensionCatalogUnavailableError(
                    f"tool_call 原 model-call 缺少 sealed assembly: {tool_call_id}"
                )
            assembly_id = row[0]
        assembly = self._storage.get_context_assembly(
            session_id, thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )
        if assembly.model_call_id != model_call_id:
            raise ExtensionCatalogUnavailableError("tool_call 原 assembly model-call identity 不一致")
        snapshot = self.load_sealed_resource_activation(
            session_id, thread_id=thread_id, assembly_id=assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
            checkpoint_ns=checkpoint_ns,
        )
        return snapshot, model_call_id

    def resolve_extension_dispatch_for_tool_call(
        self, session_id: str, *, thread_id: str, tool_call_id: str,
        model_call_id: str | None = None, checkpoint_ns: str = "",
    ) -> ExtensionDispatchBindingRef:
        snapshot, model_call_id = self.load_tool_call_resource_activation(
            session_id, thread_id=thread_id, tool_call_id=tool_call_id,
            model_call_id=model_call_id, checkpoint_ns=checkpoint_ns,
        )
        parents = tuple(
            binding for binding in snapshot.bindings
            if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID
        )
        if len(parents) != 1:
            raise ExtensionCatalogUnavailableError("tool_call sealed snapshot 缺少唯一 MCP parent")
        binding = parents[0]
        return extension_dispatch_from_activation(
            snapshot=snapshot, binding=binding,
            payload=self.read_resource_activation_payload(snapshot, binding, checkpoint_ns=checkpoint_ns),
            model_call_id=model_call_id,
        )

    def require_provider_resource_activation(
        self,
        snapshot: ResourceActivationSnapshotRef,
        *,
        session_id: str,
        thread_id: str,
        execution_id: str,
        turn_id: str | None,
        model_call_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """正式 Provider seal 只消费同一 admission 下已经冻结的资源事实。"""

        if not isinstance(model_call_id, str):
            raise TypeError("provider context 需要真实 model_call UUID")
        UUID(model_call_id)
        if not isinstance(snapshot, ResourceActivationSnapshotRef):
            raise TypeError("provider context 需要 typed frozen activation snapshot")
        admission = require_current_execution_admission(langgraph_thread_id=thread_id)
        expected = (session_id, thread_id, execution_id, turn_id)
        if (admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id) != expected:
            raise RuntimeError("provider context 与当前 execution admission 不一致")
        if (snapshot.owner_session_id, snapshot.owner_thread_id, snapshot.execution_id, snapshot.turn_id) != expected:
            raise ValueError("resource-activation-snapshot-conflict: Provider snapshot owner 与 admission 不一致")
        model_bound = snapshot.activation_policy.has_model_call_boundary()
        if (snapshot.snapshot_kind == "model_call") != model_bound or (
            model_bound and snapshot.model_call_id != model_call_id
        ):
            raise ValueError("resource-activation-snapshot-conflict: Provider snapshot 与真实 model-call 不一致")
        frozen = self.read_resource_activation_snapshot(
            session_id, thread_id=thread_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
            checkpoint_ns=checkpoint_ns,
        )
        if frozen != snapshot:
            raise ValueError("resource-activation-snapshot-conflict: Provider snapshot 与已冻结事实不一致")

    def frozen_resource_activation_for_admission(
        self, admission: ExecutionAdmission, *, checkpoint_ns: str = ""
    ) -> ResourceActivationSnapshotRef | None:
        return self._activation_store().frozen_for_admission(
            admission, checkpoint_ns=self._context_owner_namespace(checkpoint_ns)
        )

    def find_resource_activation_snapshot(
        self,
        session_id: str,
        *,
        thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef | None:
        return self._activation_store().find_snapshot(
            session_id, thread_id=thread_id,
            activation_snapshot_id=activation_snapshot_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

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
        self, session_id: str, *, thread_id: str, assembly_id: str, checkpoint_ns: str = ""
    ):
        return self._activation_store().read_assembly_binding(
            session_id,
            thread_id=thread_id,
            assembly_id=assembly_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def load_sealed_resource_activation(
        self,
        session_id: str,
        *,
        thread_id: str,
        assembly_id: str,
        plan_hash: str,
        request_hash: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        """restore/replay/rewind/compaction 的唯一只读 activation 入口。

        只按已提交 assembly identity 读取 sealed binding 与 snapshot，并重算
        hash；绝不解析 display URI、当前文件、网络 endpoint 或当前 Registry。
        binding 缺失、owner/ordinal/hash 不符或正文被 retention 清理时必须显式
        失败（``resource-activation-unavailable``/``...-hash-mismatch``/
        ``...-lineage-unavailable``），不返回任何替代 snapshot。
        """

        store = self._activation_store()
        binding = store.find_assembly_binding(
            session_id,
            thread_id=thread_id,
            assembly_id=assembly_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )
        if binding is None:
            raise RuntimeError(
                "resource-activation-unavailable: 已提交 assembly 缺少 activation "
                f"绑定: {assembly_id}"
            )
        if binding["plan_hash"] != plan_hash or binding["request_hash"] != request_hash:
            raise RuntimeError(
                "resource-activation-hash-mismatch: sealed assembly 的 activation "
                f"binding 与已提交 plan/request hash 不一致: {assembly_id}"
            )
        snapshot = binding["snapshot"]
        if not isinstance(snapshot, ResourceActivationSnapshotRef):
            raise TypeError(
                "load_sealed_resource_activation 需要 domain "
                "ResourceActivationSnapshotRef"
            )
        expected_owner = (session_id, thread_id)
        if (snapshot.owner_session_id, snapshot.owner_thread_id) != expected_owner:
            raise RuntimeError(
                "resource-activation-snapshot-conflict: sealed activation owner "
                f"与 assembly owner 不一致: {assembly_id}"
            )
        return snapshot

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
            activation_snapshot.owner_thread_id,
            checkpoint_ns,
            session_id=session_id,
            read_only=True,
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
