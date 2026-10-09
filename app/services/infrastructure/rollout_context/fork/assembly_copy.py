"""fork 的 sealed source 验证与 target plan/manifest/hash 本地化。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    append_items,
    initial_epoch_state,
    seal_assembly,
)
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import payload_content_length, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _COLUMNS as _DETAIL_COLUMNS,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _detail_mapping,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    build_source_manifest,
    parse_source_manifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.fork.full_copy.plan_values import (
    localize_plan_value,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    PreparedDetail,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.mapping.itemized.prefix_frames import project_canonical_prefix_frames


def _selected_detail_refs(source: ContextAssemblySnapshot) -> tuple[DetailRef, ...]:
    """封存正文与原 typed source alias 使用各自独立的 target 身份。"""
    refs: dict[DetailRef, None] = {}
    for entry in source.selection:
        if not entry.included or entry.ref.ref_type != "request_only":
            continue
        if not isinstance(entry.detail_ref, DetailRef):
            raise TypeError("detail-unavailable: fork included source 缺少 typed detail")
        refs[entry.detail_ref] = None
        if isinstance(entry.ref.source_ref, DetailRef):
            refs[entry.ref.source_ref] = None
    return tuple(refs)


def prepare_selected_detail_mappings(
    service, connection: sqlite3.Connection, *, source_session_id: str,
    source_thread_id: str, target_session_id: str, target_thread_id: str,
    source_checkpoint_id: str | None, fork_id: str, checkpoint_ns: str, timestamp: str,
) -> None:
    """在 prepared journal 事务中登记实际将发布的 typed detail 身份。"""
    if source_checkpoint_id is None:
        return
    if service._storage._fork_source_snapshot_for_session(source_session_id) is None:
        raise RuntimeError("source-mismatch: context fork 必须消费冻结 source snapshot")
    with service._storage._connect(source_thread_id, checkpoint_ns, session_id=source_session_id,
                          read_only=True) as source_connection:
        header = source_connection.execute(
            "SELECT session_id,thread_id,source_assembly_id FROM checkpoint_context_source_snapshots "
            "WHERE checkpoint_id=? AND checkpoint_ns=?", (source_checkpoint_id, checkpoint_ns),
        ).fetchone()
        if header is None or header[:2] != (source_session_id, source_thread_id):
            raise RuntimeError("source-mismatch: fork checkpoint 缺少精确 source selection")
        if header[2] is None:
            return
        source, _audit = service._context.assemblies._read_context_assembly_connection(
            source_connection, source_session_id, source_thread_id,
            assembly_id=header[2], checkpoint_ns=checkpoint_ns,
        )
    for source_ref in _selected_detail_refs(source):
        source_ref.require_owner(source_session_id, source_thread_id)
        target_assembly = service._full_copy_identity(
            fork_id, target_session_id, "assembly", source_ref.assembly_id,
        )
        source_key = detail_ref_key(source_ref)
        target = DetailRef(
            target_session_id, target_thread_id, target_assembly,
            service._full_copy_identity(fork_id, target_session_id, "detail", source_key),
        )
        service._ensure_fork_identity_mapping(
            connection, source_session_id=source_session_id, target_session_id=target_session_id,
            fork_id=fork_id, entity_type="detail", source_id=source_key, target_id=detail_ref_key(target),
            lineage={"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                "local_id": source_key},
                     "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                "local_id": detail_ref_key(target)}}, timestamp=timestamp,
        )


def validate_source_assemblies(
    service, connection: sqlite3.Connection, checkpoint_ns: str
) -> None:
    """复制前只读验证 source，不能重建 target manifest 掩盖原件损坏。"""
    owner = connection.execute(
        "SELECT session_id, thread_id FROM database_meta WHERE singleton_id = 1"
    ).fetchone()
    if owner is None:
        raise RuntimeError("fork source rollout 缺少 database_meta owner")
    session_id, thread_id = owner
    for row in connection.execute(
        "SELECT session_id, thread_id, plan_id FROM context_plans"
    ).fetchall():
        if row[:2] != owner:
            raise ValueError("source-mismatch: fork plan registry owner 不一致")
        _, _, plan_id = row
        read_registration(connection, session_id, thread_id, plan_id)
    for row in connection.execute(
        "SELECT session_id, thread_id, snapshot_json, detail_ref FROM context_assemblies"
    ).fetchall():
        if row[:2] != owner:
            raise ValueError("source-mismatch: fork assembly registry owner 不一致")
        _, _, raw, detail_ref = row
        snapshot = ContextAssemblySnapshot.from_dict(json.loads(raw))
        service._context.assemblies._validate_context_assembly_manifest(
            connection,
            snapshot,
            registration=read_registration(connection, session_id, thread_id, snapshot.plan_id),
            header_detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
        snapshot.validate_hashes()


def refresh_copied_assemblies(state: FullCopyRemapState) -> None:
    """引用身份变更后按 domain 算法生成 target 自己的 fingerprint。"""
    connection = state.connection
    items = {item.item_id: item for item in state.new_items.values()}

    for item in items.values():
        for table in ("assembly_item_refs", "context_assembly_selections"):
            connection.execute(
                f"UPDATE {table} SET "
                "source_revision=CASE WHEN source_revision IS NULL THEN NULL ELSE ? END, "
                "content_hash=CASE WHEN content_hash IS NULL THEN NULL ELSE ? END, "
                "content_length=CASE WHEN content_length IS NULL THEN NULL ELSE ? END "
                "WHERE ref_type='canonical_item' AND ref_id=?",
                (
                    (item.source_manifest.source_revision if item.source_manifest else None)
                    or f"canonical:{item.item_id}:{item.content_hash}",
                    item.content_hash,
                    payload_content_length(item.payload_kind, item.payload),
                    item.item_id,
                ),
            )
    for assembly_id, raw, detail_ref in connection.execute(
        "SELECT assembly_id, snapshot_json, detail_ref FROM context_assemblies "
        "WHERE session_id=? AND thread_id=?",
        (state.target_session_id, state.target_thread_id),
    ).fetchall():
        value = localize_plan_value(state, json.loads(raw))
        snapshot = ContextAssemblySnapshot.from_dict(value)
        plan = snapshot.as_sealed_plan()
        snapshot = replace(
            snapshot,
            plan_hash=plan.plan_hash(),
            request_hash=context_request_hash(
                plan,
                snapshot.provider_version,
                projector_id=snapshot.projector_id,
                projector_version=snapshot.projector_version,
                target_format=snapshot.target_format,
                wire_request=snapshot.request_hash_preimage,
            ),
        )
        # registry/seal binding 必须等全部 target snapshot/hash 已写入后安装；
        # remap coordinator 在同一事务末尾执行完整 assembly 验证。
        snapshot.validate_hashes()
        result = connection.execute(
            "UPDATE context_assemblies SET plan_hash=?, request_hash=?, snapshot_json=? "
            "WHERE session_id=? AND thread_id=? AND assembly_id=?",
            (
                snapshot.plan_hash,
                snapshot.request_hash,
                canonical_json_text(snapshot.to_dict()),
                state.target_session_id,
                state.target_thread_id,
                assembly_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork target assembly snapshot 写入失败")


def copy_selected_context_for_fork(
    service, connection: sqlite3.Connection, *, source_session_id: str,
    source_thread_id: str, target_session_id: str, target_thread_id: str,
    source_checkpoint_id: str | None, fork_id: str, checkpoint_ns: str,
    timestamp: str, detail_capability: ForkDetailCapability | None,
) -> tuple[PreparedDetail, ...]:
    """普通 fork 导入 checkpoint 的真实封存上下文，正文只消费冻结 capability。"""
    if source_checkpoint_id is None:
        return ()
    if service._storage._fork_source_snapshot_for_session(source_session_id) is None:
        raise RuntimeError("source-mismatch: context fork 必须消费冻结 source snapshot")
    with service._storage._connect(source_thread_id, checkpoint_ns, session_id=source_session_id,
                          read_only=True) as source_connection:
        header = source_connection.execute(
            "SELECT session_id,thread_id,source_assembly_id FROM checkpoint_context_source_snapshots "
            "WHERE checkpoint_id=? AND checkpoint_ns=?", (source_checkpoint_id, checkpoint_ns),
        ).fetchone()
        if header is None or header[:2] != (source_session_id, source_thread_id):
            raise RuntimeError("source-mismatch: fork checkpoint 缺少精确 source selection")
        if header[2] is None:
            return ()
        source, _audit_ref = service._context.assemblies._read_context_assembly_connection(
            source_connection, source_session_id, source_thread_id,
            assembly_id=header[2], checkpoint_ns=checkpoint_ns,
        )
        registration = read_registration(source_connection, source_session_id,
                                         source_thread_id, source.plan_id)
        if registration.draft is not None:
            registered_refs = registration.draft.refs
            registered_contributions = registration.draft.contributions
        else:
            registered = parse_source_manifest(
                registration.source_manifest, session_id=source_session_id,
                thread_id=source_thread_id, plan_id=source.plan_id,
            )
            registered_refs, registered_contributions = registered.refs, registered.contributions
        source_rows = {
            row[0]: row[1] for row in source_connection.execute(
                "SELECT contribution_id,source_ordinal FROM context_contributions"
            ).fetchall()
        }
        outcome = source_connection.execute(
            "SELECT outcome FROM context_assemblies WHERE assembly_id=?", (source.assembly_id,),
        ).fetchone()[0]
        source_schema = source_connection.execute(
            "SELECT schema_version FROM database_meta WHERE singleton_id=1"
        ).fetchone()[0]
        source_details = {
            record.detail_ref: record for row in source_connection.execute(
                f"SELECT {','.join(_DETAIL_COLUMNS)} FROM context_plan_details "
                "WHERE session_id=? AND thread_id=?",
                (source_session_id, source_thread_id),
            ).fetchall() for record in [detail_record_from_mapping(_detail_mapping(row))]
        }
    if detail_capability is None or source.seal_binding is None:
        raise RuntimeError("detail-unavailable: fork source selection 缺少冻结 detail/prefix capability")

    maps: dict[str, dict[str, str]] = {}
    for entity, source_id, target_id in connection.execute(
        "SELECT entity_type,source_local_id,target_local_id FROM fork_identity_mappings WHERE fork_id=?",
        (fork_id,),
    ).fetchall():
        maps.setdefault(entity, {})[source_id] = target_id
    existing_maps = {entity: set(mapping) for entity, mapping in maps.items()}

    def local(entity: str, source_id: str | None, *, required: bool = False) -> str | None:
        if source_id is None:
            return None
        mapping = maps.setdefault(entity, {})
        if source_id not in mapping:
            if required:
                raise RuntimeError(f"source-mismatch: fork context 缺少 {entity} target mapping: {source_id}")
            mapping[source_id] = service._full_copy_identity(fork_id, target_session_id, entity, source_id)
        return mapping[source_id]

    target_assembly = local("assembly", source.assembly_id)
    target_plan = local("plan", source.plan_id)
    target_execution = local("execution", source.execution_id, required=True)
    target_turn = local("turn", source.turn_id, required=True)
    target_model = local("model_call", source.model_call_id, required=True)
    target_items = {item.item_id: item for item in service._storage.read_items(
        target_session_id, target_thread_id, checkpoint_ns=checkpoint_ns,
        connection=connection,
    )}
    meta = connection.execute(
        "SELECT history_view_revision,source_overlay_epoch FROM database_meta WHERE singleton_id=1"
    ).fetchone()
    target_view = connection.execute(
        "SELECT b.head_view_id FROM checkpoint_namespace_state n JOIN branches b "
        "ON b.branch_id=n.active_branch_id WHERE n.checkpoint_ns=?", (checkpoint_ns,),
    ).fetchone()[0]
    selected_contributions = {item.contribution_id for item in source.contributions}
    contributions = tuple(item for item in registered_contributions
                          if item.contribution_id in selected_contributions)
    if {item.contribution_id for item in contributions} != selected_contributions:
        raise RuntimeError("source-mismatch: fork source contribution 与原 plan registry 不一致")
    ordinal_base = connection.execute(
        "SELECT COALESCE(MAX(source_ordinal),-1)+1 FROM context_contributions"
    ).fetchone()[0]
    ordinal_map = {item.contribution_id: ordinal_base + index for index, item in enumerate(
        sorted(contributions, key=lambda item: source_rows[item.contribution_id]))}
    overlay_bindings = {}
    for source_id, target_id in maps.get("source_overlay", {}).items():
        row = connection.execute(
            "SELECT base_ref,delta_ref,source_overlay_epoch FROM source_overlays WHERE overlay_id=?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("source-mismatch: fork overlay target registry 缺失")
        overlay_bindings[source_id] = (target_id, *row)

    def contribution(item, *, sealed: bool):
        binding = item.source_binding
        if binding is not None and binding.overlay_id is not None:
            overlay_id, base_ref, delta_ref, epoch = overlay_bindings[binding.overlay_id]
            alias = base_ref if binding.overlay_role == "base" else delta_ref
            target_id = f"overlay:{overlay_id}:{binding.overlay_role}"
            maps.setdefault("context_contribution", {})[item.contribution_id] = target_id
            maps.setdefault("request_ref", {})[binding.overlay_ref] = alias
            maps.setdefault("source_identity", {})[binding.source_ref] = alias
            binding = replace(binding, source_ref=alias, overlay_ref=alias,
                              overlay_id=overlay_id, source_overlay_epoch=epoch)
        else:
            target_id = local("context_contribution", item.contribution_id)
            # 普通 SourceBinding 是当前 owner 的逻辑来源别名，不是可跨 owner 解引用的路径。
            # 保留语义来源身份；contribution/request ref/detail 均独立分配 target-local identity。
        maps.setdefault("request_ref", {})[item.contribution_id] = target_id
        return replace(item, contribution_id=target_id, source_binding=binding, body=None,
                       assembly_id=target_assembly if sealed else None,
                       contribution_ordinal=item.contribution_ordinal if sealed else None,
                       source_ordinal=None if sealed else ordinal_map[item.contribution_id])

    target_registry = tuple(contribution(item, sealed=False) for item in contributions)
    target_contributions = tuple(contribution(item, sealed=True) for item in source.contributions)
    details: dict[DetailRef, DetailRef] = {}
    prepared = []
    for source_ref in _selected_detail_refs(source):
        source_ref.require_owner(source_session_id, source_thread_id)
        target_key = maps.get("detail", {}).get(detail_ref_key(source_ref))
        if target_key is None:
            raise RuntimeError("source-mismatch: fork detail 缺少 prepared journal mapping")
        target_ref = detail_ref_from_key(target_key)
        target_ref.require_owner(
            target_session_id, target_thread_id, local("assembly", source_ref.assembly_id),
        )
        record = source_details.get(source_ref)
        if record is None:
            raise RuntimeError("source-mismatch: fork 冻结 detail snapshot 缺失")
        artifact = detail_capability.prepare_detail(source_record=record, target_ref=target_ref)
        prepared.append(PreparedDetail(artifact.record, artifact.manifest_bytes))
        details[source_ref] = target_ref

    included_ids = {entry.ref.ref_id for entry in source.selection if entry.included}

    def context_ref(ref: ContextRef) -> ContextRef:
        if ref.ref_type == "canonical_item":
            target_id = local("item", ref.ref_id, required=ref.ref_id in included_ids)
            item = target_items.get(target_id)
            fields = {} if item is None else {
                "content_hash": item.content_hash,
                "content_length": payload_content_length(item.payload_kind, item.payload),
                "source_revision": (item.source_manifest.source_revision if item.source_manifest else None)
                or f"canonical:{item.item_id}:{item.content_hash}", "item_sequence": item.item_sequence,
            }
            return replace(ref, session_id=target_session_id, thread_id=target_thread_id,
                           ref_id=target_id, **fields)
        source_ref = ref.source_ref
        if isinstance(source_ref, DetailRef):
            source_ref = details.get(source_ref)
            if source_ref is None:
                raise RuntimeError("source-mismatch: fork source detail alias 缺少有效 selection")
        else:
            source_ref = local("request_ref", source_ref)
        return replace(ref, session_id=target_session_id, thread_id=target_thread_id,
                       plan_id=target_plan, ref_id=local("request_ref", ref.ref_id), source_ref=source_ref,
                       source_overlay_epoch=(
                           next(value[3] for value in overlay_bindings.values()
                                if source_ref in value[1:3]) if ref.base_delta_role in {"base", "delta"}
                           else ref.source_overlay_epoch))

    target_refs = tuple(context_ref(ref) for ref in source.refs)
    refs_by_id = {ref.ref_id: ref for ref in target_refs}
    tools = tuple(replace(ref, session_id=target_session_id, thread_id=target_thread_id,
                          plan_id=target_plan, assembly_id=target_assembly,
                          ref_id=local("tool_set", ref.ref_id)) for ref in source.tool_set_refs)
    tool_by_id = {ref.ref_id: ref for ref in tools}
    selection = []
    for entry in source.selection:
        if entry.ref.ref_type == "tool_set":
            target_id = local("tool_set", entry.ref.ref_id)
            ref = tool_by_id.get(target_id)
            if ref is None:
                ref = replace(entry.ref, session_id=target_session_id, thread_id=target_thread_id,
                              plan_id=target_plan, assembly_id=target_assembly, ref_id=target_id)
        else:
            ref = refs_by_id[local("item" if entry.ref.ref_type == "canonical_item" else "request_ref",
                                  entry.ref.ref_id)]
        selection.append(replace(
            entry, assembly_id=target_assembly, ref=ref,
            contribution_id=local("context_contribution", entry.contribution_id),
            detail_ref=details.get(entry.detail_ref), source_revision=ref.source_revision,
            content_hash=ref.content_hash, content_length=ref.content_length,
            source_overlay_epoch=ref.source_overlay_epoch if isinstance(ref, ContextRef) else None,
        ))
    selection = tuple(selection)
    binding = source.seal_binding
    state = initial_epoch_state(provider_profile=binding.prefix.state.provider_profile,
                                tool_compatibility_key=binding.prefix.state.tool_compatibility_key,
                                turn_id=target_turn)
    root = None if binding.root is None else replace(
        binding.root, epoch=state.epoch, sources=tuple(replace(
            provenance,
            source_ordinal=next(ordinal_map[item.contribution_id] for item in contributions
                               if item.source_binding is not None
                               and item.source_binding.source_ref == provenance.source_identity
                               and item.source_revision == provenance.revision),
        ) for provenance in binding.root.sources))
    frames = []
    for frame in binding.prefix.state.frames:
        ref = frame.item_ref
        if frame.role == "system":
            if root is None:
                raise RuntimeError("source-mismatch: fork system frame 缺少真实冻结 root")
            frames.append(replace(frame, content=root.content, item_ref=AppendedItemRef(
                "source_item", f"root:{state.epoch.token}", "compiled-root", root.frame_hash(), "tracked")))
        elif ref.ref_type == "canonical_item":
            group = tuple(target_items[local("item", item_id, required=True)]
                          for item_id in frame.content["item_ids"])
            localized, _ = project_canonical_prefix_frames(
                group, provider_profile=frame.provider_profile, start_ordinal=frame.ordinal - 1,
            )
            frames.extend(localized)
        else:
            # context/history fork 只继承已封存快照；目标 CSM 独立建立 tracking。
            frames.append(replace(frame, item_ref=replace(
                ref, ref_id=local("request_ref", ref.ref_id),
                source_identity=maps.get("source_identity", {}).get(ref.source_identity, ref.source_identity),
                tracking_state="not_tracked")))
    state = append_items(state, frames, desired_provider_profile=state.provider_profile,
                         desired_tool_compatibility_key=state.tool_compatibility_key)
    target_binding = replace(binding, prefix=seal_assembly(state, assembly_id=target_assembly),
                             root=root, tool_set_snapshot_id=local("tool_set", binding.tool_set_snapshot_id),
                             previous_assembly_id=None, source_revisions=())
    target = replace(source, session_id=target_session_id, thread_id=target_thread_id,
                     assembly_id=target_assembly, plan_id=target_plan, execution_id=target_execution,
                     turn_id=target_turn, model_call_id=target_model, active_view_id=target_view,
                     history_view_revision=meta[0], source_overlay_epoch=meta[1], refs=target_refs,
                     contributions=target_contributions, tool_set_refs=tools, selection=selection,
                     seal_binding=target_binding)
    target = replace(target, plan_hash=target.as_sealed_plan().plan_hash(),
                     request_hash=context_request_hash(
                         target.as_sealed_plan(), target.provider_version, projector_id=target.projector_id,
                         projector_version=target.projector_version, target_format=target.target_format,
                         wire_request=target.request_hash_preimage))
    target.validate_hashes()
    provenance = {"source_session_id": source_session_id, "source_plan_id": source.plan_id,
                  "source_assembly_id": source.assembly_id, "source_snapshot_hash": sha256_jcs(source.to_dict()),
                  "source_schema_version": source_schema, "audit_id": fork_id}
    for entity, mapping in maps.items():
        for source_id, target_id in mapping.items():
            if source_id in existing_maps.get(entity, set()):
                continue
            service._ensure_fork_identity_mapping(
                connection, source_session_id=source_session_id, target_session_id=target_session_id,
                fork_id=fork_id, entity_type=entity, source_id=source_id, target_id=target_id,
                lineage={"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                    "local_id": source_id},
                         "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                    "local_id": target_id},
                         **({"source_snapshot": provenance} if entity == "assembly" else {})}, timestamp=timestamp,
            )
    for item in target_registry:
        connection.execute(
            "INSERT INTO context_contributions(contribution_id,source_kind,source_revision,content_hash,content_length,"
            "request_only,contribution_kind,visibility,protection,selection_role,replacement_policy,source_ordinal,"
            "binding_manifest_json,created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?)",
            (item.contribution_id,item.source_kind,item.source_revision,item.content_hash,item.content_length,
             item.contribution_kind,item.visibility,item.protection,item.selection_role,item.replacement_policy,
             item.source_ordinal,canonical_json_text(item.binding_manifest()),timestamp),
        )
    for detail in prepared:
        service._context.assemblies._register_context_plan_detail(connection, detail.record, session_id=target_session_id,
                                              thread_id=target_thread_id, checkpoint_ns=checkpoint_ns)
    connection.execute(
        "INSERT INTO context_assemblies(assembly_id,session_id,thread_id,turn_id,execution_id,plan_id,plan_hash,"
        "request_hash,history_view_revision,source_overlay_epoch,snapshot_json,model_call_id,status,outcome,created_at,"
        "sealed_at,terminal_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'terminal',?,?,?,?)",
        (target_assembly,target_session_id,target_thread_id,target_turn,target_execution,target_plan,target.plan_hash,
         target.request_hash,meta[0],meta[1],canonical_json_text(target.to_dict()),target_model,outcome,timestamp,timestamp,timestamp),
    )
    service._context.assemblies._write_context_assembly_manifest(connection, target, checkpoint_ns=checkpoint_ns)
    sources_by_id = {ref.ref_id: ref for ref in registered_refs}
    if any(sources_by_id.get(ref.ref_id) != ref for ref in source.refs):
        raise RuntimeError("source-mismatch: fork selection 与冻结 plan source refs 不一致")
    write_imported_registration(
        connection, target, detail_key=None, origin="fork_import", source_provenance=provenance,
        source_manifest=build_source_manifest(
            session_id=target_session_id, thread_id=target_thread_id, plan_id=target_plan,
            refs=target_refs, contributions=target_registry),
        seal_idempotency_key=service._full_copy_identity(fork_id,target_session_id,"plan_seal",source.plan_id),
    )
    service._context.assemblies._validate_context_assembly_manifest(
        connection, target, registration=read_registration(connection,target_session_id,target_thread_id,target_plan),
        checkpoint_ns=checkpoint_ns,
    )
    connection.execute("UPDATE model_calls SET assembly_id=? WHERE model_call_id=?", (target_assembly,target_model))
    connection.execute(
        "UPDATE checkpoint_context_source_snapshots SET source_assembly_id=? "
        "WHERE session_id=? AND thread_id=? AND checkpoint_id=("
        "SELECT head_checkpoint_id FROM branches WHERE head_view_id=?)",
        (target_assembly,target_session_id,target_thread_id,target_view),
    )
    return tuple(prepared)
