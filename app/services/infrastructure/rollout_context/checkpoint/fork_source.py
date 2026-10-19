"""Checkpoint fork 的source 解析、冻结和预检 owner。"""


from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.phase_observer import observe_phase
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionForkResourceSourceDTO
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugWorkspaceForkConfig,
    capture_source_copy_snapshot,
    debug_configuration_resource_uri,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetContext,
    ResourceTargetFacts,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.fork.copy_items import (
    EFFECTIVE_PREFIX_SQL,
    select_view_prefix,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    publish_captured_snapshot,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkSourceSnapshot,
    capture_fork_rollout_snapshot,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    read_local_scopes,
    require_historical_scope,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )

@dataclass(frozen=True, slots=True)
class RolloutForkResult:
    """Saver 完成并收敛 target fork journal 后返回的 completion facts。"""

    source_checkpoint_id: str | None
    source_view_id: str | None
    source_view_revision: int | None = None
    anchor_item_id: str | None = None
    fork_id: str | None = None
    materialization_id: str | None = None
    journal_state: str | None = None
    source_snapshot_sha256: str | None = None


class CheckpointForkSource:
    def __init__(
        self, *, checkpoint: RolloutCheckpointSaver, storage, context_reader,
        attachment_store: AttachmentBlobStore | None,
        node_debug_store: NodeDebugSessionStore,
        node_debug_workspace_config: Callable[[], NodeDebugWorkspaceForkConfig] | None,
    ) -> None:
        self._checkpoint = checkpoint
        self._storage = storage
        self._context_reader = context_reader
        self.attachment_store = attachment_store
        self.node_debug_store = node_debug_store
        self.node_debug_workspace_config = node_debug_workspace_config

    @asynccontextmanager
    async def _source_read_admission(self, session_id: str) -> AsyncIterator[None]:
        """完整冷读先取得拓扑与 source 共享锁，再核权威可见性。"""
        sessions_root = self._storage.storage.sessions_dir
        async with (
            NavigationTopologyGate(sessions_root).shared(),
            SessionLifecycleGate(sessions_root).shared(session_id),
        ):
            node = self._storage.storage._path_resolver.catalog_store.get_node(session_id)
            if node.kind != "session":
                raise RuntimeError(f"fork source 不是 Session: {session_id}")
            if node.state != "active":
                raise SessionDeletionPendingError(
                    "session_deletion_pending: fork source 正在删除，拒绝新 capture: "
                    f"session_id={session_id}, state={node.state}"
                )
            yield

    def resolve_thread_owner(self, session_id: str, thread_id: str) -> MutationIntentOwner:
        """从两个权威 catalog 解析精确 Session/thread owner。"""
        resolver = self._storage.storage._path_resolver
        resolver.resolve_thread_node(session_id, thread_id)
        return MutationIntentOwner(session_id=session_id, thread_id=thread_id)


    def resolve_main_thread_owner(self, session_id: str) -> MutationIntentOwner:
        resolver = self._storage.storage._path_resolver
        return self.resolve_thread_owner(session_id, resolver.main_thread_id(session_id))


    def resolve_prepared_main_thread_owner(
        self,
        record: SessionCreationRecord,
    ) -> MutationIntentOwner:
        """仅凭 fork 创建 record capability 解析未发布 target main owner。"""
        resolver = self._storage.storage._path_resolver
        session_path = resolver.resolve_prepared_session_node(record)
        thread_id = record.main_thread_id
        thread_path = resolver.resolve_prepared_thread_node(record, thread_id)
        if thread_path != session_path:
            raise RuntimeError(
                "prepared fork main thread 必须折叠到 Session 根目录: "
                f"session_id={record.session_id}, thread_id={thread_id}, "
                f"path={thread_path}"
            )
        control_path = session_path / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError(
                "prepared fork target 缺少普通文件形式的 per-session thread "
                f"catalog: session_id={record.session_id}, path={control_path}"
            )
        control_store = SessionControlStore(control_path)
        try:
            control_store.verify_matches_catalog_main_thread(thread_id)
        finally:
            control_store.close()
        return MutationIntentOwner(session_id=record.session_id, thread_id=thread_id)


    def _require_thread_owner(
        self,
        session_id: str,
        owner: MutationIntentOwner | None,
    ) -> MutationIntentOwner:
        if owner is None:
            return self.resolve_main_thread_owner(session_id)
        if owner.session_id != session_id:
            raise RuntimeError("fork source owner 与 Session 不匹配")
        return self.resolve_thread_owner(session_id, owner.thread_id)


    async def _capture_fork_source_snapshot(
        self, *, source_session_id: str, source_owner: MutationIntentOwner,
        mode: str, checkpoint_ns: str, snapshot_root: Path,
        capture_record: ForkSourceCaptureRecord, same_storage_owner: bool,
    ) -> ForkSourceSnapshot:
        """持有 source 生命周期和共享写锁，只捕获原文件及附件 ref metadata。"""
        request = json.loads(capture_record.request_json)
        source_domain = request["source_storage_domain_id"]
        target_domain = request["target_storage_domain_id"]
        actual_domain = self._storage.storage._path_resolver.catalog_store.get_node(source_session_id).workspace_id
        if source_domain != actual_domain or (source_domain == target_domain) != same_storage_owner:
            raise ValueError("fork_owner_overlap: 冻结 source 资源域与实际 owner 不一致")
        async with (
            self._source_read_admission(source_session_id),
            self._context_reader.read_lease(source_session_id, source_owner.thread_id, checkpoint_ns),
        ):
            worker = asyncio.get_running_loop().run_in_executor(
                None, copy_context().run, partial(
                    capture_fork_rollout_snapshot, self._storage,
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                    snapshot_session_node=snapshot_root / source_session_id,
                    snapshot_id=capture_record.snapshot_id, rollout_lock_held=True,
                ),
            )
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    rollout_snapshot = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError as error:
                    # 取消不能终止线程；重复取消也必须等复制完成，才能释放 source 锁。
                    if cancellation is None:
                        cancellation = error
            if cancellation is not None:
                raise cancellation
            await observe_phase(capture_record.copy_operation_id, "copy.source.partial")
            with self._storage.storage.fork_source_snapshot_scope(
                session_id=source_session_id, thread_id=source_owner.thread_id,
                session_node=rollout_snapshot.session_node,
            ):
                point = self._resolve_fork_source(
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                    mode=mode, source_view_id=request["source_view_id"],
                    source_view_revision=request["source_view_revision"],
                    anchor_item_id=request["anchor_item_id"], anchor_mode=request["anchor_mode"],
                    checkpoint_ns=checkpoint_ns,
                )
                if point.source_view_id is None:
                    raise RuntimeError("source-mismatch: captured resource selection 缺少 view")
                stage = rollout_snapshot.session_node / "rollout"
                with closing(sqlite3.connect(
                    (stage / "index.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True,
                )) as connection:
                    if mode != "full_rollout_copy":
                        select_view_prefix(
                            self._storage.storage, connection, stage=stage,
                            source_view_id=point.source_view_id,
                            source_checkpoint_id=point.source_checkpoint_id,
                            anchor_item_id=point.anchor_item_id, anchor_mode=request["anchor_mode"],
                            source_view_revision=point.source_view_revision,
                        )
                        cursor = connection.execute(
                            "SELECT DISTINCT ref.resource_uri,1 FROM item_resource_references ref "
                            "JOIN fork_selected_items selected ON selected.item_id=ref.item_id "
                            "WHERE ref.resource_kind='attachment' ORDER BY ref.resource_uri",
                        )
                    else:
                        cursor = connection.execute(
                            "SELECT ref.resource_uri,MAX(CASE WHEN member.item_id IS NULL THEN 0 ELSE 1 END) "
                            "FROM item_resource_references ref LEFT JOIN context_view_items member "
                            "ON member.item_id=ref.item_id AND member.view_id=? AND member.visible=1 AND member.context_selected=1 "
                            "WHERE ref.resource_kind='attachment' GROUP BY ref.resource_uri ORDER BY ref.resource_uri",
                            (point.source_view_id,),
                        )
                    references = []
                    while rows := cursor.fetchmany(256):
                        references.extend((uri, bool(required)) for uri, required in rows
                                          if uri.startswith("boxteam-session://"))
                    source = ResourceTargetFacts(
                        resource_uri=request["source_uri"], storage_domain_id=source_domain,
                        lifecycle="snapshot", available=True, required_for_context=True, snapshot_import_supported=True,
                        owner_session_id=source_session_id, owner_thread_id=source_owner.thread_id,
                    )
                    target = ResourceTargetContext(
                        storage_domain_id=target_domain,
                        owner_session_id=capture_record.target_session_id, owner_thread_id=capture_record.target_thread_id,
                    )
                    choices = {choice["resource_uri"]: choice["action"] for choice in request["resources"]}
                    selected_only = mode != "full_rollout_copy"
                    self._storage.storage.check_fork_target_resources(
                        connection, source, target, choices=choices, selected_only=selected_only,
                    )
                    self._storage.assemblies.check_fork_target_resources(
                        connection, source, target, choices=choices, selected_only=selected_only,
                        captured_node=rollout_snapshot.session_node,
                    )
                    self._checkpoint._activation_store().check_fork_target_resources(
                        connection, source, target, choices=choices, selected_only=selected_only,
                        captured_node=rollout_snapshot.session_node,
                    )
            if references and self.attachment_store is None:
                raise RuntimeError("fork source 含附件，但 Saver 未注入唯一 AttachmentBlobStore")
            attachments = self.attachment_store.capture_fork_attachments(
                capture_record, references=references, same_storage_owner=same_storage_owner,
            ) if references else ()
            debug_prefix = request["source_uri"] + "/debug/node/configurations/"
            debug_choices = {uri: action for uri, action in choices.items() if uri.startswith(debug_prefix)}
            selected_debug = []
            if debug_choices:
                manifest = self.node_debug_store.read_manifest(source_session_id, source_owner.thread_id)
                registered = {
                    debug_configuration_resource_uri(request["source_uri"], configuration_id): configuration_id
                    for configuration_id in manifest.configuration_ids
                } if manifest is not None else {}
                for uri, action in debug_choices.items():
                    if uri not in registered:
                        raise ValueError(f"unknown_fork_resource: 来源 owner 未登记调试方案: {uri}")
                    decision = check_resource_target(
                        ResourceTargetFacts(
                            resource_uri=uri, storage_domain_id=source_domain,
                            lifecycle="snapshot", available=True, snapshot_import_supported=True,
                            owner_session_id=source_session_id, owner_thread_id=source_owner.thread_id,
                        ), target, choice=action,
                    )
                    if decision.action == "copy_snapshot":
                        selected_debug.append(registered[uri])
            debug_snapshot = None
            if selected_debug:
                if self.node_debug_workspace_config is None:
                    raise RuntimeError("显式调试方案复制缺少 Workspace debug 配置 provider")
                config = self.node_debug_workspace_config()
                debug_snapshot = capture_source_copy_snapshot(
                    self.node_debug_store, session_id=source_session_id, thread_id=source_owner.thread_id,
                    configuration_ids=tuple(selected_debug), workspace_config_revision=config.revision,
                    workspace_config_hash=config.content_hash, workspace_id=config.workspace_id,
                )
            known = {request["source_uri"], *debug_choices}
            for attachment in attachments:
                known.update((attachment.source_file_id, *attachment.source_file_aliases))
            with closing(sqlite3.connect((stage / "index.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                known.update(uri for (uri,) in connection.execute(
                    "SELECT DISTINCT display_uri FROM resource_activation_bindings WHERE display_uri IS NOT NULL",
                ))
            if unknown := choices.keys() - known:
                raise ValueError(f"unknown_fork_resource: 来源冻结选择未登记资源: {sorted(unknown)}")
            snapshot = ForkSourceSnapshot(rollout=rollout_snapshot, attachments=attachments, node_debug=debug_snapshot)
            await observe_phase(capture_record.copy_operation_id, "copy.attachments.preparing")
            publish_captured_snapshot(capture_record, snapshot)
            await observe_phase(capture_record.copy_operation_id, "copy.source.marker_published")
        return snapshot

    async def preflight_fork(
        self, *, source_session_id: str, source_owner: MutationIntentOwner | None = None,
        mode: str = "context_fork", source_view_id: str | None = None,
        source_view_revision: int | None = None, anchor_item_id: str | None = None,
        anchor_mode: str = "inclusive", checkpoint_ns: str = "",
    ) -> RolloutForkResult:
        """在创建目标之前固定真实 view/revision/item，不反序列化全 checkpoint。"""
        async with self._source_read_admission(source_session_id):
            source_owner = self._require_thread_owner(source_session_id, source_owner)
            async with self._context_reader.read_lease(
                source_session_id, source_owner.thread_id, checkpoint_ns,
            ):
                return self._resolve_fork_source(
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                    mode=mode, source_view_id=source_view_id, source_view_revision=source_view_revision,
                    anchor_item_id=anchor_item_id, anchor_mode=anchor_mode, checkpoint_ns=checkpoint_ns,
                )

    async def project_display_resources(
        self, *, source_session_id: str, source_owner: MutationIntentOwner,
        source_view_id: str, source_view_revision: int, anchor_item_id: str | None,
        mode: str, anchor_mode: str,
        source_uri: str,
    ) -> tuple[SessionForkResourceSourceDTO, ...]:
        """只读已登记 typed 引用及 producer 关系；完整正文闭包仍由 capture 核验。"""
        async with self._source_read_admission(source_session_id):
            source_owner = self._require_thread_owner(source_session_id, source_owner)
            async with self._context_reader.read_lease(
                source_session_id, source_owner.thread_id, "",
            ):
                point = self._resolve_fork_source(
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                    mode=mode, source_view_id=source_view_id, source_view_revision=source_view_revision,
                    anchor_item_id=anchor_item_id if mode == "view_prefix_fork" else None,
                    anchor_mode=anchor_mode, checkpoint_ns="",
                )
                with self._storage.storage.read_connection(source_session_id, source_owner.thread_id, "") as connection:
                    revision = connection.execute(
                        "SELECT history_view_revision FROM database_meta WHERE singleton_id=1",
                    ).fetchone()[0]
                    if revision != source_view_revision:
                        raise ValueError("source-mismatch: 附件候选的冻结历史 revision 已变化")
                    if mode == "full_rollout_copy":
                        references = connection.execute(
                            "SELECT ref.resource_uri,MAX(CASE WHEN member.item_id IS NULL THEN 0 ELSE 1 END) "
                            "FROM item_resource_references ref LEFT JOIN context_view_items member "
                            "ON member.item_id=ref.item_id AND member.view_id=? AND member.visible=1 AND member.context_selected=1 "
                            "WHERE ref.resource_kind='attachment' GROUP BY ref.resource_uri ORDER BY ref.resource_uri",
                            (source_view_id,),
                        ).fetchall()
                    else:
                        # 只展开可见正文的精确 producer；审计前任与 backing producer 不展开。
                        local_scopes = sorted(read_local_scopes(connection))
                        scope_values = ",".join("(?,?)" for _ in local_scopes)
                        references = connection.execute(
                            "WITH RECURSIVE local_scope(session_id,thread_id) AS ("
                            f"VALUES {scope_values}), visible(item_id) AS ("
                            f"SELECT item_id FROM ({EFFECTIVE_PREFIX_SQL})), "
                            "selected(item_id) AS (SELECT item_id FROM visible UNION "
                            "SELECT selection.ref_id FROM visible JOIN item_catalog item USING(item_id) "
                            "JOIN model_calls call ON call.model_call_id=COALESCE("
                            "json_extract(item.producer_ref_json,'$.model_call_id'),"
                            "json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id')) "
                            "JOIN context_assembly_selections selection ON selection.assembly_id=call.assembly_id "
                            "JOIN context_assemblies assembly ON assembly.assembly_id=selection.assembly_id "
                            "JOIN local_scope scope ON scope.session_id=assembly.session_id "
                            "AND scope.thread_id=selection.ref_thread_id "
                            "JOIN item_catalog source ON source.item_id=selection.ref_id "
                            "AND source.content_hash=selection.content_hash "
                            "AND source.payload_length=selection.content_length "
                            "AND source.source_revision=selection.source_revision "
                            "AND source.item_sequence=json_extract(assembly.snapshot_json, "
                            "'$.selection[' || selection.plan_ordinal || '].ref.item_sequence') "
                            "WHERE selection.included=1 AND selection.ref_type='canonical_item' UNION "
                            "SELECT peer.item_id FROM selected JOIN item_catalog item USING(item_id) "
                            "JOIN item_catalog peer ON peer.message_group_id=item.message_group_id UNION "
                            "SELECT turn.root_input_item_id FROM selected JOIN item_catalog item USING(item_id) "
                            "JOIN turn_records turn ON turn.turn_id=item.turn_id UNION "
                            "SELECT turn.final_item_id FROM selected JOIN item_catalog item USING(item_id) "
                            "JOIN turn_records turn ON turn.turn_id=item.turn_id WHERE turn.final_item_id IS NOT NULL) "
                            "SELECT DISTINCT ref.resource_uri,1 FROM selected JOIN item_resource_references ref USING(item_id) "
                            "WHERE ref.resource_kind='attachment' ORDER BY ref.resource_uri",
                            (*[value for scope in local_scopes for value in scope], point.anchor_item_id,
                             source_view_id, 1 if anchor_mode == "before" else 0),
                        ).fetchall()
                native = tuple((uri, bool(required)) for uri, required in references if uri.startswith("boxteam-session://"))
                if native and self.attachment_store is None:
                    raise RuntimeError("fork source 含附件，但 Saver 未注入唯一 AttachmentBlobStore")
                attachments = self.attachment_store.project_fork_attachment_sources(
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id, references=native,
                ) if native else ()
                manifest = self.node_debug_store.read_manifest(source_session_id, source_owner.thread_id)
                configurations = self.node_debug_store.read_registered_configuration_payloads(
                    source_session_id, source_owner.thread_id, tuple(manifest.configuration_ids),
                ) if manifest is not None else {}
                return (*attachments, *(SessionForkResourceSourceDTO(
                    resource_uri=debug_configuration_resource_uri(source_uri, configuration_id),
                    name=configuration.name, content_type="application/json", availability="available",
                    required_for_context=False,
                ) for configuration_id, (configuration, _) in configurations.items()))

    async def freeze_display_point(
        self, *, source_session_id: str, source_owner: MutationIntentOwner,
        turn_id: str | None = None, item_id: str | None = None,
        source_view_id: str | None = None, source_view_revision: int | None = None,
        mode: str = "view_prefix_fork", anchor_mode: str = "inclusive", checkpoint_ns: str = "",
    ) -> RolloutForkResult:
        """点击用户消息取真实 root item；响应直接使用 projection 的稳定 item。"""
        if turn_id is not None and item_id is not None:
            raise ValueError("fork source point 不能同时选择 Turn root 和响应 item")
        if (source_view_id is None) != (source_view_revision is None):
            raise ValueError("fork source point 必须同时携带 source view/revision")
        if mode == "view_prefix_fork" and (turn_id is not None or item_id is not None) and source_view_id is None:
            raise ValueError("source-mismatch: 消息点击必须携带实际历史读取的 source view/revision")
        async with self._source_read_admission(source_session_id):
            source_owner = self._require_thread_owner(source_session_id, source_owner)
            async with self._context_reader.read_lease(
                source_session_id, source_owner.thread_id, checkpoint_ns,
            ):
                if mode != "view_prefix_fork":
                    return self._resolve_fork_source(
                        source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                        mode=mode, source_view_id=source_view_id, source_view_revision=source_view_revision,
                        anchor_item_id=None, anchor_mode=anchor_mode, checkpoint_ns=checkpoint_ns,
                    )
                with self._storage.storage.read_connection(
                    source_session_id, source_owner.thread_id, checkpoint_ns,
                ) as connection:
                    head = connection.execute(
                        "SELECT b.head_view_id,m.history_view_revision,b.head_checkpoint_id "
                        "FROM checkpoint_namespace_state n JOIN branches b ON b.branch_id=n.active_branch_id "
                        "JOIN database_meta m ON m.singleton_id=1 WHERE n.checkpoint_ns=?", (checkpoint_ns,),
                    ).fetchone()
                    if head is None or head[0] is None:
                        raise ValueError("source-mismatch: source rollout 缺少真实 active view")
                    display_view = source_view_id if source_view_id is not None else head[0]
                    display_revision = source_view_revision if source_view_revision is not None else head[1]
                    if display_revision != head[1]:
                        raise ValueError("source-mismatch: 展示快照 revision 已变化")
                    if turn_id is not None:
                        root = connection.execute(
                            "SELECT turn.root_input_item_id FROM context_view_turns turn "
                            "JOIN context_view_items member ON member.view_id=turn.view_id "
                            "AND member.item_id=turn.root_input_item_id AND member.visible=1 "
                            "WHERE turn.view_id=? AND turn.turn_id=?", (display_view, turn_id),
                        ).fetchone()
                        if root is None or root[0] is None:
                            raise ValueError("source-mismatch: 点击用户消息不属于展示快照 view")
                        item_id = root[0]
                    elif item_id is None:
                        completed = self._completed_fork_anchor(connection, display_view)
                        if completed is None:
                            # 展示真实来源，但没有合法默认锚点时不能提交前缀复制。
                            return RolloutForkResult(
                                source_checkpoint_id=head[2], source_view_id=display_view, source_view_revision=display_revision,
                            )
                        item_id = completed[0]
                return self._resolve_fork_source(
                    source_session_id=source_session_id, source_thread_id=source_owner.thread_id,
                    mode="view_prefix_fork", source_view_id=display_view, source_view_revision=display_revision,
                    anchor_item_id=item_id, anchor_mode=anchor_mode, checkpoint_ns=checkpoint_ns,
                )

    @staticmethod
    def _completed_fork_anchor(connection: sqlite3.Connection, view_id: str) -> tuple[str, str, str] | None:
        completed = connection.execute(
            "SELECT final.item_id,turn.session_id,turn.thread_id "
            "FROM context_view_turns member JOIN turn_records turn ON turn.turn_id=member.turn_id "
            "JOIN context_view_items final ON final.view_id=member.view_id "
            "AND final.item_id=turn.final_item_id AND final.visible=1 AND final.context_selected=1 "
            "WHERE member.view_id=? AND turn.status='completed' "
            "ORDER BY final.context_ordinal DESC LIMIT 1", (view_id,),
        ).fetchone()
        if completed is None and connection.execute(
            "SELECT 1 FROM context_view_turns WHERE view_id=? LIMIT 1", (view_id,),
        ).fetchone() is None:
            completed = connection.execute(
                "SELECT member.item_id,execution.session_id,execution.thread_id "
                "FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
                "JOIN executions execution ON execution.execution_id=item.execution_id "
                "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 AND execution.execution_origin='internal' "
                "AND execution.outcome IN ('completed','completed_empty') "
                "ORDER BY member.context_ordinal DESC LIMIT 1", (view_id,),
            ).fetchone()
        return completed

    @staticmethod
    def _empty_fork_checkpoint_source(
        connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str, source_view_id: str, source_checkpoint_id: str | None,
    ) -> tuple[str, str | None]:
        """沿本地冻结 bootstrap 关系定位空上下文，不恢复历史执行。"""
        checkpoint_id = source_checkpoint_id
        visited: set[str] = set()
        while checkpoint_id is not None:
            if checkpoint_id in visited:
                raise ValueError("source-mismatch: fork checkpoint 冻结关系存在循环")
            visited.add(checkpoint_id)
            checkpoint = connection.execute(
                "SELECT view_id,metadata_json FROM checkpoints "
                "WHERE checkpoint_id=? AND checkpoint_ns=? AND status='active'",
                (checkpoint_id, checkpoint_ns),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("source-mismatch: fork checkpoint 冻结关系缺少本地 checkpoint")
            metadata = json.loads(checkpoint[1])
            materialization_id = metadata.get("fork_materialization_id")
            if materialization_id is None:
                return (checkpoint[0], checkpoint_id) if len(visited) > 1 else (source_view_id, None)
            journal = connection.execute(
                "SELECT target_session_id,target_thread_id,source_session_id,source_thread_id,source_checkpoint_id "
                "FROM fork_materializations WHERE materialization_id=? AND checkpoint_ns=? "
                "AND status IN ('prepared','target_committed','committed')",
                (materialization_id, checkpoint_ns),
            ).fetchone()
            if (journal is None or metadata.get("source") != "fork"
                    or metadata.get("boxteam_execution_binding") is not None
                    or metadata.get("fork_context_checkpoint_id") != journal[4]):
                raise ValueError("source-mismatch: fork checkpoint 与冻结物化 journal 不一致")
            for historical_owner in (journal[:2], journal[2:4]):
                require_historical_scope(
                    connection, *historical_owner, current_owner=(session_id, thread_id),
                )
            checkpoint_id = journal[4]
        return source_view_id, None

    def _resolve_fork_source(
        self, *, source_session_id: str, source_thread_id: str, mode: str,
        source_view_id: str | None, source_view_revision: int | None,
        anchor_item_id: str | None, anchor_mode: str, checkpoint_ns: str,
    ) -> RolloutForkResult:
        """三种公开 mode 共用同一冻结索引解析入口。"""
        if mode not in {"context_fork", "view_prefix_fork", "full_rollout_copy"}:
            raise ValueError(f"不支持的 fork mode: {mode}")
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("fork anchor_mode 必须是 inclusive 或 before")
        with self._storage.storage.read_connection(
            source_session_id, source_thread_id, checkpoint_ns,
        ) as connection:
            head = connection.execute(
                "SELECT b.head_view_id,b.head_checkpoint_id,m.history_view_revision "
                "FROM checkpoint_namespace_state n JOIN branches b ON b.branch_id=n.active_branch_id "
                "JOIN database_meta m ON m.singleton_id=1 WHERE n.checkpoint_ns=?", (checkpoint_ns,),
            ).fetchone()
            if head is None or head[0] is None:
                raise ValueError("source-mismatch: source rollout 缺少真实 active view")
            active_view, source_checkpoint_id, revision = head
            if source_view_revision is not None and (
                type(source_view_revision) is not int or source_view_revision != revision
            ):
                raise ValueError("source-mismatch: source view revision 已变化")
            if mode == "view_prefix_fork":
                if source_view_id is None or source_view_revision is None or anchor_item_id is None:
                    raise ValueError("view_prefix_fork 必须固定 source view/revision/item")
            else:
                if source_view_id is not None and source_view_id != active_view:
                    raise ValueError("source-mismatch: source point 已不是 active view")
                source_view_id = active_view
            if mode == "full_rollout_copy":
                if anchor_item_id is not None or anchor_mode != "inclusive":
                    raise ValueError("full_rollout_copy 不能携带截断锚点")
            elif mode == "context_fork":
                completed = self._completed_fork_anchor(connection, source_view_id)
                if completed is None:
                    if connection.execute(
                        "SELECT 1 FROM context_view_items WHERE view_id=? AND visible=1 LIMIT 1", (source_view_id,),
                    ).fetchone() is not None or connection.execute(
                        "SELECT 1 FROM context_view_turns WHERE view_id=? LIMIT 1", (source_view_id,),
                    ).fetchone() is not None:
                        raise ValueError("fork_source_not_completed: active view 没有已完成的安全截止点")
                    empty_source_view, frozen_empty_checkpoint = self._empty_fork_checkpoint_source(
                        connection, session_id=source_session_id, thread_id=source_thread_id,
                        checkpoint_ns=checkpoint_ns, source_view_id=source_view_id,
                        source_checkpoint_id=source_checkpoint_id,
                    )
                    empty_checkpoint = connection.execute(
                        "WITH RECURSIVE lineage(view_id) AS (SELECT ? UNION SELECT view.parent_view_id "
                        "FROM context_views view JOIN lineage ON lineage.view_id=view.view_id WHERE view.parent_view_id IS NOT NULL) "
                        "SELECT cp.checkpoint_id,execution.session_id,execution.thread_id,execution.execution_id,"
                        "execution.job_id,execution.admission_idempotency_key,execution.admission_payload_hash,cp.metadata_json "
                        "FROM checkpoints cp JOIN lineage ON lineage.view_id=cp.view_id "
                        "JOIN executions execution ON execution.execution_id=json_extract(cp.metadata_json,'$.boxteam_execution_binding.execution_id') "
                        "WHERE cp.checkpoint_ns=? AND cp.status='active' "
                        "AND (? IS NULL OR cp.checkpoint_id=?) AND execution.execution_origin='internal' "
                        "AND execution.turn_id IS NULL AND execution.outcome IN ('completed','completed_empty') "
                        "AND NOT EXISTS(SELECT 1 FROM context_view_items member WHERE member.view_id=cp.view_id AND member.visible=1) "
                        "AND NOT EXISTS(SELECT 1 FROM checkpoint_channels channel JOIN context_view_items member "
                        "ON member.view_id=channel.context_view_id WHERE channel.checkpoint_id=cp.checkpoint_id AND member.visible=1) "
                        "ORDER BY cp.commit_id DESC LIMIT 1",
                        (empty_source_view, checkpoint_ns, frozen_empty_checkpoint, frozen_empty_checkpoint),
                    ).fetchone()
                    if empty_checkpoint is None:
                        if connection.execute("SELECT 1 FROM executions LIMIT 1").fetchone() is not None:
                            raise ValueError("fork_source_not_completed: 空 view 缺少精确完成的 internal checkpoint/admission")
                        source_checkpoint_id = None
                    else:
                        source_checkpoint_id, historical_session, historical_thread, execution_id, job_id, admission_key, admission_hash, metadata = empty_checkpoint
                        if json.loads(metadata).get("boxteam_execution_binding") != {
                            "execution_id": execution_id, "job_id": job_id,
                            "admission_idempotency_key": admission_key, "admission_payload_hash": admission_hash,
                        }:
                            raise ValueError("source-mismatch: 空 internal checkpoint 与冻结 admission identity 不一致")
                        admission = self._storage.executions._read_execution_admission_connection(
                            connection, session_id=historical_session, thread_id=historical_thread,
                            execution_id=execution_id, checkpoint_ns=checkpoint_ns,
                        )
                        self._storage.executions.execution_admission_cursor(admission, connection=connection, checkpoint_ns=checkpoint_ns)
                else:
                    require_historical_scope(connection, completed[1], completed[2])
                expected_anchor = completed[0] if completed is not None else None
                if anchor_item_id is not None and anchor_item_id != expected_anchor:
                    raise ValueError("source-mismatch: context_fork 自动截止点与冻结 source point 不一致")
                anchor_item_id = expected_anchor
            if anchor_item_id is not None and connection.execute(
                "SELECT 1 FROM context_view_items WHERE view_id=? AND item_id=? AND visible=1 AND context_selected=1",
                (source_view_id, anchor_item_id),
            ).fetchone() is None:
                raise ValueError("source-mismatch: 点击 item 不属于冻结 source view 的有效上下文")
            if mode != "full_rollout_copy" and anchor_item_id is not None:
                visible_sequences = [row[0] for row in connection.execute(
                    f"WITH prefix AS ({EFFECTIVE_PREFIX_SQL}) "
                    "SELECT DISTINCT message.message_sequence FROM prefix member "
                    "JOIN item_catalog item ON item.item_id=member.item_id "
                    "JOIN messages message ON message.jsonl_offset=item.jsonl_offset "
                    "AND message.jsonl_length=item.jsonl_length "
                    "ORDER BY member.context_ordinal",
                    (anchor_item_id, source_view_id, 1 if anchor_mode == "before" else 0),
                )]
                validate_tool_protocol_closure(connection, visible_sequences, len(visible_sequences))
                selected_checkpoint = connection.execute(
                    "WITH RECURSIVE lineage(view_id) AS (SELECT ? UNION "
                    "SELECT view.parent_view_id FROM context_views view JOIN lineage "
                    "ON view.view_id=lineage.view_id WHERE view.parent_view_id IS NOT NULL), "
                    f"prefix AS ({EFFECTIVE_PREFIX_SQL}) "
                    "SELECT cp.checkpoint_id FROM checkpoints cp JOIN lineage ON lineage.view_id=cp.view_id "
                    "WHERE cp.checkpoint_ns=? AND cp.status='active' AND NOT EXISTS "
                    "(SELECT 1 FROM context_view_items member WHERE member.view_id=cp.view_id "
                    "AND member.visible=1 AND member.context_selected=1 "
                    "AND NOT EXISTS (SELECT 1 FROM prefix WHERE prefix.item_id=member.item_id)) "
                    "AND (json_type(cp.metadata_json,'$.boxteam_execution_binding') IS NULL OR EXISTS "
                    "(SELECT 1 FROM prefix JOIN item_catalog item ON item.item_id=prefix.item_id "
                    "WHERE item.execution_id=json_extract(cp.metadata_json,'$.boxteam_execution_binding.execution_id'))) "
                    "ORDER BY cp.commit_id DESC LIMIT 1",
                    (source_view_id, anchor_item_id, source_view_id,
                     1 if anchor_mode == "before" else 0, checkpoint_ns),
                ).fetchone()
                source_checkpoint_id = selected_checkpoint[0] if selected_checkpoint is not None else None
                if mode == "context_fork" and source_checkpoint_id is None:
                    raise ValueError("source-mismatch: 完成截止点缺少同 lineage、成员和执行绑定的 checkpoint")
            return RolloutForkResult(
                source_checkpoint_id=source_checkpoint_id, source_view_id=source_view_id,
                source_view_revision=revision, anchor_item_id=anchor_item_id,
            )
