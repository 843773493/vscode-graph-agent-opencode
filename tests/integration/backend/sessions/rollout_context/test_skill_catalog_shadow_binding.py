"""真实 Skill owner 的同名覆盖不得改写历史 tracked/sealed binding。"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents.skill_runtime import (
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
    start_workspace_skill_catalog_publication,
)
from app.agents.tools.skill_loading import create_skill_load_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import get_boxteam_home
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.resource_platform.bootstrap import (
    bootstrap_resource_platform,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    SkillCatalogSnapshotConflict,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 使用当前文件的正式隔离工作区
)
from tests.support.gateway_skill_sources import accepted_gateway_skill_sources

GATEWAY_ID = "gateway_0123456789abcdef0123456789abcdef"
GATEWAY_BODY = "Gateway shared 原始正文\n"
WORKSPACE_BODY = "Workspace shared 高优先级正文\n"


def _write_shared_skill(root: Path, description: str, body: str) -> None:
    source = root / "shared" / "SKILL.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        f"---\nname: shared\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


@pytest.fixture
async def skill_catalog_owner(registry_db, request):
    saver, session, _connection, accepted = registry_db
    workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    workspace_skills = workspace / ".boxteam" / "skills"
    if workspace_skills.exists():
        shutil.rmtree(workspace_skills)
    workspace_skills.mkdir()
    _write_shared_skill(get_boxteam_home() / "skills", "Gateway 共享来源", GATEWAY_BODY)
    platform = bootstrap_resource_platform(workspace_root=workspace)
    files = platform.file_registry
    registry = ResourceRegistry()
    sources = accepted_gateway_skill_sources(
        skills_root=get_boxteam_home() / "skills", gateway_id=GATEWAY_ID,
        workspace_id=load_or_create_workspace_id(workspace),
        observation_channel=files.observation_channel,
    )
    catalog = build_workspace_skill_catalog(
        workspace, registry=registry, file_registry=files,
        gateway_id=GATEWAY_ID, gateway_sources=sources, bundled_skill_groups=(),
    )
    scope = LifetimeScope("same-name-skill-publication")
    thread = saver._storage.storage.main_thread_id(session)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, thread), control_state_port=saver,
    )
    middleware = WorkspaceSkillsMiddleware(
        catalog=catalog, context_source_manager=manager, execution_owned_catalog=True,
    )
    middleware.before_agent({}, None, {})
    try:
        await files.start()
        start_workspace_skill_catalog_publication(
            catalog, registry=registry, file_registry=files,
            lifetime_scope=scope, gateway_sources=sources,
        )
        yield saver, session, thread, accepted, registry, files, manager
    finally:
        await scope.close()
        await platform.close()


async def test_workspace_shadow_keeps_gateway_tracked_and_sealed_binding(
    skill_catalog_owner, frozen_provider_wire, frozen_provider_activation,
):
    saver, session, thread, accepted, registry, files, manager = skill_catalog_owner
    owner = ContextSourceOwnerKey(session, thread)
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    policy = ResourceActivationPolicySnapshot(
        default_boundary="turn", boundaries={"skill": "model_call"},
    )
    tool_set = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())

    def seal(key: str) -> ContextAssemblySnapshot:
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            prepared = saver.prepare_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key=f"seal:{key}",
                context_source_manager=manager, tool_set_supplier=lambda: tool_set,
                **frozen_provider_wire("same-name-skill", "chat_completions"),
                **frozen_provider_activation(
                    saver, plan_creation_idempotency_key=key, registry=registry, policy=policy,
                ),
            )
        snapshot = prepared["snapshot"]
        assert isinstance(snapshot, ContextAssemblySnapshot)
        return snapshot

    def restored_catalog(reader: RolloutCheckpointSaver, sealed: ContextAssemblySnapshot):
        activation = reader.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
        )
        catalog = skill_catalog_from_activation(
            activation, lambda frozen, binding: reader.read_resource_activation_payload(
                frozen, binding, storage_owner=(session, thread),
            ),
        )
        assert catalog is not None
        return catalog

    initial = seal("gateway-catalog")
    gateway = restored_catalog(saver, initial).entries["shared"]
    assert gateway.entry_identity == f"skill-entry:gateway:{GATEWAY_ID}:shared"
    assert gateway.display_uri == f"boxteam://gateway/{GATEWAY_ID}/resources/skills/shared/SKILL.md"
    assert gateway.body == GATEWAY_BODY

    frozen_activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=initial.assembly_id,
        plan_hash=initial.plan_hash, request_hash=initial.request_hash,
    )
    metadata_ref = next(
        ref for ref in frozen_activation.bindings
        if ref.resource_kind == "skill" and ref.facet == "metadata"
    )
    activation_ref = next(
        ref for ref in frozen_activation.bindings
        if ref.resource_kind == "skill" and ref.facet == "activation"
        and ref.display_uri == metadata_ref.display_uri
    )
    mismatched_activation = replace(
        activation_ref, resource_id=f"{metadata_ref.resource_id.removesuffix(':metadata')}:other",
    )
    malformed_activation = replace(
        frozen_activation,
        bindings=tuple(
            mismatched_activation if ref is activation_ref else ref
            for ref in frozen_activation.bindings
        ),
    )

    def read_malformed(_snapshot, binding):
        if binding.facet == "catalog":
            return {"entries": []}
        if binding.facet == "metadata":
            return {"name": "shared", "description": "Gateway 共享来源"}
        return {"body": GATEWAY_BODY}

    with pytest.raises(SkillCatalogSnapshotConflict, match="Skill metadata 正文非法"):
        skill_catalog_from_activation(malformed_activation, read_malformed)

    skill_load = create_skill_load_tool(manager, control_admission=lambda _name, _mode: nullcontext())
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        receipt = json.loads(await skill_load.ainvoke({"name": "shared", "mode": "tracked"}))
    assert (receipt["status"], receipt["tracked"], receipt["display_uri"], receipt["revision"]) == (
        "loaded", True, gateway.display_uri, gateway.activation_revision,
    )
    sealed = seal("gateway-tracked")
    assert sealed.seal_binding is not None
    gateway_frames = tuple(
        frame for frame in sealed.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == f"skill:{gateway.entry_identity}"
    )
    assert len(gateway_frames) == 1
    assert gateway_frames[0].item_ref.source_revision == gateway.activation_revision
    assert isinstance(gateway_frames[0].content, str)
    assert GATEWAY_BODY in gateway_frames[0].content
    original_catalog = restored_catalog(saver, sealed)
    assert original_catalog.entries["shared"] == gateway

    # 新目录必须先由真实 watcher 接受，再由后台 producer 发布；不手工拼 binding。
    catalog_id = "skill-entry:workspace:catalog:catalog"
    previous_revision = registry.snapshot(catalog_id).revision
    _write_shared_skill(files.workspace_root / ".boxteam" / "skills", "Workspace 同名覆盖", WORKSPACE_BODY)
    async with asyncio.timeout(5):
        while registry.snapshot(catalog_id).revision == previous_revision:
            await asyncio.sleep(0.01)
    current = seal("workspace-shadow")
    current_catalog = manager.frozen_skill_catalog()
    workspace = current_catalog.entries["shared"]
    assert workspace.entry_identity == "skill-entry:workspace:shared"
    assert workspace.display_uri == (
        f"boxteam://workspace/{load_or_create_workspace_id(files.workspace_root)}/resources/skills/shared/SKILL.md"
    )
    assert workspace.body == WORKSPACE_BODY
    assert workspace.activation_revision != gateway.activation_revision
    assert current_catalog.sources[gateway.display_uri] == gateway
    tracked = tuple(
        state for state in saver.load_context_source_control_states(owner)
        if state.tracking_status == "tracked" and state.source_kind == "skill"
    )
    assert len(tracked) == 1
    assert (tracked[0].source_id, tracked[0].latest_revision, tracked[0].latest_visible_committed_revision) == (
        f"skill:{gateway.entry_identity}", gateway.activation_revision, gateway.activation_revision,
    )
    assert current.seal_binding is not None
    assert tuple(
        frame for frame in current.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == f"skill:{gateway.entry_identity}"
    ) == gateway_frames

    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        historical = restored_catalog(restarted, sealed)
        assert historical == original_catalog
        assert historical.entries["shared"] == gateway
        assert restored_catalog(restarted, current).entries["shared"] == workspace
        restarted_manager = ContextSourceManager(owner=owner, control_state_port=restarted)
        historical_tool = create_skill_load_tool(
            restarted_manager, control_admission=lambda _name, _mode: nullcontext(),
            activation_snapshot_resolver=lambda: restored_catalog(restarted, sealed),
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            historical_receipt = json.loads(await historical_tool.ainvoke({"name": "shared", "mode": "tracked"}))
        assert historical_receipt["status"] == "already_active"
        assert historical_receipt["tracked"] is True
        assert historical_receipt["display_uri"] == gateway.display_uri
        assert historical_receipt["revision"] == gateway.activation_revision
        assert historical_receipt["source_rebound"] is False
        assert WORKSPACE_BODY not in json.dumps(historical_receipt, ensure_ascii=False)
        current_tool = create_skill_load_tool(
            restarted_manager, control_admission=lambda _name, _mode: nullcontext(),
            activation_snapshot_resolver=lambda: restored_catalog(restarted, current),
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            current_receipt = json.loads(await current_tool.ainvoke({"name": "shared"}))
        assert current_receipt["status"] == "rebound"
        assert current_receipt["display_uri"] == workspace.display_uri
        assert current_receipt["revision"] == workspace.activation_revision
        assert current_receipt["tracked"] is False
        assert current_receipt["source_rebound"] is True
        assert restored_catalog(restarted, sealed) == original_catalog
