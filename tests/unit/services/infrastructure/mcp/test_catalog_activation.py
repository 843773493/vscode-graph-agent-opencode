"""唯一 MCP Owner 发布的 guidance/member 双通道；不建立独立 activation writer。"""

from __future__ import annotations

import errno
import json
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
from app.domain.itemized.epoch.source_protocol import (
    parse_source_blocks,
    render_source_blocks,
)
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
    McpCatalogRelistError,
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
from tests.support.mcp.session_doubles import drain_pending_relists


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
def catalog_selection(catalog_workspace):
    return ToolSelectionStore(boxteam_root=catalog_workspace / ".boxteam")


@pytest.fixture
async def catalog_runtime(catalog_workspace, catalog_selection):
    factory = CatalogSessionFactory()
    owner = McpCatalogOwner(
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "controlled-server"}}},
        workspace_root=catalog_workspace, event_service=EventChannelService(), session_factory=factory,
    )
    await owner.start()
    registry = ResourceRegistry(
        publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture,
    )
    resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={})

    def policies(metadata):
        selection = catalog_selection.snapshot("default")
        return {entry.tool_id: resolver.resolve(
            entry, execution_override=selection.execution_overrides.get(entry.tool_id),
            model_visibility_override=selection.model_visibility_overrides.get(entry.tool_id),
        ) for entry in metadata}

    publisher = McpCatalogResourcePublisher(registry=registry, policy_resolver=policies)
    scope = LifetimeScope("catalog-runtime-test")
    scope.register(owner.subscribe_publication(publisher.publish), label="mcp-resource-publication")

    def republish():
        with owner.publication_lock:
            publisher.publish(owner.publication_snapshot())

    scope.register(catalog_selection.subscribe("default", republish), label="selection-publication")
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
    assert "RuntimeError" in unavailable.error
    assert "controlled tools/list failed" not in unavailable.error
    assert registry.last_valid_snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before
    assert unavailable.members == before.members
    assert unavailable.retained_revision == before.revision


@pytest.mark.parametrize("native_path", [
    "/private/mcp/catalog.json",
    r"C:\private\mcp\catalog.json",
    r"\\server\private\mcp\catalog.json",
    r"\\?\UNC\server\private\mcp\catalog.json",
])
@pytest.mark.parametrize("source", ["explicit", "notification"])
async def test_catalog_failure_publishes_facts_and_keeps_raw_diagnostics(
    catalog_runtime, native_path: str, source: str, caplog: pytest.LogCaptureFixture,
):
    owner, factory, registry, _scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    binding = owner.binding_snapshot()
    failure = PermissionError(errno.EACCES, "private server diagnostic", native_path)
    failure.winerror = 5
    factory.session.error = failure
    if source == "explicit":
        factory.session.supports_tool_list_changed = lambda: False
        with pytest.raises(McpCatalogRelistError) as raised:
            await owner.relist_servers_without_notifications()
        assert raised.value.__cause__ is failure
    else:
        factory.changed()
        await drain_pending_relists(owner)

    unavailable = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    public_error = unavailable.error
    assert public_error == owner.list_servers()[0].last_relist_error
    assert public_error == owner.publication_snapshot().error
    if source == "explicit":
        assert public_error == str(raised.value)
    assert "server_id=mini" in public_error
    assert "relist" in public_error
    assert "PermissionError" in public_error
    assert "errno=13" in public_error
    assert "winerror=5" in public_error
    assert native_path not in public_error
    assert "private server diagnostic" not in public_error
    assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
    assert str(failure) in caplog.text
    assert not unavailable.available
    assert unavailable.payload is None
    assert registry.last_valid_snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before
    assert unavailable.members == before.members
    assert unavailable.retained_revision == before.revision
    assert owner.binding_snapshot() == binding

    factory.session.error = None
    if source == "explicit":
        await owner.relist_servers_without_notifications()
    else:
        factory.changed()
        await drain_pending_relists(owner)
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID).available
    assert owner.list_servers()[0].last_relist_error is None
    assert owner.publication_snapshot().error is None
    assert owner.binding_snapshot() == binding


async def test_subscription_is_released_by_runtime_scope(catalog_runtime):
    owner, factory, registry, scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    await scope.close()
    factory.session.description = "scope 关闭后的新指引"
    factory.changed()
    await drain_pending_relists(owner)
    assert owner.catalog_revision() != dict(before.source_lineage)[MCP_OWNER_CATALOG_SOURCE_ID]
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before


async def test_empty_catalog_keeps_explicit_lineage_and_custom_typed_members(catalog_workspace, extension_catalog_policy_resolver):
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
        publisher = McpCatalogResourcePublisher(registry=registry, custom_tools=(tool,), policy_resolver=extension_catalog_policy_resolver)
        publisher.publish(owner.publication_snapshot())
        resource = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
        guidance = json.loads(resource.payload["content"])
        assert guidance["automatic_definitions"] == []
        assert guidance["navigation"][0]["tool_count"] == 1
        assert len(resource.members) == 1
        assert resource.members[0].member_id == "local_echo"
        assert resource.members[0].member_kind == "custom"
        assert resource.members[0].server_id is None
        assert dict(resource.source_lineage)[MCP_OWNER_CATALOG_SOURCE_ID] == owner.catalog_revision()
        assert dict(resource.source_lineage)[EXTENSION_CATALOG_SOURCE_ID].startswith("sha256:")
        assert {descriptor.facet for descriptor in registry.capture().descriptors} == {"activation", "definition"}
        definition, = (snapshot for snapshot in registry.capture().snapshots if snapshot.facet == "definition")
        assert definition.members == resource.members
        assert definition.payload["input_schema"]["properties"]["text"]["type"] == "string"
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
    initial_body = saver.read_resource_activation_payload(
        initial, initial_binding, checkpoint_ns=prepare_options["checkpoint_ns"],
        storage_owner=(session, thread),
    )
    assert json.loads(initial_body["content"])["navigation"] == []
    assert json.loads(initial_body["content"])["automatic_definitions"] == []
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
        ) == initial_body
        returned_snapshot = restarted.get_context_assembly(session, thread, assembly_id=returned["assembly_id"])
        assert returned["tools"] == cold["tools"] == empty["tools"]
        assert returned_snapshot.seal_binding.prefix.state.epoch == initial_snapshot.seal_binding.prefix.state.epoch
        if boundary == "model_call":
            assert returned_snapshot.seal_binding.prefix.state.frames[:-1] == cold_snapshot.seal_binding.prefix.state.frames
            block, = parse_source_blocks(returned_snapshot.seal_binding.prefix.state.frames[-1].content)
            assert (block.source_identity, block.revision, block.body, block.operation) == (
                MCP_GUIDANCE_SOURCE_ID, binding.revision, initial_body["content"], "update",
            )
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
    guidance = tuple(
        frame for frame in original.seal_binding.prefix.state.frames
        if frame.item_ref.source_identity == MCP_GUIDANCE_SOURCE_ID
    )
    assert len(guidance) == 1
    assert guidance[0].role == "user"
    block, = parse_source_blocks(guidance[0].content)
    assert (block.source_identity, block.revision, block.body, block.source_kind, block.root_placement) == (
        MCP_GUIDANCE_SOURCE_ID, parent.revision, original_body["content"], "mcp_tool_catalog", "tail_only",
    )
    assert any(message.content == guidance[0].content for message in first["messages"])

    factory.session.minimum = 5
    factory.changed()
    await drain_pending_relists(_owner)
    second, after = guidance_prepare("owner-guidance-schema-only")
    changed = next(binding for binding in after.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    assert parent.revision == changed.revision
    assert parent.content_hash == changed.content_hash
    before_definition, = (binding for binding in before.bindings if binding.facet == "definition")
    after_definition, = (binding for binding in after.bindings if binding.facet == "definition")
    assert before_definition.revision != after_definition.revision
    assert before.bindings_hash != after.bindings_hash
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
                replace(frame, content=render_source_blocks(tuple(
                    replace(block, body="同 revision 的伪造指引正文") for block in parse_source_blocks(frame.content)
                )))
                if frame.item_ref.source_identity == MCP_GUIDANCE_SOURCE_ID else frame
                for frame in preparation.prefix_state.frames
            )))
        return plan, preparation

    monkeypatch.setattr(seal_preparation, "compile_prefix_context", corrupt_preparation)
    if invalid == "missing_receipt":
        with pytest.raises(ItemSchemaError, match="guidance applied revision"):
            guidance_prepare("guidance-guard-invalid:" + invalid)
    else:
        with pytest.raises(ValueError, match="source-mismatch: source 来源块与 sealed 正文或绑定不一致"):
            guidance_prepare("guidance-guard-invalid:" + invalid)
    assert tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("context_assemblies", "context_assembly_selections", "resource_activation_assembly_bindings")) == counts
    assert canonical_json_bytes(saver.get_context_assembly(session, thread, assembly_id=original.assembly_id).to_dict()) == original_bytes
    assert connection.execute("SELECT COUNT(*) FROM context_source_control_states WHERE source_id=?",
                              (MCP_GUIDANCE_SOURCE_ID,)).fetchone() == (0,)


@pytest.mark.parametrize("boundary", ["model_call", "turn"])
async def test_selection_publication_injects_full_definition_at_allowed_boundary(
    catalog_runtime, catalog_selection, registry_db, guidance_prepare, boundary,
):
    _owner, factory, registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    options = {"boundary": boundary, "checkpoint_ns": f"model:visibility-{boundary}"}
    first, frozen = guidance_prepare("visibility-initial", **options)
    parent, = (binding for binding in frozen.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    original = saver.read_resource_activation_payload(frozen, parent, checkpoint_ns=options["checkpoint_ns"], storage_owner=(session, thread))
    assert json.loads(original["content"])["automatic_definitions"] == []
    list_calls = factory.session.list_calls
    catalog_selection.apply_changes(agent_id="default", changes={"mcp__mini__echo": (True, True)})
    published = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    definition, = json.loads(published.payload["content"])["automatic_definitions"]
    assert definition["tool_name"] == "mcp__mini__echo"
    assert definition["input_schema"]["properties"]["text"]["minLength"] == 1
    assert definition["input_schema"]["required"] == ["text"]
    assert factory.session.list_calls == list_calls
    second, activation = guidance_prepare("visibility-enabled", **options)
    new_parent, = (binding for binding in activation.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    selected = saver.read_resource_activation_payload(activation, new_parent, checkpoint_ns=options["checkpoint_ns"], storage_owner=(session, thread))
    assert selected == (published.payload if boundary == "model_call" else original)
    snapshot = saver.get_context_assembly(session, thread, assembly_id=second["assembly_id"])
    prior = saver.get_context_assembly(session, thread, assembly_id=first["assembly_id"])
    assert second["tools"] == first["tools"]
    assert snapshot.seal_binding.prefix.state.epoch == prior.seal_binding.prefix.state.epoch
    assert (snapshot.request_hash != prior.request_hash) == (boundary == "model_call")
    third, _ = guidance_prepare("visibility-stable", **options)
    assert third["messages"] == second["messages"], "同一完整定义不能在稳定后继重复投递"
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        retry, _ = guidance_prepare("visibility-enabled", context_owner=restarted, **options)
        assert retry["messages"] == second["messages"]
    assert saver.read_resource_activation_payload(frozen, parent, checkpoint_ns=options["checkpoint_ns"], storage_owner=(session, thread)) == original


async def test_disabling_and_reenabling_selection_republishes_members_and_definition(
    catalog_runtime, catalog_selection,
):
    _owner, _factory, registry, scope = catalog_runtime
    before = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    catalog_selection.apply_changes(agent_id="default", changes={"mcp__mini__echo": (False, False)})
    disabled = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert disabled.members == ()
    assert json.loads(disabled.payload["content"])["navigation"] == []
    catalog_selection.apply_changes(agent_id="default", changes={"mcp__mini__echo": (True, False)})
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before
    await scope.close()
    catalog_selection.apply_changes(agent_id="default", changes={"mcp__mini__echo": (True, True)})
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID) == before, "runtime 释放后不得保留选择订阅"
