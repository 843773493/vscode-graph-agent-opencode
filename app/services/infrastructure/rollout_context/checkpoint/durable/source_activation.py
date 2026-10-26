"""assembly seal 事务内校验冻结 activation，并提交 source applied 状态。"""

from __future__ import annotations

import json
import sqlite3

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import matches_tool_call_id
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.services.business.resource_derivations.team import (
    TEAM_EVENT_SOURCE_ID,
    TEAM_FACETS,
    TEAM_LEDGER_SOURCE_ID,
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
    team_facet_payload,
)
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_GUIDANCE_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash
from app.services.infrastructure.rollout_context.checkpoint.durable.context_source_control import (
    _SELECT_COLUMNS,
    _TABLE_NAME,
    _state_from_row,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    DefinitionProvenance,
    definition_source_values,
    is_tool_definition_source,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)


def _validate_activation_guidance(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
    previous: ContextAssemblySnapshot | None,
    *, resource_id: str = MCP_TOOL_CATALOG_RESOURCE_ID, source_id: str = MCP_GUIDANCE_SOURCE_ID,
) -> None:
    """同一 assembly 事务核 exact guidance 与唯一 activation 父行，不更新 CSM 控制行。"""
    rows = connection.execute(
        "SELECT r.revision, r.content_hash, r.availability FROM resource_activation_assembly_bindings a "
        "JOIN resource_activation_bindings r ON r.activation_snapshot_id = a.activation_snapshot_id "
        "AND r.session_id = a.session_id AND r.thread_id = a.thread_id "
        "WHERE a.assembly_id = ? AND a.session_id = ? AND a.thread_id = ? AND r.resource_id = ?",
        (snapshot.assembly_id, snapshot.session_id, snapshot.thread_id, resource_id),
    ).fetchall()
    if len(rows) > 1:
        raise ItemSchemaError("context-source-seal-conflict: MCP activation parent 不唯一")
    binding = snapshot.seal_binding
    frames = tuple(frame for frame in binding.prefix.state.frames if frame.item_ref.source_identity == source_id)
    previous_frames = () if previous is None or previous.seal_binding is None else tuple(
        frame for frame in previous.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == source_id
    )
    if not rows and not previous_frames and not frames:
        return
    if not frames:
        raise ItemSchemaError("context-source-seal-conflict: activation guidance 缺少 selected frame")
    frame = frames[-1]
    content = frame.content
    if frame.source_protocol == 1:
        blocks = parse_source_blocks(content)
        if len(blocks) != 1 or blocks[0].source_identity != source_id:
            raise ItemSchemaError("context-source-seal-conflict: guidance 来源块不唯一或身份不一致")
        content = blocks[0].body
    expected_revision, expected_hash = (
        (rows[0][0], rows[0][1]) if rows else
        (sha256_jcs({"content": ""}), sha256_jcs({"content": ""}))
    )
    if (
        frame.role != "user" or frame.payload_kind != "text"
        or not isinstance(content, str)
        or frame.item_ref.source_revision != expected_revision
        or sha256_jcs({"content": content}) != expected_hash
        or (rows and rows[0][2] != "available")
        or (not rows and content != "")
        or any(source.source_identity == source_id for source in (() if binding.root is None else binding.root.sources))
    ):
        raise ItemSchemaError("context-source-seal-conflict: guidance 正文/revision 与冻结 activation 不一致")
    previous_revision = previous_frames[-1].item_ref.source_revision if previous_frames else None
    applied = tuple(source for source in binding.source_revisions if source.source_id == source_id)
    # 新 epoch 复用相同 activation revision 时仍校验完整指引帧，
    # 但它没有推进来源 revision，不得伪造一份重复 applied 回执。
    required = previous_revision != expected_revision
    if len(applied) != int(required) or any(
        source.revision != expected_revision or source.previous_revision != previous_revision
        or source.expected_control_revision is not None
        for source in applied
    ):
        raise ItemSchemaError("context-source-seal-conflict: guidance applied revision 与原 assembly 不一致")



def _validate_definition_sources(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
    previous: ContextAssemblySnapshot | None,
) -> None:
    """与 assembly 同事务校验冻结定义；不创建或更新 CSM 控制行。"""
    def require_definition(ref: DefinitionProvenance) -> tuple[str, str]:
        rows = connection.execute(
            "SELECT session_id,thread_id FROM resource_activation_bindings "
            "WHERE activation_snapshot_id=? AND resource_id=? AND revision=? "
            "AND content_hash=? AND resource_kind='mcp_tool_catalog' AND facet='definition' AND availability='available'",
            (ref.activation_snapshot_id, ref.resource_id, ref.revision, ref.revision),
        ).fetchall()
        if len(rows) != 1:
            raise ItemSchemaError("definition-source-conflict: 冻结定义缺失或身份不唯一")
        require_historical_scope(connection, *rows[0], current_owner=(snapshot.session_id, snapshot.thread_id))
        return tuple(rows[0])

    assert snapshot.seal_binding is not None
    if any(is_tool_definition_source(source.source_identity) for source in (
        () if snapshot.seal_binding.root is None else snapshot.seal_binding.root.sources
    )):
        raise ItemSchemaError("definition-source-conflict: 外部定义只能作为 tail user 来源")
    current: dict[str, str] = {}
    for value, revision in definition_source_values(snapshot):
        discovered = value.discovered_from
        original_owner = require_definition(discovered)
        rows = connection.execute(
            "SELECT tool.tool_call_id,item.projection_manifest_json FROM model_calls call "
            "JOIN resource_activation_assembly_bindings activation ON activation.assembly_id=call.assembly_id "
            "JOIN item_catalog item ON json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id')=call.model_call_id "
            "JOIN messages message ON message.jsonl_offset=item.jsonl_offset AND message.jsonl_length=item.jsonl_length "
            "JOIN tool_calls tool ON tool.assistant_message_sequence=message.message_sequence "
            "WHERE call.model_call_id=? AND call.session_id=? AND call.thread_id=? "
            "AND activation.activation_snapshot_id=? AND tool.tool_name='search_tools' AND tool.result_message_sequence IS NOT NULL",
            (discovered.model_call_id, *original_owner, discovered.activation_snapshot_id),
        ).fetchall()
        if not any(matches_tool_call_id(
            ItemProjectionIdentity.from_dict(json.loads(manifest)["projection_identity"]), call_id, discovered.tool_call_id,
        ) for call_id, manifest in rows):
            raise ItemSchemaError("definition-source-conflict: 原发现调用不属于所指冻结请求")
        if value.definition is not None:
            definition = value.definition
            require_definition(definition.provenance)
            if (definition.provenance.resource_id != discovered.resource_id
                    or sha256_jcs(definition.payload()) != definition.provenance.revision
                    or extension_schema_hash(definition.input_schema) != definition.schema_hash):
                raise ItemSchemaError("definition-source-conflict: 完整定义正文与冻结身份不一致")
        current[discovered.resource_id] = revision
    if any(is_tool_definition_source(source.source_id) and source.source_id not in current
           for source in snapshot.seal_binding.source_revisions):
        raise ItemSchemaError("definition-source-conflict: 活动定义回执缺少真实 selected 来源")
    prior = {value.discovered_from.resource_id: revision for value, revision in definition_source_values(previous)}
    for source_id, revision in current.items():
        applied = tuple(source for source in snapshot.seal_binding.source_revisions if source.source_id == source_id)
        if len(applied) != int(prior.get(source_id) != revision) or any(
            source.revision != revision or source.previous_revision != prior.get(source_id)
            or source.expected_control_revision is not None for source in applied
        ):
            raise ItemSchemaError("definition-source-conflict: 活动定义 applied 回执与封存前驱不一致")


class ContextSourceActivationMixin:
    def receive_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        """cold recipient 只提交 pending observation；不创建 admission、CSM 或 canonical item。"""
        self._context_source_rollout_owner(session_id, thread_id)
        payload = json.loads(payload_json)
        if (payload["session_id"], payload["ledger_revision"], payload["event_hash"]) != (session_id, ledger_revision, event_hash):
            raise ValueError("team pending observation 的冻结 owner/revision/hash 不一致")
        payload_json = canonical_json_bytes(payload).decode()
        if not self._context_source_control_index_ready(session_id, thread_id):
            self._storage.initialize(session_id, "", thread_id=thread_id, validate_jsonl_items=False)
        with self._context.executions._execution_input_transaction(session_id, thread_id) as connection:
            row = connection.execute("SELECT event_hash,payload_json FROM team_source_observations "
                                     "WHERE session_id=? AND thread_id=? AND ledger_revision=?",
                                     (session_id, thread_id, ledger_revision)).fetchone()
            if row is not None:
                if tuple(row) != (event_hash, payload_json):
                    raise RuntimeError("team pending observation 同 revision preimage 冲突")
                return
            connection.execute("INSERT INTO team_source_observations VALUES (?,?,?,?,?)",
                               (session_id, thread_id, ledger_revision, event_hash, payload_json))
    def require_team_source_observation(self, session_id: str, thread_id: str, *, ledger_revision: int,
                                       event_hash: str, payload_json: str) -> None:
        """发布 ready 前按状态索引精确核持久提交，不重投已完成 recipient。"""
        with self._storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
            self._storage._require_v2_runtime(connection)
            row = connection.execute("SELECT event_hash,payload_json FROM team_source_observations "
                                     "WHERE session_id=? AND thread_id=? AND ledger_revision=?",
                                     (session_id, thread_id, ledger_revision)).fetchone()
            if row is None or tuple(row) != (event_hash, canonical_json_bytes(json.loads(payload_json)).decode()):
                raise RuntimeError("team-state-not-ready: recipient 持久 observation 与原 fanout 不一致")
    def validate_team_activation_observations(self, connection: sqlite3.Connection,
                                             snapshot: ResourceActivationSnapshotRef) -> None:
        """冻结向量与原 pending 状态索引核对；不读取当前 collaboration ledger。"""
        for binding in snapshot.bindings:
            if binding.resource_kind != "team_state":
                continue
            if binding.facet not in TEAM_FACETS or binding.resource_id != TEAM_RESOURCE_IDS[binding.facet] or binding.source_lineage_ref is None:
                raise ItemSchemaError("team-state-snapshot-conflict: 未知 facet 或缺少冻结 lineage")
            lineage = dict(binding.source_lineage_ref.sources)
            revision = int(lineage[TEAM_LEDGER_SOURCE_ID])
            if revision == 0:
                payload = {"content": ""}
            else:
                row = connection.execute("SELECT event_hash,payload_json FROM team_source_observations "
                                         "WHERE session_id=? AND thread_id=? AND ledger_revision=?",
                                         (snapshot.owner_session_id, snapshot.owner_thread_id, revision)).fetchone()
                if row is None or row[0] != lineage[TEAM_EVENT_SOURCE_ID]:
                    raise ItemSchemaError("team-state-not-ready: required revision 尚未持久 reconcile")
                payload = team_facet_payload(str(row[1]), snapshot.owner_thread_id, binding.facet)
            if binding.revision != sha256_jcs(payload) or binding.content_hash != sha256_jcs(payload):
                raise ItemSchemaError("team-state-snapshot-conflict: facet 与原 pending observation 不一致")
    def _apply_sealed_context_sources(
        self,
        connection: sqlite3.Connection,
        snapshot: ContextAssemblySnapshot,
        *,
        previous: ContextAssemblySnapshot | None,
    ) -> None:
        """由唯一 assembly_sealed 事务确认 source-applied 与 prefix predecessor。

        ToolSet/PrefixEpoch 的 applied 真值就是不可变 assembly binding；这里
        不另建可改写的状态表。source 控制行只在同一事务内按 from revision
        推进可见基准，失败由调用方回滚整个 assembly。
        """
        binding = snapshot.seal_binding
        if binding is None:
            return
        previous_id = previous.assembly_id if previous is not None else None
        if binding.previous_assembly_id != previous_id:
            raise RuntimeError(
                "context-seal-predecessor-conflict: applied assembly 已变化，拒绝半提交"
            )
        if previous is not None and previous.seal_binding is None:
            raise ItemSchemaError(
                "context-seal-binding-required: 既有 assembly 缺少 applied epoch binding"
            )
        # imported predecessor 仍参与同一事务的历史 frontier CAS，但不冒充
        # 目标已 applied 的 epoch；目标首次请求必须独立从 ordinal 1 开始。
        if previous is not None and (previous.session_id, previous.thread_id) == (
            snapshot.session_id, snapshot.thread_id,
        ):
            previous_binding = previous.seal_binding
            previous_epoch = previous_binding.prefix.state.epoch
            current_epoch = binding.prefix.state.epoch
            if current_epoch.ordinal == previous_epoch.ordinal:
                if (
                    current_epoch != previous_epoch
                    or binding.prefix.parent != previous_binding.prefix.state.parent
                    or binding.root != previous_binding.root
                    or binding.prefix.state.provider_profile != previous_binding.prefix.state.provider_profile
                    or binding.prefix.state.tool_compatibility_key != previous_binding.prefix.state.tool_compatibility_key
                ):
                    raise ItemSchemaError("context-seal-epoch-conflict: 同 epoch applied binding 被重写")
            elif current_epoch.ordinal != previous_epoch.ordinal + 1:
                raise ItemSchemaError("context-seal-epoch-conflict: rebuild ordinal 必须连续")
        elif binding.prefix.state.epoch.ordinal != 1 or binding.prefix.parent is not None:
            raise ItemSchemaError("context-seal-epoch-conflict: 首个 assembly 必须是 initial epoch")
        _validate_activation_guidance(connection, snapshot, previous)
        _validate_definition_sources(connection, snapshot, previous)
        for facet in TEAM_FACETS:
            _validate_activation_guidance(connection, snapshot, previous,
                                          resource_id=TEAM_RESOURCE_IDS[facet], source_id=TEAM_SOURCE_IDS[facet])
        for source in binding.source_revisions:
            if (is_tool_definition_source(source.source_id)
                    or source.source_id in {MCP_GUIDANCE_SOURCE_ID, *TEAM_SOURCE_IDS.values()}):
                continue
            row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ?",
                (snapshot.session_id, snapshot.thread_id, source.source_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "context-source-seal-conflict: source 未由 owner 登记: " + source.source_id
                )
            stored = _state_from_row(tuple(row))
            rebuilding_source = source.expected_control_revision is not None
            if rebuilding_source and binding.prefix.consumed_transition is None:
                raise ItemSchemaError(
                    "context-source-seal-conflict: rebuild CAS 缺少真实 consumed transition"
                )
            if (
                stored.latest_visible_committed_revision != source.previous_revision
                or (
                    (stored.state_revision != source.expected_control_revision or not stored.tracking_status == "tracked")
                    if rebuilding_source else stored.latest_revision != source.revision
                )
            ):
                raise RuntimeError(
                    "context-source-seal-conflict: observed/applied revision 已变化: "
                    + source.source_id
                )
            selected_source = any(
                provenance.source_identity == source.source_id
                and provenance.revision == source.revision
                for provenance in (() if binding.root is None else binding.root.sources)
            ) or any(
                frame.item_ref.source_identity == source.source_id
                and frame.item_ref.source_revision == source.revision
                for frame in binding.prefix.state.frames
            )
            if not selected_source:
                raise ItemSchemaError(
                    "context-source-seal-conflict: applied source 没有真实 selected frame: "
                    + source.source_id
                )
            cursor = connection.execute(
                f"UPDATE {_TABLE_NAME} SET latest_visible_committed_revision = ?, latest_revision = ?, "
                "state_revision = state_revision + 1, updated_at = ? "
                "WHERE session_id = ? AND thread_id = ? AND source_id = ? "
                "AND state_revision = ?",
                (
                    source.revision, source.revision if rebuilding_source else stored.latest_revision,
                    _now(), snapshot.session_id, snapshot.thread_id,
                    source.source_id, stored.state_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("context-source-seal-conflict: source applied CAS 未命中")
