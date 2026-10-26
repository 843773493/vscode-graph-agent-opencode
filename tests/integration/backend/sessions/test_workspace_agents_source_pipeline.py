"""AGENTS 真实文件 owner、语义派生、冻结与后端冷恢复闭环。"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from app.agents.workspace_agents import (
    publish_workspace_agents,
    register_workspace_agents_source,
)
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.serialization import normalize_wire_request
from app.services.business.resource_derivations.agents import (
    WORKSPACE_AGENTS_RESOURCE_ID,
    WORKSPACE_AGENTS_SOURCE_ID,
    agents_content,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    workspace_agent_spec_display_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
    _wait_until,
    _wire_text,
    cold_fork_deadlines,
    restart_source_case,
    source_case,
)
from tests.support.api_helpers import create_session
from tests.support.fork_deadline import ForkDeadlineRegistry

__all__ = ["cold_fork_deadlines", "restart_source_case", "source_case"]


def _messages(wire: dict[str, object]) -> list[dict[str, object]]:
    messages = wire["messages"]
    assert isinstance(messages, list) and all(isinstance(message, dict) for message in messages)
    return messages


def _has_prompt(assembly: ContextAssemblySnapshot, prompt: str) -> bool:
    assert isinstance(assembly.request_hash_preimage, dict)
    return prompt in _wire_text(assembly.request_hash_preimage["messages"])


def _assert_sealed_agents_facets(
    saver: RolloutCheckpointSaver, assembly: ContextAssemblySnapshot, content: str,
) -> None:
    """从真实 assembly binding/detail 读回语义与原来源 lineage，不重新解析文件。"""
    activation = saver.load_sealed_resource_activation(
        assembly.session_id, thread_id=assembly.thread_id, assembly_id=assembly.assembly_id,
        plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
    )
    agents = [binding for binding in activation.bindings if binding.resource_kind == "workspace_agents"]
    assert sorted(binding.facet for binding in agents) == ["chain", "raw-file"]
    chain = next(binding for binding in agents if binding.facet == "chain")
    raw = next(binding for binding in agents if binding.facet == "raw-file")
    assert chain.resource_id == WORKSPACE_AGENTS_RESOURCE_ID and chain.display_uri is None
    assert raw.display_uri is not None and raw.display_uri.endswith("/agent-spec/root/AGENTS.md")
    assert chain.revision == raw.revision == sha256_jcs({"content": content})
    assert chain.source_lineage_ref.sources == ((raw.resource_id, raw.revision),)
    assert len(raw.source_lineage_ref.sources) == 1
    raw_source_id = raw.source_lineage_ref.sources[0][0]
    assert raw_source_id == raw.resource_id.removeprefix("workspace-agents:raw-file:")
    assert raw_source_id != raw.display_uri
    for binding in agents:
        payload = saver.read_resource_activation_payload(
            activation, binding, storage_owner=(assembly.session_id, assembly.thread_id),
        )
        assert agents_content(payload) == content


@pytest.mark.asyncio
async def test_agents_chain_uses_registered_exact_file_facets_without_source_io(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_owner = source_case.container.workspace_file_resource_registry
    workspace = file_owner.workspace_root
    workspace_id = load_or_create_workspace_id(workspace)
    root_body, child_body = "\ufeff根指令\r\n保留空白  \n", "子目录规则🧭\n"
    (workspace / "AGENTS.md").write_bytes(root_body.encode())
    child = workspace / "agents-chain-child"
    child.mkdir()
    (child / "AGENTS.md").write_bytes(child_body.encode())
    unknown = workspace / "agents-chain-unknown"
    unknown.mkdir()
    (unknown / "AGENTS.md").write_bytes(b"\xff")
    file_uris = register_workspace_agents_source(file_owner, workspace_id=workspace_id)
    root_source = file_owner.source_descriptor_for_uri(file_uris[0])
    assert root_source.source_id == f"workspace-agents-source:{workspace_id}"
    assert root_source.entry_identity == f"workspace-agents-entry:{workspace_id}"
    child_uri = resource_display_uri(
        scope="workspace", scope_id=workspace_id, kind="files",
        tail_segments=("root", child.name, "AGENTS.md"),
    )
    file_owner.register_file(
        uri=child_uri,
        path=child / "AGENTS.md",
        source_id=f"workspace-agents-source:{workspace_id}:{child.name}",
        entry_identity=f"workspace-agents-entry:{workspace_id}:{child.name}",
    )
    registry = ResourceRegistry()

    def forbid_io(*_args, **_kwargs):
        raise AssertionError("AGENTS graph 不得重新读取来源或扫描目录")

    with monkeypatch.context() as patch:
        patch.setattr(file_owner._reconciler._reader, "read", forbid_io)
        snapshot = publish_workspace_agents(
            registry=registry, file_registry=file_owner, file_uris=(*file_uris, child_uri),
        )
    assert snapshot.available and snapshot.display_uri is None
    assert agents_content(snapshot.payload) == root_body + "\n\n" + child_body
    raw = [item for item in registry.snapshots() if item.facet == "raw-file"]
    assert {agents_content(item.payload) for item in raw} == {root_body, child_body}
    assert {item.display_uri for item in raw} == {*file_uris, child_uri}
    for item in raw:
        assert item.display_uri is not None
        accepted = file_owner.observed_revision(file_owner.source_id_for_uri(item.display_uri))
        assert item.source_lineage == ((accepted.source_id, accepted.revision),)
        assert accepted.revision == "sha256:" + hashlib.sha256(accepted.content.encode()).hexdigest()
    assert snapshot.source_lineage == tuple(sorted((item.resource_id, item.revision) for item in raw))


@pytest.mark.asyncio
async def test_agents_absence_and_empty_raw_revisions_share_unchanged_semantic_facet(
    source_case: RuntimeSourceCase,
) -> None:
    file_owner = source_case.container.workspace_file_resource_registry
    path = file_owner.workspace_root / "AGENTS.md"
    path.unlink(missing_ok=True)
    file_uris = register_workspace_agents_source(
        file_owner, workspace_id=load_or_create_workspace_id(file_owner.workspace_root),
    )
    absent = file_owner.observed_revision(file_owner.source_id_for_uri(file_uris[0]))
    registry = ResourceRegistry()
    first = publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=file_uris)
    path.write_bytes(b"")
    file_owner.refresh(file_uris[0])
    present = file_owner.observed_revision(file_owner.source_id_for_uri(file_uris[0]))
    second = publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=file_uris)
    assert absent.revision != present.revision and not absent.exists and present.exists
    assert first == second and agents_content(second.payload) == ""


@pytest.mark.asyncio
async def test_agents_invalid_or_unregistered_source_blocks_chain_and_preserves_valid(
    source_case: RuntimeSourceCase,
) -> None:
    file_owner = source_case.container.workspace_file_resource_registry
    path = file_owner.workspace_root / "AGENTS.md"
    path.write_text("有效指令\n", encoding="utf-8")
    workspace_id = load_or_create_workspace_id(file_owner.workspace_root)
    file_uris = register_workspace_agents_source(file_owner, workspace_id=workspace_id)
    registry = ResourceRegistry()
    first = publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=file_uris)
    other_workspace = resource_display_uri(
        scope="workspace", scope_id="other-workspace", kind="files", tail_segments=("root", "AGENTS.md"),
    )
    with pytest.raises(ValueError, match="同一 Workspace"):
        publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=(*file_uris, other_workspace))
    assert registry.snapshot(WORKSPACE_AGENTS_RESOURCE_ID) is first
    unknown = resource_display_uri(
        scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root", "unregistered", "AGENTS.md"),
    )
    with pytest.raises(KeyError, match="工作区资源尚未注册"):
        publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=(*file_uris, unknown))
    assert registry.last_valid_snapshot(WORKSPACE_AGENTS_RESOURCE_ID) == first
    path.write_bytes(b"\xff")
    file_owner.refresh(file_uris[0])
    invalid = publish_workspace_agents(registry=registry, file_registry=file_owner, file_uris=file_uris)
    assert not invalid.available and invalid.error_code == "dependency-unavailable"
    failed = file_owner.observed_revision(file_owner.source_id_for_uri(file_uris[0]))
    assert failed.error_code == "invalid_utf8" and not failed.available
    assert file_uris[0] in failed.error and str(file_owner.workspace_root) not in failed.error
    assert failed.retained_revision is not None and failed.content == "有效指令\n"
    assert str(file_owner.workspace_root) not in repr(invalid)
    assert registry.last_valid_snapshot(WORKSPACE_AGENTS_RESOURCE_ID) == first
    assert len(registry.descriptors()) == 2


@pytest.mark.asyncio
async def test_live_agents_publication_appends_delta_without_changing_sealed_root(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    files = case.container.workspace_file_resource_registry
    first_body, second_body = "CIL_LIVE_AGENTS_A\n", "CIL_LIVE_AGENTS_B\r\n保留末尾空白  \n"
    path = files.workspace_root / "AGENTS.md"
    path.write_bytes(first_body.encode())
    published: list[ResourceSnapshot] = []
    original_publish = ResourceRegistry.publish

    def record_publication(registry: ResourceRegistry, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        accepted = original_publish(registry, snapshot)
        if accepted.resource_id == WORKSPACE_AGENTS_RESOURCE_ID:
            published.append(accepted)
        return accepted

    monkeypatch.setattr(ResourceRegistry, "publish", record_publication)
    session_id = await create_session(case.client, "CIL 同进程 AGENTS A→B")
    await case.plan(session_id, [{"content": "CIL 同进程回复"}, {"content": "CIL 同进程回复"}])
    first = await _send(case, session_id, "cil-live-first-private")
    assert isinstance(first["job_id"], str)
    await _job(case, first["job_id"])
    saver = case.container.checkpointer
    thread_id = first["thread_id"]
    assert isinstance(thread_id, str)
    first_assembly, = saver.list_context_assemblies(session_id, thread_id)
    _assert_sealed_agents_facets(saver, first_assembly, first_body)
    first_bytes = canonical_json_bytes(first_assembly.to_dict())
    assert first_assembly.seal_binding is not None
    assert first_assembly.seal_binding.root is not None
    root_bytes = canonical_json_bytes(first_assembly.seal_binding.root.frame_manifest())
    owner = ContextSourceOwnerKey(session_id, thread_id)
    before_control = next(state for state in saver.load_context_source_control_states(owner)
                          if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
    assert before_control.latest_revision == before_control.latest_visible_committed_revision == sha256_jcs({"content": first_body})
    requests_before = len(case.requests())
    path.write_bytes(second_body.encode())
    uri = workspace_agent_spec_display_uri(load_or_create_workspace_id(files.workspace_root))
    files.refresh(uri)
    await _wait_until(lambda: any(snapshot.available and agents_content(snapshot.payload) == second_body
                                  for snapshot in published))
    # 文件 owner 和语义图已发布 B，但 source 控制与请求只能在真正 activation 推进。
    assert len(case.requests()) == requests_before
    assert next(state for state in saver.load_context_source_control_states(owner)
                if state.source_id == WORKSPACE_AGENTS_SOURCE_ID) == before_control
    second = await _send(case, session_id, "cil-live-second-private")
    assert isinstance(second["job_id"], str)
    await _job(case, second["job_id"])
    assert second["thread_id"] == thread_id
    assemblies = saver.list_context_assemblies(session_id, thread_id)
    second_assembly, = [assembly for assembly in assemblies if assembly.assembly_id != first_assembly.assembly_id]
    _assert_sealed_agents_facets(saver, second_assembly, second_body)
    assert second_assembly.seal_binding is not None
    revision, = [state for state in second_assembly.seal_binding.source_revisions
                 if state.source_id == WORKSPACE_AGENTS_SOURCE_ID]
    assert revision.previous_revision == before_control.latest_visible_committed_revision
    assert revision.revision == sha256_jcs({"content": second_body})
    assert revision.revision != revision.previous_revision
    frame, = [frame for frame in second_assembly.seal_binding.prefix.state.frames
              if frame.item_ref.source_identity == WORKSPACE_AGENTS_SOURCE_ID]
    expected_delta = (
        f"--- AGENTS.md@{revision.previous_revision}\n"
        f"+++ AGENTS.md@{revision.revision}\n"
        "@@ -1 +1,2 @@\n"
        "-CIL_LIVE_AGENTS_A\n"
        "+CIL_LIVE_AGENTS_B\r\n"
        "+保留末尾空白  \n"
    )
    assert frame.role == "user"
    assert [(block.source_identity, block.body) for block in parse_source_blocks(frame.content)] == [(WORKSPACE_AGENTS_SOURCE_ID, expected_delta)]
    assert second_assembly.seal_binding.root is not None
    assert canonical_json_bytes(second_assembly.seal_binding.root.frame_manifest()) == root_bytes
    restored = saver.get_context_assembly(session_id, thread_id, assembly_id=first_assembly.assembly_id)
    assert canonical_json_bytes(restored.to_dict()) == first_bytes
    wire = case.requests()[-1]
    assert normalize_wire_request(wire) == second_assembly.request_hash_preimage
    assert {"role": "user", "content": frame.content} in _messages(wire)
    case.write_actual("live-agents-a-to-b", {
        "first_assembly_id": first_assembly.assembly_id, "second_assembly_id": second_assembly.assembly_id,
        "first_assembly_hash": hashlib.sha256(first_bytes).hexdigest(),
        "first_root_hash": hashlib.sha256(root_bytes).hexdigest(),
        "previous_revision": revision.previous_revision, "revision": revision.revision,
        "source_delta": expected_delta,
        "published_chain_revisions": [snapshot.revision for snapshot in published], "second_wire": wire,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"managed_gateway": True, "terminal": True, "browser": True}], indirect=True)
async def test_agents_full_copy_target_restores_sealed_chain_before_real_provider(
    source_case: RuntimeSourceCase, cold_fork_deadlines: ForkDeadlineRegistry,
) -> None:
    case = source_case
    body = "CIL_FORK_AGENTS 精确恢复来源链。\r\n保留空白  \n"
    (case.container.workspace_file_resource_registry.workspace_root / "AGENTS.md").write_bytes(body.encode())
    source_session = await create_session(case.client, "CIL AGENTS fork 来源")
    await case.plan(source_session, [{"content": "CIL fork 目标回复"}])
    first = await _send(case, source_session, "cil-fork-source-private")
    assert isinstance(first["job_id"], str) and isinstance(first["thread_id"], str)
    await _job(case, first["job_id"])
    source_thread = first["thread_id"]
    saver = case.container.checkpointer
    original, = saver.list_context_assemblies(source_session, source_thread)
    _assert_sealed_agents_facets(saver, original, body)
    original_bytes = canonical_json_bytes(original.to_dict())
    source_owner = ContextSourceOwnerKey(source_session, source_thread)
    original_control = saver.load_context_source_control_states(source_owner)
    point_response = await case.client.get(
        f"/api/v1/sessions/{source_session}/fork-source-point", params={"mode": "full_rollout_copy"},
    )
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    key = "cil-agents-full-copy-provider"
    deadline = cold_fork_deadlines.for_operation(point["main_container_uri"], key)
    requests_before = len(case.requests())
    response = await deadline.wait(case.client.post(
        f"/api/v1/sessions/{source_session}/fork-context", json={
            "idempotency_key": key, "source_uri": point["source_uri"],
            "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
            "mode": "full_rollout_copy", "target_container_uri": point["main_container_uri"],
            "creation_kind": "new_session_main", "title": "CIL AGENTS fork 目标",
        },
    ), "AGENTS full-copy public HTTP commit")
    assert response.status_code == 200, response.text
    cold_fork_deadlines.complete(deadline)
    target = response.json()["data"]
    target_session, target_thread = target["session"]["session_id"], target["thread_id"]
    assert target_session != source_session and target_thread != source_thread
    assert len(case.requests()) == requests_before
    restored = saver.get_context_assembly(target_session, target_thread, assembly_id=original.assembly_id)
    assert canonical_json_bytes(restored.to_dict()) == original_bytes
    copied = saver.load_sealed_resource_activation(
        target_session, thread_id=target_thread, assembly_id=original.assembly_id,
        plan_hash=original.plan_hash, request_hash=original.request_hash,
    )
    copied_agents = [binding for binding in copied.bindings if binding.resource_kind == "workspace_agents"]
    assert sorted(binding.facet for binding in copied_agents) == ["chain", "raw-file"]
    for binding in copied_agents:
        assert agents_content(saver.read_resource_activation_payload(
            copied, binding, storage_owner=(target_session, target_thread),
        )) == body
    await case.plan(target_session, [{"content": "CIL fork 目标回复"}], thread_id=target_thread)
    sent = await _send(case, target_session, "cil-fork-target-private")
    assert isinstance(sent["job_id"], str) and sent["thread_id"] == target_thread
    await _job(case, sent["job_id"])
    current, = [assembly for assembly in saver.list_context_assemblies(target_session, target_thread)
                if (assembly.session_id, assembly.thread_id) == (target_session, target_thread)]
    _assert_sealed_agents_facets(saver, current, body)
    wire = case.requests()[-1]
    assert len(case.requests()) == requests_before + 1
    assert normalize_wire_request(wire) == current.request_hash_preimage
    assert sum(_wire_text(message["content"]).count("CIL_FORK_AGENTS") for message in _messages(wire)) == 1
    control, = [state for state in saver.load_context_source_control_states(ContextSourceOwnerKey(target_session, target_thread))
                if state.source_id == WORKSPACE_AGENTS_SOURCE_ID]
    assert control.latest_revision == control.latest_visible_committed_revision == sha256_jcs({"content": body})
    assert saver.load_context_source_control_states(source_owner) == original_control
    for session_id, thread_id in ((source_session, source_thread), (target_session, target_thread)):
        assert canonical_json_bytes(saver.get_context_assembly(
            session_id, thread_id, assembly_id=original.assembly_id,
        ).to_dict()) == original_bytes
    assert case.sql(target_session, target_thread,
                    "SELECT session_id,thread_id,execution_id,assembly_id FROM model_calls WHERE model_call_id=?",
                    (current.model_call_id,)) == [(target_session, target_thread, current.execution_id, current.assembly_id)]
    case.write_actual("fork-agents-target-provider", {
        "source_session_id": source_session, "source_thread_id": source_thread,
        "target_session_id": target_session, "target_thread_id": target_thread,
        "original_assembly_id": original.assembly_id, "original_assembly_hash": hashlib.sha256(original_bytes).hexdigest(),
        "target_assembly_id": current.assembly_id, "target_wire": wire, "fork_response": target,
    })


@pytest.mark.parametrize(
    "agents_after,restart_source_case",
    [(variant, {"agents_content": "CIL_OLD_AGENTS 已完成首帧。\n"})
     for variant in ("unchanged", "changed", "empty", "absent")],
    indirect=["restart_source_case"], ids=["unchanged", "changed", "empty", "absent"],
)
@pytest.mark.asyncio
async def test_cold_agents_restart_reconciles_before_second_provider(
    restart_source_case: RestartRuntimeSourceCase, agents_after: str,
) -> None:
    case = restart_source_case
    first_body = "CIL_OLD_AGENTS 已完成首帧。\n"
    current_body = "CIL_NEW_AGENTS 停机期间的新指令。\n" if agents_after == "changed" else (
        "" if agents_after in {"empty", "absent"} else first_body
    )
    first_prompt, second_prompt = "cil-first-private", "cil-second-private"
    session_id = await create_session(case.client, "CIL 冷 AGENTS 首次绑定")
    await case.plan(session_id, [{"content": "CIL 同 owner 回复"}])
    first = await _send(case, session_id, first_prompt)
    assert isinstance(first["job_id"], str)
    await _job(case, first["job_id"])
    catalog = await case.client.get("/api/v1/session-catalog/export")
    assert catalog.status_code == 200, catalog.text
    node = next(item for item in catalog.json()["data"]["items"] if item["node_id"] == session_id)
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    thread_node = resolver.resolve_thread_node(session_id, resolver.main_thread_id(session_id))
    assert "storage_relative_path" not in node
    assert resolver.resolve_session_node(session_id).name == session_id
    sessions = case.workspace / ".boxteam/sessions"
    with RolloutCheckpointSaver(sessions) as cold:
        owner = cold.forks.source.resolve_main_thread_owner(session_id)
        owner_key = ContextSourceOwnerKey(owner.session_id, owner.thread_id)
        assert owner.thread_id != session_id
        assemblies = cold.list_context_assemblies(session_id, owner.thread_id)
        first_candidates = [snapshot for snapshot in assemblies if _has_prompt(snapshot, first_prompt)]
        assert len(first_candidates) == 1
        first_assembly = first_candidates[0]
        _assert_sealed_agents_facets(cold, first_assembly, first_body)
        assert first_assembly.seal_binding is not None
        applied = [revision for revision in first_assembly.seal_binding.source_revisions
                   if revision.source_id == WORKSPACE_AGENTS_SOURCE_ID]
        assert len(applied) == 1 and applied[0].previous_revision is None
        old_revision = applied[0].revision
        assert old_revision == sha256_jcs({"content": first_body})
        before_control = next(state for state in cold.load_context_source_control_states(owner_key)
                              if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert before_control.latest_revision == before_control.latest_visible_committed_revision == old_revision
        assert before_control.tracking_status == "tracked"
        assert first_assembly.seal_binding.root is not None
        assert [(block.source_identity, block.body) for block in parse_source_blocks(first_assembly.seal_binding.root.content)
                if block.source_identity == WORKSPACE_AGENTS_SOURCE_ID] == [(WORKSPACE_AGENTS_SOURCE_ID, first_body)]
        root_bytes = canonical_json_bytes(first_assembly.seal_binding.root.frame_manifest())
        first_bytes = canonical_json_bytes(first_assembly.to_dict())
    first_requests = [request for request in case.requests()
                      if first_prompt in _wire_text(request["messages"])]
    assert len(first_requests) == 1
    assert normalize_wire_request(first_requests[0]) == first_assembly.request_hash_preimage

    def change_agents_while_stopped() -> None:
        path = case.workspace / "AGENTS.md"
        if agents_after == "absent":
            path.unlink()
        else:
            path.write_text(current_body, encoding="utf-8")

    await case.restart(hard=True, while_stopped=change_agents_while_stopped)
    # 服务重建不能自行推进 desired 或 visible；第二次真实 send 才观察并 seal。
    with RolloutCheckpointSaver(sessions) as cold:
        assert cold.forks.source.resolve_main_thread_owner(session_id) == owner
        restored_control = next(state for state in cold.load_context_source_control_states(owner_key)
                                if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert restored_control == before_control
        restored_first = cold.get_context_assembly(session_id, owner.thread_id, assembly_id=first_assembly.assembly_id)
        assert canonical_json_bytes(restored_first.to_dict()) == first_bytes
        _assert_sealed_agents_facets(cold, restored_first, first_body)
    request_boundary = len(case.requests())
    await case.plan(session_id, [{"content": "CIL 同 owner 回复"}])
    second = await _send(case, session_id, second_prompt)
    assert isinstance(second["job_id"], str)
    assert second["thread_id"] == owner.thread_id
    await _job(case, second["job_id"])
    second_requests = [request for request in case.requests()[request_boundary:]
                       if second_prompt in _wire_text(request["messages"])]
    assert len(second_requests) == 1
    with RolloutCheckpointSaver(sessions) as cold:
        assert cold.forks.source.resolve_main_thread_owner(session_id) == owner
        second_candidates = [snapshot for snapshot in cold.list_context_assemblies(session_id, owner.thread_id)
                             if _has_prompt(snapshot, second_prompt)]
        assert len(second_candidates) == 1
        second_assembly = second_candidates[0]
        assert second_assembly.seal_binding is not None
        _assert_sealed_agents_facets(cold, second_assembly, current_body)
        assert second_assembly.seal_binding.root is not None
        assert canonical_json_bytes(second_assembly.seal_binding.root.frame_manifest()) == root_bytes
        assert (second_assembly.session_id, second_assembly.thread_id) == (session_id, owner.thread_id)
        assert normalize_wire_request(second_requests[0]) == second_assembly.request_hash_preimage
        current = next(state for state in cold.load_context_source_control_states(owner_key)
                       if state.source_id == WORKSPACE_AGENTS_SOURCE_ID)
        assert current.latest_revision == current.latest_visible_committed_revision
        revisions = [revision for revision in second_assembly.seal_binding.source_revisions
                     if revision.source_id == WORKSPACE_AGENTS_SOURCE_ID]
        new_frames = [frame for frame in second_assembly.seal_binding.prefix.state.frames
                      if frame.item_ref.source_identity == WORKSPACE_AGENTS_SOURCE_ID
                      and frame.item_ref.source_revision == current.latest_revision]
        if agents_after == "unchanged":
            assert revisions == [] and new_frames == []
            assert current == before_control
            assert any(block.source_identity == WORKSPACE_AGENTS_SOURCE_ID and block.body == first_body
                       for message in _messages(second_requests[0]) if message["role"] == "system"
                       for block in parse_source_blocks(message["content"]))
        else:
            assert current.latest_revision != old_revision
            assert [(revision.previous_revision, revision.revision) for revision in revisions] == [
                (old_revision, current.latest_revision),
            ]
            assert len(new_frames) == 1 and new_frames[0].role == "user"
            expected_delta = (
                f"--- AGENTS.md@{old_revision}\n+++ AGENTS.md@{current.latest_revision}\n"
                + ("@@ -1 +1 @@\n-CIL_OLD_AGENTS 已完成首帧。\n+CIL_NEW_AGENTS 停机期间的新指令。\n"
                   if agents_after == "changed" else "@@ -1 +0,0 @@\n-CIL_OLD_AGENTS 已完成首帧。\n")
            )
            assert [(block.source_identity, block.body) for block in parse_source_blocks(new_frames[0].content)] == [(WORKSPACE_AGENTS_SOURCE_ID, expected_delta)]
            assert {"role": "user", "content": new_frames[0].content} in _messages(second_requests[0])
            assert current.state_revision > before_control.state_revision
        restored_first = cold.get_context_assembly(session_id, owner.thread_id, assembly_id=first_assembly.assembly_id)
        assert canonical_json_bytes(restored_first.to_dict()) == first_bytes
    with sqlite3.connect((thread_node / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        model_owner = connection.execute(
            "SELECT session_id,thread_id,execution_id,assembly_id FROM model_calls WHERE model_call_id=?",
            (second_assembly.model_call_id,),
        ).fetchall()
    assert model_owner == [(session_id, owner.thread_id, second_assembly.execution_id, second_assembly.assembly_id)]
    case.write_actual(f"cold-agents-{agents_after}", {
        "session_id": session_id, "thread_id": owner.thread_id, "source_id": WORKSPACE_AGENTS_SOURCE_ID,
        "backend_pids": [handle.process.pid for handle in case.backends],
        "previous_revision": old_revision, "current_revision": current.latest_revision,
        "visible_revision": current.latest_visible_committed_revision,
        "first_assembly_id": first_assembly.assembly_id, "second_assembly_id": second_assembly.assembly_id,
        "first_assembly_hash": hashlib.sha256(first_bytes).hexdigest(),
        "first_root_hash": hashlib.sha256(root_bytes).hexdigest(),
        "source_receipts": [{"previous_revision": revision.previous_revision, "revision": revision.revision}
                            for revision in revisions],
        "source_frames": [{"role": frame.role, "content": frame.content} for frame in new_frames],
        "second_wire": second_requests[0], "model_owner": model_owner,
    })


