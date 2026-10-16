"""唯一 MCP Owner 发布的 guidance/member 双通道；不建立独立 activation writer。"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import empty_checkpoint
from pydantic import Field, create_model

from app.agents.policy import ToolMetadata, ToolPolicyResolver
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.agents.tools.custom_invocation import create_extension_tool_invoker_tool
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.lifecycle import LifetimeScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import (
    ResourceActivationBoundary,
    ResourceActivationPolicySnapshot,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import (
    McpCatalogOwner,
    McpCatalogResourcePublisher,
)
from app.services.infrastructure.mcp.catalog_activation import (
    EXTENSION_CATALOG_SOURCE_ID,
    MCP_GUIDANCE_SOURCE_ID,
    MCP_OWNER_CATALOG_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import (
    preparation as seal_preparation,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 当前正式测试路径隔离 SQLite fixture
)
from tests.support.mcp_session_doubles import drain_pending_relists


class CatalogSession:
    def __init__(self) -> None:
        self.minimum = 1
        self.description = "回显给定文本"
        self.empty = False
        self.error: Exception | None = None
        self.list_calls = 0

    async def initialize(self) -> None:
        return None

    def supports_tool_list_changed(self) -> bool:
        return True

    async def list_tools(self) -> list[BaseTool]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        if self.empty:
            return []

        async def echo(text: str) -> str:
            return text

        return [StructuredTool.from_function(
            coroutine=echo, name="echo", description=self.description,
            args_schema=create_model("EchoInput", text=(str, Field(min_length=self.minimum))),
        )]


class CatalogSessionFactory:
    def __init__(self) -> None:
        self.session = CatalogSession()
        self.notify: Callable[[], None] | None = None

    @asynccontextmanager
    async def __call__(
        self, server: McpServerConfig, notify: Callable[[], None],
    ) -> AsyncIterator[CatalogSession]:
        assert server.server_id == "mini"
        self.notify = notify
        yield self.session

    def changed(self) -> None:
        assert self.notify is not None
        self.notify()


@pytest.fixture
def catalog_workspace(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    shutil.rmtree(context.workspace_root)
    shutil.copytree(
        context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        context.workspace_root,
    )
    return context.workspace_root


@pytest.fixture
async def catalog_runtime(catalog_workspace):
    factory = CatalogSessionFactory()
    owner = McpCatalogOwner(
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "controlled-server"}}},
        workspace_root=catalog_workspace, event_service=EventChannelService(), session_factory=factory,
    )
    await owner.start()
    registry = ResourceRegistry(
        publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture,
    )
    publisher = McpCatalogResourcePublisher(registry=registry)
    scope = LifetimeScope("catalog-runtime-test")
    scope.register(owner.subscribe_publication(publisher.publish), label="mcp-resource-publication")
    try:
        yield owner, factory, registry, scope
    finally:
        await scope.close()
        await owner.shutdown()


async def test_owner_schema_only_change_preserves_selected_guidance_bytes(catalog_runtime):
    owner, factory, registry, _scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert before.display_uri is None
    original_publication = owner.publication_snapshot()
    factory.session.minimum = 5
    factory.changed()
    assert not registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID).available
    await drain_pending_relists(owner)
    after = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    publication = owner.publication_snapshot()
    assert after.available
    assert canonical_json_bytes(after.payload) == canonical_json_bytes(before.payload)
    assert after.revision == before.revision == sha256_jcs(before.payload)
    assert len(canonical_json_bytes(after.payload)) == len(canonical_json_bytes(before.payload))
    assert after.members != before.members
    assert after.members[0].schema_hash != before.members[0].schema_hash
    assert publication.binding_ref.catalog_revision != original_publication.binding_ref.catalog_revision
    assert after.generation == before.generation + 1
    assert dict(after.source_lineage)[MCP_OWNER_CATALOG_SOURCE_ID] == publication.binding_ref.catalog_revision
    assert dict(after.source_lineage)[EXTENSION_CATALOG_SOURCE_ID] != dict(before.source_lineage)[EXTENSION_CATALOG_SOURCE_ID]
    assert set(after.payload) == {"content"}


async def test_owner_description_change_updates_actual_guidance(catalog_runtime):
    owner, factory, registry, _scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    factory.session.description = "新的真实指引正文"
    factory.changed()
    await drain_pending_relists(owner)
    after = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert after.payload != before.payload
    assert "新的真实指引正文" in after.payload["content"]
    assert after.revision != before.revision


async def test_relist_failure_blocks_registry_and_preserves_last_valid(catalog_runtime):
    owner, factory, registry, _scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    factory.session.error = RuntimeError("controlled tools/list failed")
    factory.changed()
    await drain_pending_relists(owner)
    unavailable = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert not unavailable.available
    assert unavailable.payload is None
    assert "controlled tools/list failed" in unavailable.error
    assert registry.last_valid_snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before
    assert unavailable.members == before.members
    assert unavailable.retained_revision == before.revision


async def test_subscription_is_released_by_runtime_scope(catalog_runtime):
    owner, factory, registry, scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    await scope.close()
    factory.session.description = "scope 关闭后的新指引"
    factory.changed()
    await drain_pending_relists(owner)
    assert owner.catalog_revision() != dict(before.source_lineage)[MCP_OWNER_CATALOG_SOURCE_ID]
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before


async def test_empty_catalog_keeps_explicit_lineage_and_custom_typed_members(catalog_workspace):
    owner = McpCatalogOwner(
        raw_config={"servers": {}}, workspace_root=catalog_workspace,
        event_service=EventChannelService(),
    )

    def custom(text: str) -> str:
        return text

    tool = StructuredTool.from_function(custom, name="local_echo", description="本地 custom target")
    await owner.start()
    try:
        registry = ResourceRegistry()
        publisher = McpCatalogResourcePublisher(registry=registry, custom_tools=(tool,))
        publisher.publish(owner.publication_snapshot())
        resource = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
        assert resource.payload == {"content": ""}
        assert len(resource.members) == 1
        assert resource.members[0].member_id == "local_echo"
        assert resource.members[0].member_kind == "custom"
        assert resource.members[0].server_id is None
        assert dict(resource.source_lineage)[MCP_OWNER_CATALOG_SOURCE_ID] == owner.catalog_revision()
        assert dict(resource.source_lineage)[EXTENSION_CATALOG_SOURCE_ID].startswith("sha256:")
        assert len(registry.capture().descriptors) == 1
    finally:
        await owner.shutdown()


@pytest.fixture
def guidance_prepare(catalog_runtime, registry_db, frozen_provider_activation, frozen_provider_wire):
    owner, _factory, registry, _scope = catalog_runtime
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, thread), control_state_port=saver,
        mutation_intent_port=saver,
    )
    envelope = create_extension_tool_invoker_tool(
        owner.get_tools(), catalog_binding_resolver=owner.binding_snapshot,
    )
    producer = RegisteredToolSetProducer(
        agent_id="default",
        selection_reader=ToolSelectionStore(boxteam_root=saver._storage.storage.sessions_dir.parent),
        policy_resolver=ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={}),
        tools=(envelope,), metadata={envelope.name: ToolMetadata(
            tool_id=envelope.name, origin="builtin", kind="default", group_id="extensions",
        )},
    )
    producer.freeze_structured_output_tools({})

    def prepare(
        key, *, context_owner=None, boundary: ResourceActivationBoundary = "model_call",
        checkpoint_ns: str = "",
    ):
        target = saver if context_owner is None else context_owner
        source_manager = manager if target is saver else ContextSourceManager(
            owner=ContextSourceOwnerKey(session, thread), control_state_port=target,
            mutation_intent_port=target,
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            activation = frozen_provider_activation(
                target, plan_creation_idempotency_key=key, registry=registry,
                checkpoint_ns=checkpoint_ns,
                policy=ResourceActivationPolicySnapshot(
                    default_boundary="turn", boundaries={"mcp_tool_catalog": boundary},
                ),
            )
            result = target.prepare_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                request_messages=(), context_source_manager=source_manager,
                tool_set_supplier=producer.snapshot,
                checkpoint_ns=checkpoint_ns,
                **frozen_provider_wire("fixture-model", "chat_completions"), **activation,
            )
        return result, activation["activation_snapshot"]

    return prepare


@pytest.mark.parametrize("boundary", ["turn", "model_call"])
async def test_empty_guidance_reappears_after_cold_successor_without_identity_drift(
    catalog_runtime, registry_db, guidance_prepare, boundary,
):
    owner, factory, _registry, _scope = catalog_runtime
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    prepare_options = {"boundary": boundary, "checkpoint_ns": "model:guidance-successor"}
    factory.session.empty = True
    factory.changed()
    await drain_pending_relists(owner)
    empty, initial = guidance_prepare("guidance-empty-initial", **prepare_options)
    initial_binding = next(binding for binding in initial.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    assert saver.read_resource_activation_payload(
        initial, initial_binding, checkpoint_ns=prepare_options["checkpoint_ns"],
        storage_owner=(session, thread),
    ) == {"content": ""}
    initial_snapshot = saver.get_context_assembly(session, thread, assembly_id=empty["assembly_id"])
    contribution_id = f"source-full:{MCP_GUIDANCE_SOURCE_ID}:{initial_binding.revision}"
    ordinal = connection.execute(
        "SELECT source_ordinal FROM context_contributions WHERE contribution_id=?", (contribution_id,),
    ).fetchone()[0]

    factory.session.empty = False
    factory.changed()
    await drain_pending_relists(owner)
    guidance_prepare("guidance-populated-before-restart", **prepare_options)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        cold, _activation = guidance_prepare(
            "guidance-populated-cold-successor", context_owner=restarted, **prepare_options,
        )
        cold_snapshot = restarted.get_context_assembly(session, thread, assembly_id=cold["assembly_id"])
        factory.session.empty = True
        factory.changed()
        await drain_pending_relists(owner)
        returned, activation = guidance_prepare(
            "guidance-empty-after-cold-successor", context_owner=restarted, **prepare_options,
        )
        binding = next(binding for binding in activation.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
        assert binding.revision == initial_binding.revision
        assert binding.members == ()
        assert restarted.read_resource_activation_payload(
            activation, binding, checkpoint_ns=prepare_options["checkpoint_ns"],
            storage_owner=(session, thread),
        ) == {"content": ""}
        returned_snapshot = restarted.get_context_assembly(session, thread, assembly_id=returned["assembly_id"])
        assert returned["tools"] == cold["tools"] == empty["tools"]
        assert returned_snapshot.seal_binding.prefix.state.epoch == initial_snapshot.seal_binding.prefix.state.epoch
        if boundary == "model_call":
            assert returned_snapshot.seal_binding.prefix.state.frames[:-1] == cold_snapshot.seal_binding.prefix.state.frames
            assert returned_snapshot.seal_binding.prefix.state.frames[-1].content == ""
        else:
            # 当前真实 admission 的 Turn 尚未改变，目录刷新不能改写冻结输入。
            assert activation == _activation == initial
            assert returned_snapshot.seal_binding.prefix.state.frames == cold_snapshot.seal_binding.prefix.state.frames == initial_snapshot.seal_binding.prefix.state.frames
            assert returned_snapshot.seal_binding.prefix.state.prefix_hash == initial_snapshot.seal_binding.prefix.state.prefix_hash
        assert restarted.get_context_assembly(session, thread, assembly_id=empty["assembly_id"]) == initial_snapshot
        assert connection.execute(
            "SELECT source_ordinal FROM context_contributions WHERE contribution_id=?", (contribution_id,),
        ).fetchall() == [(ordinal,)]


async def test_owner_freeze_seal_and_restart_use_exact_guidance_and_members(
    catalog_runtime, registry_db, guidance_prepare, monkeypatch,
):
    _owner, factory, registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    first, before = guidance_prepare("owner-guidance-first")
    parent = next(binding for binding in before.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    assert parent.display_uri is None
    original_body = saver.read_resource_activation_payload(
        before, parent, storage_owner=(session, thread),
    )
    original = saver.get_context_assembly(session, thread, assembly_id=first["assembly_id"])
    assert [tool["function"]["name"] for tool in first["tools"]] == ["invoke_extension_tool"]
    assert any(
        original_body["content"] in message.content
        for message in first["messages"]
        if isinstance(message.content, str)
    )
    guidance = tuple(
        frame for frame in original.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == MCP_GUIDANCE_SOURCE_ID
    )
    assert len(guidance) == 1
    assert guidance[0].role == "user"
    assert guidance[0].content == original_body["content"]

    factory.session.minimum = 5
    factory.changed()
    await drain_pending_relists(_owner)
    second, after = guidance_prepare("owner-guidance-schema-only")
    changed = next(binding for binding in after.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    assert parent.revision == changed.revision
    assert parent.content_hash == changed.content_hash
    assert before.bindings_hash == after.bindings_hash
    assert parent.members[0].schema_hash != changed.members[0].schema_hash
    assert saver.read_resource_activation_payload(
        after, changed, storage_owner=(session, thread),
    ) == original_body
    later = saver.get_context_assembly(session, thread, assembly_id=second["assembly_id"])
    assert second["tools"] == first["tools"]
    assert [(ref.source_revision, ref.content_hash, ref.tools) for ref in later.tool_set_refs] == [
        (ref.source_revision, ref.content_hash, ref.tools) for ref in original.tool_set_refs
    ]
    assert later.plan_hash == original.plan_hash
    assert later.request_hash == original.request_hash
    assert later.seal_binding.prefix.state.epoch == original.seal_binding.prefix.state.epoch
    assert later.seal_binding.prefix.state.epoch.reason.value != "toolset_changed"
    assert after.activation_provenance_hash != before.activation_provenance_hash
    assert second["messages"] == first["messages"]

    def forbid_live_capture():
        raise AssertionError("sealed retry 不得读取 live Registry")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "capture", forbid_live_capture)
        retry, restored_activation = guidance_prepare("owner-guidance-first")
    assert retry["messages"] == first["messages"]
    assert restored_activation == before
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        restored = restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=second["assembly_id"],
            plan_hash=later.plan_hash, request_hash=later.request_hash,
        )
        ref = next(binding for binding in restored.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
        assert ref.display_uri is None
        assert ref.members == changed.members
        assert restarted.read_resource_activation_payload(
            restored, ref, storage_owner=(session, thread),
        ) == original_body


@pytest.mark.parametrize("invalid", ["missing_receipt", "changed_content"])
async def test_guidance_seal_rejects_invalid_receipt_or_body_atomically(
    catalog_runtime, registry_db, guidance_prepare, monkeypatch, invalid,
):
    owner, factory, _registry, _scope = catalog_runtime
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    first, _activation = guidance_prepare("guidance-guard-first")
    original = saver.get_context_assembly(session, thread, assembly_id=first["assembly_id"])
    original_bytes = canonical_json_bytes(original.to_dict())
    counts = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                   for table in ("context_assemblies", "context_assembly_selections", "resource_activation_assembly_bindings"))
    if invalid == "missing_receipt":
        factory.session.description = "实际推进的 MCP 指引正文"
        factory.changed()
        await drain_pending_relists(owner)
    else:
        admission = saver.execution_admission(
            session, thread_id=thread, execution_id=accepted["initial_execution_id"],
        )
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "guidance-guard-checkpoint"
        checkpoint["channel_values"] = {"messages": []}
        checkpoint["channel_versions"] = {"messages": "1"}
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop"}, {"messages": "1"})
            saver.rewind(config, checkpoint_id=checkpoint["id"])

    compile_prefix = seal_preparation.compile_prefix_context

    def corrupt_preparation(*args, **kwargs):
        plan, preparation = compile_prefix(*args, **kwargs)
        if invalid == "missing_receipt":
            preparation = replace(preparation, source_revisions=tuple(
                source for source in preparation.source_revisions if source.source_id != MCP_GUIDANCE_SOURCE_ID
            ))
        else:
            assert preparation.transition is not None
            preparation = replace(preparation, prefix_state=replace(preparation.prefix_state, frames=tuple(
                replace(frame, content="同 revision 的伪造指引正文")
                if frame.item_ref.source_identity == MCP_GUIDANCE_SOURCE_ID else frame
                for frame in preparation.prefix_state.frames
            )))
        return plan, preparation

    monkeypatch.setattr(seal_preparation, "compile_prefix_context", corrupt_preparation)
    if invalid == "missing_receipt":
        with pytest.raises(ItemSchemaError, match="guidance applied revision"):
            guidance_prepare("guidance-guard-invalid:" + invalid)
    else:
        with pytest.raises(ValueError, match="source-mismatch: source wire frame 与 sealed 正文不一致"):
            guidance_prepare("guidance-guard-invalid:" + invalid)
    assert tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("context_assemblies", "context_assembly_selections", "resource_activation_assembly_bindings")) == counts
    assert canonical_json_bytes(saver.get_context_assembly(session, thread, assembly_id=original.assembly_id).to_dict()) == original_bytes
    assert connection.execute("SELECT COUNT(*) FROM context_source_control_states WHERE source_id=?",
                              (MCP_GUIDANCE_SOURCE_ID,)).fetchone() == (0,)
