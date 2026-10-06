from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import main
from app.container import AppContainer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import (
    get_session_creation_service,
    get_session_path_resolver,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    user_turn_admission_payload_hash,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugConfigurationBreakpointDTO,
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionDTO,
    SessionGenerationOriginDTO,
)
from app.services.business.job.service import JobService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugTargetValidationError,
    build_workspace_fork_config,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.full_copy import (
    operation as full_copy_operation,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
    _isolate_non_navigation_services,
)
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)


@dataclass(frozen=True, slots=True)
class ForkIntegrationContext:
    workspace: Path
    sessions: SessionService
    saver: RolloutCheckpointSaver
    forks: SessionContextForkService


@pytest.fixture
def fork_context(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> ForkIntegrationContext:
    workspace = prepare_rollout_workspace(request)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    sessions_dir = workspace / ".boxteam" / "sessions"
    config_service = ConfigService(workspace_root=workspace)
    monkeypatch.setattr(
        config_service,
        "resolve_new_session_provider_id",
        lambda _agent_id: "test-provider",
    )
    saver = RolloutCheckpointSaver(
        sessions_dir,
        node_debug_workspace_config=lambda: build_workspace_fork_config(
            workspace, config_service.get_debug_runtime_config()
        ),
    )
    sessions = SessionService(
        config_service=config_service,
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        workspace_id=load_or_create_workspace_id(workspace),
        path_resolver=get_session_path_resolver(sessions_dir),
        creation_service=get_session_creation_service(sessions_dir),
        fork_relationship_checker=saver,
    )
    return ForkIntegrationContext(
        workspace=workspace,
        sessions=sessions,
        saver=saver,
        forks=SessionContextForkService(session_service=sessions, checkpointer=saver),
    )


@pytest.fixture
def tool_set() -> ToolSetSnapshot:
    return ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@pytest.fixture
def production_fork_container(request: pytest.FixtureRequest,
                              monkeypatch: pytest.MonkeyPatch) -> AppContainer:
    """fork/creation/admission 使用真实生产装配，只隔离无关 lifespan 外部服务。"""
    workspace = prepare_rollout_workspace(request)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    container = _build_owned_container(workspace, monkeypatch, request)
    _isolate_non_navigation_services(container, monkeypatch)
    # 外设进程不参与 fork；仅替换终端列表 transport，真实 delete/drain/catalog/fence 保留。
    terminal = container.session_resource_service._provider_registry.get("terminal")
    monkeypatch.setattr(terminal._owner._transport, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "configure_application_logging", lambda **_: None)
    monkeypatch.setattr(main, "_model_stream_controller", None)
    return container


def _production_fork_context(container: AppContainer) -> ForkIntegrationContext:
    return ForkIntegrationContext(
        workspace=container.workspace_root,
        sessions=container.session_service,
        saver=container.checkpointer,
        forks=container.session_context_fork_service,
    )


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
    *,
    pending_sends: list[object] | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-08-15T00:00:00+00:00",
        "channel_values": {"messages": messages, "counter": len(messages)},
        "channel_versions": {
            "messages": str(len(messages)),
            "counter": str(len(messages)),
        },
        "versions_seen": {},
        "pending_sends": pending_sends or [],
        "updated_channels": ["messages", "counter"],
    }


async def _seed_source(context: ForkIntegrationContext):
    source = await context.sessions.create(
        SessionCreateRequest(title="Fork 集成源会话")
    )
    thread_id = context.saver._storage.main_thread_id(source.session_id)
    owner = MutationIntentOwner(
        session_id=source.session_id,
        thread_id=thread_id,
    )
    first_accepted = context.saver.accept_turn(
        source.session_id,
        thread_id=thread_id,
        accepted_ingress_id="u1",
        acceptance_idempotency_key="acceptance-turn-1",
        job_id="job-turn-1",
        admission_idempotency_key="admission-turn-1",
        payload="问题一",
        turn_id="turn-1",
        root_item_id="item-u1",
        initial_execution_id="execution-turn-1",
    )
    first_admission = context.saver.execution_admission(
        source.session_id,
        thread_id=thread_id,
        execution_id=str(first_accepted["initial_execution_id"]),
    )
    first_messages = [
        HumanMessage(
            content="问题一",
            id="u1",
            response_metadata={"turn_id": "turn-1"},
        )
    ]
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            first_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        first_config = await context.saver.aput(
            build_checkpoint_config(thread_id),
            _checkpoint("cp1", first_messages),
            {"source": "stub", "step": 1, "parents": {}},
            {"messages": "1", "counter": "1"},
        )
    second_messages = [*first_messages, AIMessage(content="回答一", id="a1")]
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            first_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        second_config = await context.saver.aput(
            first_config,
            _checkpoint(
                "cp2",
                second_messages,
                pending_sends=[{"node": "pending-node", "attempt": 2}],
            ),
            {"source": "stub", "step": 2, "parents": {}},
            {"messages": "2", "counter": "2"},
        )
    context.saver.finalize_turn(
        session_id=source.session_id,
        thread_id=thread_id,
        turn_id="turn-1",
        final_message_id="a1",
    )
    second_accepted = context.saver.accept_turn(
        source.session_id,
        thread_id=thread_id,
        accepted_ingress_id="u2",
        acceptance_idempotency_key="acceptance-turn-2",
        job_id="job-turn-2",
        admission_idempotency_key="admission-turn-2",
        payload="问题二",
        turn_id="turn-2",
        root_item_id="item-u2",
        initial_execution_id="execution-turn-2",
    )
    second_admission = context.saver.execution_admission(
        source.session_id,
        thread_id=thread_id,
        execution_id=str(second_accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            second_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        await context.saver.aput_writes(
            second_config,
            [("scratchpad", {"pending": True})],
            "task-pending",
            "parent/child",
        )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            second_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        await context.saver.aput(
            second_config,
            _checkpoint(
                "cp3",
                [
                    *second_messages,
                    HumanMessage(
                        content="问题二",
                        id="u2",
                        response_metadata={"turn_id": "turn-2"},
                    ),
                ],
            ),
            {"source": "stub", "step": 3, "parents": {}},
            {"messages": "3", "counter": "3"},
        )
    return source


def _seed_debug_configurations(
    context: ForkIntegrationContext, source_session_id: str
) -> tuple[str, str]:
    script = context.workspace / "app.mjs"
    script.write_text("const value = 1;\nconsole.log(value);\n", encoding="utf-8")
    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    source_thread_id = context.sessions.path_resolver.main_thread_id(source_session_id)
    active_id = "dbgcfg_11111111111111111111111111111111"
    saved_id = "dbgcfg_22222222222222222222222222222222"
    for configuration_id, name in (
        (active_id, "活动方案"),
        (saved_id, "保存方案"),
    ):
        context.saver._node_debug_store.write_configuration(
            source_session_id,
            NodeDebugConfigurationDTO(
                configuration_id=configuration_id,
                name=name,
                script_path="app.mjs",
                working_directory="",
                launch_profile_name="node-default",
                breakpoints=[
                    NodeDebugConfigurationBreakpointDTO(
                        breakpoint_id=f"bp_{configuration_id[-4:]}",
                        path="app.mjs",
                        line=1,
                        created_at=created_at,
                    )
                ],
                created_at=created_at,
                updated_at=created_at,
            ),
            source_thread_id,
        )
    context.saver._node_debug_store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=source_session_id,
            thread_id=source_thread_id,
            active_configuration_id=active_id,
            actions=[
                NodeDebugActionRecordDTO(
                    action_id="act_source_only",
                    session_id=source_session_id,
                    thread_id=source_thread_id,
                    action="continue",
                    message="source runtime only",
                    result="success",
                    created_at=created_at,
                )
            ],
            updated_at=created_at,
        )
    )
    context.saver._node_debug_store.write_launch_claim(
        NodeDebugLaunchClaimDTO(
            session_id=source_session_id,
            thread_id=source_thread_id,
            process_instance_id="node-debug-proc_source",
            nonce="source-nonce",
            phase="running",
            configuration_id=active_id,
            pid=4242,
            process_identity_source="test",
            process_start_marker="source-marker",
            inspector_host="127.0.0.1",
            inspector_port=9311,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    return active_id, saved_id


def _main_thread_id(context: ForkIntegrationContext, session_id: str) -> str:
    return context.sessions.path_resolver.main_thread_id(session_id)


async def _get_tuple(
    context: ForkIntegrationContext,
    session_id: str,
    *,
    saver: RolloutCheckpointSaver | None = None,
    checkpoint_id: str | None = None,
):
    checkpoint_saver = saver or context.saver
    owner = checkpoint_saver.resolve_main_thread_owner(session_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        return await checkpoint_saver.aget_tuple(
            build_checkpoint_config(
                owner.thread_id,
                checkpoint_id=checkpoint_id,
            )
        )


def _fork_origins(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT child_session_id, source_session_id, source_checkpoint_id, "
            "source_view_id, fork_mode, relationship FROM fork_origins"
        ).fetchall()


def _retention_refs(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT reference_kind, reference_id, target_view_id, "
            "owner_session_id, status FROM retention_refs"
        ).fetchall()


def _pending_write_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT task_id, task_path, write_index, channel, status "
            "FROM pending_writes ORDER BY task_path, write_index"
        ).fetchall()


def _fork_materialization_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT materialization_id, fork_id, status, copied_message_count "
            "FROM fork_materializations"
        ).fetchall()


def _identity_mapping_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT fork_id, source_session_id, target_session_id, entity_type, "
            "source_local_id, target_local_id, source_offset, target_offset, lineage_json "
            "FROM fork_identity_mappings ORDER BY entity_type, source_local_id"
        ).fetchall()


def _main_thread_node(context: ForkIntegrationContext, session_id: str) -> Path:
    thread_id = context.saver._storage.main_thread_id(session_id)
    return context.sessions.path_resolver.resolve_thread_node(session_id, thread_id)


@pytest.mark.asyncio
async def test_public_fork_modes_copy_only_portable_debug_configurations(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    active_id, saved_id = _seed_debug_configurations(
        fork_context, source.session_id
    )

    context_child = await fork_context.forks.fork(
        source.session_id,
        mode="context_fork",
        checkpoint_id="cp2",
        anchor="u1",
    )
    prefix_child = await fork_context.forks.fork(
        source.session_id,
        mode="history_prefix_fork",
        checkpoint_id="cp2",
    )
    full_child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
        checkpoint_id="cp2",
    )

    store = fork_context.saver._node_debug_store
    context_thread_id = _main_thread_id(fork_context, context_child.session_id)
    prefix_thread_id = _main_thread_id(fork_context, prefix_child.session_id)
    full_thread_id = _main_thread_id(fork_context, full_child.session_id)
    source_thread_id = _main_thread_id(fork_context, source.session_id)
    context_manifest = store.read_manifest(
        context_child.session_id, context_thread_id
    )
    assert context_manifest is not None
    assert context_manifest.active_configuration_id is None
    assert context_manifest.actions == []
    context_configurations = store.list_configurations(
        context_child.session_id, context_thread_id
    )
    assert [item.name for item in context_configurations] == ["活动方案"]
    assert context_configurations[0].configuration_id != active_id

    assert store.read_manifest(prefix_child.session_id, prefix_thread_id) is None
    assert store.list_configurations(prefix_child.session_id, prefix_thread_id) == []

    full_manifest = store.read_manifest(full_child.session_id, full_thread_id)
    assert full_manifest is not None
    assert full_manifest.active_configuration_id is None
    assert full_manifest.actions == []
    full_configurations = store.list_configurations(
        full_child.session_id, full_thread_id
    )
    assert {item.name for item in full_configurations} == {"活动方案", "保存方案"}
    assert {item.configuration_id for item in full_configurations}.isdisjoint(
        {active_id, saved_id}
    )

    for child, expected_count in (
        (context_child, 1),
        (full_child, 2),
    ):
        rollout_root = _main_thread_node(fork_context, child.session_id)
        with sqlite3.connect(
            rollout_root / "rollout" / "index.sqlite"
        ) as connection:
            row = connection.execute(
                "SELECT lineage_json FROM fork_identity_mappings "
                "WHERE entity_type = 'debug_snapshot'"
            ).fetchone()
            mapping_rows = connection.execute(
                "SELECT source_local_id, target_local_id, lineage_json "
                "FROM fork_identity_mappings "
                "WHERE entity_type = 'debug_configuration'"
            ).fetchall()
        assert row is not None
        lineage = json.loads(row[0])
        assert lineage["state"] == "published"
        assert len(lineage["source_configurations"]) == expected_count
        assert len(lineage["target_configuration_id_map"]) == expected_count
        assert len(mapping_rows) == expected_count
        assert all(
            json.loads(mapping[2])["source_revision"] == 1
            for mapping in mapping_rows
        )
        assert store.read_launch_claim(
            child.session_id, _main_thread_id(fork_context, child.session_id)
        ) is None
    source_claim = store.read_launch_claim(source.session_id, source_thread_id)
    assert source_claim is not None
    assert (source_claim.pid, source_claim.inspector_port, source_claim.phase) == (
        4242,
        9311,
        "running",
    )


@pytest.mark.asyncio
async def test_debug_config_drift_aborts_entire_public_fork(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    _seed_debug_configurations(fork_context, source.session_id)
    provider = fork_context.saver._node_debug_workspace_config
    assert provider is not None
    stable = provider()
    drifted = replace(
        stable,
        revision="f" * 64,
        content_hash=f"sha256:{'f' * 64}",
    )
    calls = 0

    def drifting_provider():
        nonlocal calls
        calls += 1
        return stable if calls <= 2 else drifted

    fork_context.saver._node_debug_workspace_config = drifting_provider
    before = {item.session_id for item in (await fork_context.sessions.list()).items}

    with pytest.raises(NodeDebugTargetValidationError, match="配置 revision 已漂移"):
        await fork_context.forks.fork(
            source.session_id,
            mode="context_fork",
            checkpoint_id="cp2",
            anchor="u1",
        )

    after = {item.session_id for item in (await fork_context.sessions.list()).items}
    assert after == before
    source_node = fork_context.sessions.path_resolver.resolve_session_node(
        source.session_id
    )
    assert not (source_node / ".fork-debug-staging").exists()


@pytest.mark.asyncio
async def test_restart_recovers_debug_published_before_materialization_commit(
    fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context)
    _seed_debug_configurations(fork_context, source.session_id)
    target = await fork_context.sessions.create(
        SessionCreateRequest(title="debug fork crash target")
    )
    target_thread_id = _main_thread_id(fork_context, target.session_id)
    storage = fork_context.saver._storage
    original_commit = storage.commit_fork_materialization

    def crash_before_materialization_commit(*_args, **_kwargs):
        raise RuntimeError("injected crash before materialization commit")

    monkeypatch.setattr(
        storage,
        "commit_fork_materialization",
        crash_before_materialization_commit,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        await fork_context.saver.afork(
            source_session_id=source.session_id,
            target_session_id=target.session_id,
            mode="context_fork",
            checkpoint_id="cp2",
            anchor="u1",
        )
    assert (
        fork_context.saver._node_debug_store.read_manifest(
            target.session_id, target_thread_id
        )
        is not None
    )

    monkeypatch.setattr(storage, "commit_fork_materialization", original_commit)
    provider = fork_context.saver._node_debug_workspace_config
    with RolloutCheckpointSaver(
        storage.sessions_dir,
        node_debug_workspace_config=provider,
    ) as restarted:
        restarted._storage.initialize(target.session_id)
        assert (
            restarted._node_debug_store.read_manifest(
                target.session_id, target_thread_id
            )
            is None
        )
        with restarted._storage._connect(
            target_thread_id, "", session_id=target.session_id, read_only=True
        ) as connection:
            assert connection.execute(
                "SELECT status FROM fork_materializations"
            ).fetchall() == [("aborted",)]
            assert connection.execute(
                "SELECT COUNT(*) FROM fork_identity_mappings "
                "WHERE entity_type IN ('debug_snapshot', 'debug_configuration')"
            ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_full_copy_restart_publishes_ready_debug_after_rollout_install(
    fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context)
    _seed_debug_configurations(fork_context, source.session_id)
    target = await fork_context.sessions.create(
        SessionCreateRequest(title="full copy debug crash target")
    )
    target_thread_id = _main_thread_id(fork_context, target.session_id)

    original_publish = full_copy_operation.publish_ready_node_debug_fork

    def crash_after_rollout_install(*_args, **_kwargs):
        raise RuntimeError("injected crash after rollout install")

    monkeypatch.setattr(
        full_copy_operation,
        "publish_ready_node_debug_fork",
        crash_after_rollout_install,
    )
    with pytest.raises(RuntimeError, match="injected crash after rollout install"):
        await fork_context.saver.afork(
            source_session_id=source.session_id,
            target_session_id=target.session_id,
            mode="full_rollout_copy",
            checkpoint_id="cp2",
        )

    target_node = fork_context.sessions.path_resolver.resolve_session_node(
        target.session_id
    )
    target_thread_node = _main_thread_node(fork_context, target.session_id)
    assert not (target_node / "debug" / "node").exists()
    with sqlite3.connect(target_thread_node / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT status FROM fork_materializations"
        ).fetchall() == [("target_committed",)]
        lineage = json.loads(
            connection.execute(
                "SELECT lineage_json FROM fork_identity_mappings "
                "WHERE entity_type = 'debug_snapshot'"
            ).fetchone()[0]
        )
    assert lineage["state"] == "ready"
    staging = target_node / lineage["target_debug_staging_path"]
    assert staging.is_dir()

    monkeypatch.setattr(
        full_copy_operation,
        "publish_ready_node_debug_fork",
        original_publish,
    )
    provider = fork_context.saver._node_debug_workspace_config
    with RolloutCheckpointSaver(
        fork_context.saver._storage.sessions_dir,
        node_debug_workspace_config=provider,
    ) as restarted:
        restarted._storage.initialize(target.session_id)
        manifest = restarted._node_debug_store.read_manifest(
            target.session_id, target_thread_id
        )
        assert manifest is not None
        assert manifest.session_id == target.session_id
        assert manifest.thread_id == target_thread_id
        assert manifest.active_configuration_id is None
        assert manifest.actions == []
        assert not staging.exists()
        with restarted._storage._connect(
            target_thread_id, "", session_id=target.session_id, read_only=True
        ) as connection:
            assert connection.execute(
                "SELECT status FROM fork_materializations"
            ).fetchall() == [("committed",)]
            recovered_lineage = json.loads(
                connection.execute(
                    "SELECT lineage_json FROM fork_identity_mappings "
                    "WHERE entity_type = 'debug_snapshot'"
                ).fetchone()[0]
            )
        assert recovered_lineage["state"] == "published"


@pytest.mark.asyncio
async def test_all_fork_modes_materialize_independent_rollouts(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    context_child = await fork_context.forks.fork(
        source.session_id,
        mode="context_fork",
        checkpoint_id="cp2",
        anchor="u1",
    )
    prefix_child = await fork_context.forks.fork(
        source.session_id,
        mode="history_prefix_fork",
        checkpoint_id="cp2",
    )
    # 模拟源 rollout 自身已经是一个 fork，验证完整复制不会继承这条旧关系。
    fork_context.saver.record_fork_origin(
        target_session_id=source.session_id,
        source_session_id="ses_old_source",
        source_checkpoint_id="old-checkpoint",
        source_view_id="old-view",
        fork_mode="context_fork",
    )
    full_child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
        checkpoint_id="cp2",
    )

    context_tuple = await _get_tuple(fork_context, context_child.session_id)
    prefix_tuple = await _get_tuple(fork_context, prefix_child.session_id)
    full_tuple = await _get_tuple(fork_context, full_child.session_id)
    assert context_tuple is not None
    assert prefix_tuple is not None
    assert full_tuple is not None
    origins = {
        child.session_id: _fork_origins(fork_context, child.session_id)
        for child in (context_child, prefix_child, full_child)
    }
    assert all(len(rows) == 1 for rows in origins.values())
    assert origins[context_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[context_child.session_id][0][3],
        "context_fork",
        "detached",
    )
    assert origins[prefix_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[prefix_child.session_id][0][3],
        "history_prefix_fork",
        "detached",
    )
    assert origins[full_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[full_child.session_id][0][3],
        "full_rollout_copy",
        "detached",
    )
    assert all(rows[0][3] is not None for rows in origins.values())
    assert [
        message.content
        for message in context_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]
    assert [
        message.content
        for message in prefix_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]
    assert [
        message.content
        for message in full_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
        "问题二",
    ]
    assert context_tuple.checkpoint["pending_sends"] == [
        {"node": "pending-node", "attempt": 2}
    ]
    assert context_tuple.pending_writes == [
        ("task-pending", "scratchpad", {"pending": True})
    ]
    assert _pending_write_rows(fork_context, context_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    prefix_owner = fork_context.saver.resolve_main_thread_owner(prefix_child.session_id)
    with session_thread_owner_scope(prefix_owner, langgraph_thread_id=prefix_owner.thread_id):
        prefix_pending = [item async for item in fork_context.saver.alist(
            build_checkpoint_config(prefix_owner.thread_id)
        ) if item.checkpoint["id"] != ""]
    assert any(
        item.checkpoint["pending_sends"] == [{"node": "pending-node", "attempt": 2}]
        and item.pending_writes == [("task-pending", "scratchpad", {"pending": True})]
        for item in prefix_pending
    )
    assert _pending_write_rows(fork_context, prefix_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    full_pending = await _get_tuple(
        fork_context,
        full_child.session_id,
        checkpoint_id="cp2",
    )
    assert full_pending is not None
    assert full_pending.checkpoint["pending_sends"] == [
        {"node": "pending-node", "attempt": 2}
    ]
    assert full_pending.pending_writes == [
        ("task-pending", "scratchpad", {"pending": True})
    ]
    assert _pending_write_rows(fork_context, full_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    for child in (context_child, prefix_child, full_child):
        assert _fork_materialization_rows(fork_context, child.session_id)[0][2] == (
            "committed"
        )
        child_root = _main_thread_node(fork_context, child.session_id)
        with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
            turn_rows = connection.execute(
                "SELECT turn_id, status, final_item_id FROM turn_records ORDER BY turn_ordinal"
            ).fetchall()
            assert turn_rows, "fork target 缺少 canonical TurnRecord"
            assert all(row[0] != "turn-1" for row in turn_rows), (
                child.session_id,
                turn_rows,
            )
            assert any(row[1] == "completed" and row[2] for row in turn_rows)
            assert all(
                row[1] in {"completed", "unknown", "cancelled"} for row in turn_rows
            )
    identity_rows = _identity_mapping_rows(fork_context, full_child.session_id)
    assert identity_rows
    required_entity_types = {
        "item",
        "turn",
        "execution",
        "view",
        "branch",
        "checkpoint",
        "accepted_ingress",
        "acceptance_idempotency_key",
    }
    assert required_entity_types <= {str(row[3]) for row in identity_rows}
    for row in identity_rows:
        assert row[1] == source.session_id
        assert row[2] == full_child.session_id
        assert row[4]
        assert row[5]
    item_mappings = [row for row in identity_rows if row[3] == "item"]
    assert item_mappings
    assert all(row[4] != row[5] for row in item_mappings)
    assert all(row[6] is not None and row[7] is not None for row in item_mappings)
    turn_lineages = [row for row in identity_rows if row[3] == "turn"]
    assert turn_lineages
    for row in turn_lineages:
        lineage = json.loads(str(row[8]))
        assert lineage["source"]["ordinal"] == lineage["target"]["ordinal"]
        assert lineage["target"]["ordinal"] is not None
    acceptance_mappings = {
        str(row[3]): row
        for row in identity_rows
        if row[3] in {"accepted_ingress", "acceptance_idempotency_key"}
    }
    assert (
        acceptance_mappings["accepted_ingress"][4]
        != acceptance_mappings["accepted_ingress"][5]
    )
    assert (
        acceptance_mappings["acceptance_idempotency_key"][4]
        != acceptance_mappings["acceptance_idempotency_key"][5]
    )
    child_root = _main_thread_node(fork_context, context_child.session_id) / "rollout"
    assert (child_root / "rollout.jsonl").is_file()
    assert (child_root / "index.sqlite").is_file()
    assert not list(child_root.glob("segment-*.jsonl"))

    await fork_context.sessions.delete(source.session_id)
    for child in (context_child, prefix_child, full_child):
        assert (
            await _get_tuple(fork_context, child.session_id)
            is not None
        )


@pytest.mark.asyncio
async def test_full_copy_localizes_detail_and_source_overlay_lineage(
    fork_context: ForkIntegrationContext,
    tool_set: ToolSetSnapshot,
) -> None:
    source = await _seed_source(fork_context)
    source_overlay = SimpleNamespace(
        overlay_id="overlay-source-policy",
        session_id=source.session_id,
        thread_id=fork_context.saver._storage.main_thread_id(source.session_id),
        checkpoint_ns="",
        source_kind="policy",
        source_revision="policy-revision-1",
        source_overlay_epoch=7,
        base_ref="policy-base-source",
        delta_ref=None,
        supersedes_overlay_id=None,
        materializes_overlay_id=None,
        status="active",
        idempotency_key="overlay-policy-1",
        base_source_revision="policy-revision-1",
        base_content_length=None,
        base_content_hash=None,
        base_redacted_stable_digest=None,
        delta_source_revision=None,
        delta_content_length=None,
        delta_content_hash=None,
        delta_redacted_stable_digest=None,
        delta_from_revision=None,
        delta_to_revision=None,
        delta_diff_hash=None,
    )
    overlay_body = {"policy": "source-only instruction"}
    fork_context.saver.register_source_overlay(
        source_overlay,
        session_id=source.session_id,
        thread_id=source_overlay.thread_id,
        base_content=overlay_body,
    )
    detail = fork_context.saver._detail_store.write(
        session_id=source.session_id,
        thread_id=fork_context.saver._storage.main_thread_id(source.session_id),
        assembly_id="assembly-source-detail",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"blocks": [{"type": "text", "text": "source detail"}]},
        required=True,
        source_revision="detail-revision-1",
    )
    fork_context.saver._storage.register_context_plan_detail(detail)

    child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
        checkpoint_id="cp2",
    )

    source_thread_id = fork_context.saver._storage.main_thread_id(source.session_id)
    target_thread_id = fork_context.saver._storage.main_thread_id(child.session_id)
    source_overlays = fork_context.saver.list_source_overlays(
        source.session_id,
        source_thread_id,
    )
    target_overlays = fork_context.saver.list_source_overlays(
        child.session_id,
        target_thread_id,
    )
    assert len(source_overlays) == 1
    assert len(target_overlays) == 1
    source_row = source_overlays[0]
    target_row = target_overlays[0]
    assert target_row["session_id"] == child.session_id
    assert target_row["overlay_id"] != source_row["overlay_id"]
    assert target_row["base_ref"] != source_row["base_ref"]
    assert target_row["source_revision"] == source_row["source_revision"]
    assert target_row["base_content_hash"] == source_row["base_content_hash"]
    assert source_row["source_overlay_epoch"] == 7
    assert target_row["source_overlay_epoch"] == 0

    overlay_mapping = next(
        row
        for row in _identity_mapping_rows(fork_context, child.session_id)
        if row[3] == "source_overlay"
    )
    overlay_lineage = json.loads(str(overlay_mapping[8]))
    assert overlay_lineage["source"]["session_id"] == source.session_id
    assert overlay_lineage["target"]["session_id"] == child.session_id
    overlay_lineage_details = overlay_lineage["source_overlay"]
    assert overlay_lineage_details["source_base_ref"] == source_row["base_ref"]
    assert overlay_lineage_details["target_base_ref"] == target_row["base_ref"]
    assert overlay_lineage_details["source_overlay_epoch"] == 7
    assert overlay_lineage_details["target_overlay_epoch"] == 0

    source_plan = fork_context.saver.compose_committed_context_plan(
        source.session_id,
        source_thread_id,
        plan_id="plan-source-overlay",
        tool_set=tool_set,
    )
    target_plan = fork_context.saver.compose_committed_context_plan(
        child.session_id,
        target_thread_id,
        plan_id="plan-target-overlay",
        tool_set=tool_set,
    )
    source_overlay_refs = {
        ref.source_ref: ref for ref in source_plan.refs if ref.source_ref is not None
    }
    target_overlay_refs = {
        ref.source_ref: ref for ref in target_plan.refs if ref.source_ref is not None
    }
    assert source_row["base_ref"] in source_overlay_refs
    assert target_row["base_ref"] in target_overlay_refs
    assert (
        target_overlay_refs[target_row["base_ref"]].content_hash
        == source_overlay_refs[source_row["base_ref"]].content_hash
    )
    assert target_overlay_refs[target_row["base_ref"]].source_revision == (
        source_overlay_refs[source_row["base_ref"]].source_revision
    )

    detail_mapping = next(
        row
        for row in _identity_mapping_rows(fork_context, child.session_id)
        if row[3] == "detail" and row[4] == detail_ref_key(detail.detail_ref)
    )
    target_detail_ref = detail_ref_from_key(detail_mapping[5])
    target_detail_ref.require_owner(
        child.session_id,
        fork_context.saver._storage.main_thread_id(child.session_id),
    )
    target_detail = fork_context.saver._storage.get_context_plan_detail(
        child.session_id,
        target_detail_ref.thread_id,
        detail_ref=target_detail_ref,
    )
    assert target_detail["session_id"] == child.session_id
    assert target_detail["detail_ref"] != detail.detail_ref
    assert target_detail["detail_id"] == target_detail_ref.detail_id != detail.detail_id
    assert target_detail["length"] == detail.length
    assert target_detail["expires_at"] == detail.expires_at
    assert target_detail["relative_path"] != detail.relative_path
    assert target_detail["relative_path"] == (
        f"rollout/context-plan-details/{target_detail_ref.assembly_id}/{target_detail_ref.detail_id}"
    )
    assert fork_context.saver.read_context_plan_detail(
        child.session_id,
        target_detail_ref.thread_id,
        detail_ref=target_detail_ref,
    )["detail"] == {"blocks": [{"type": "text", "text": "source detail"}]}

    source_root = _main_thread_node(fork_context, source.session_id)
    target_root = _main_thread_node(fork_context, child.session_id)
    assert (source_root / detail.relative_path).is_file()
    assert not (target_root / detail.relative_path).exists()
    assert (target_root / target_detail["relative_path"]).is_file()


@pytest.mark.asyncio
async def test_history_replay_reuses_turn_root_without_execution(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    source_root = _main_thread_node(fork_context, source.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        before_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        turn_id, turn_ordinal, root_item_id, source_branch_id = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()
        before_branches = connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall()

    latest = await _get_tuple(fork_context, source.session_id)
    assert latest is not None
    source_owner = fork_context.saver.resolve_main_thread_owner(source.session_id)
    with session_thread_owner_scope(source_owner, langgraph_thread_id=source_owner.thread_id):
        replay_config = fork_context.saver.history_replay(
            latest.config, turn_id="turn-1", anchor_mode="inclusive",
        )
        history_tuple = fork_context.saver.get_tuple(replay_config)
        assert history_tuple is not None
        assert history_tuple.config["configurable"]["history_view_id"] == replay_config["configurable"]["history_view_id"]
        assert [message.id for message in history_tuple.checkpoint["channel_values"]["messages"]] == ["u1", "a1"]
        with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
            history_dump = tuple(connection.iterdump())
        history_jsonl = (source_root / "rollout" / "rollout.jsonl").read_bytes()
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.put(
                replay_config, _checkpoint("history-write-must-not-exist", []),
                {"source": "loop", "step": 1}, {"messages": "history-write"},
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.aput(
                history_tuple.config,
                _checkpoint("history-async-write-must-not-exist", []),
                {"source": "loop", "step": 1}, {"messages": "history-write"},
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.put_writes(
                replay_config, [("messages", "历史投影禁止写入执行结果")],
                "history-write-task", "history/write",
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.aput_writes(
                history_tuple.config, [("messages", "异步历史投影禁止写入执行结果")],
                "history-async-write-task", "history/async-write",
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.rewind(replay_config, checkpoint_id="cp1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.arewind(history_tuple.config, checkpoint_id="cp1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.rewind_to_turn(replay_config, turn_id="turn-1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.arewind_to_turn(history_tuple.config, turn_id="turn-1")
        readonly_tuple = await fork_context.saver.aget_tuple(history_tuple.config)
        assert readonly_tuple is not None
        assert readonly_tuple.config == history_tuple.config
        assert readonly_tuple.checkpoint == history_tuple.checkpoint
        with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
            assert tuple(connection.iterdump()) == history_dump
        assert (source_root / "rollout" / "rollout.jsonl").read_bytes() == history_jsonl
    assert replay_config["configurable"]["thread_id"] == source_owner.thread_id
    assert replay_config["configurable"]["checkpoint_id"] == "cp3"

    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        after_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        history_view = connection.execute(
            "SELECT v.view_id, v.view_kind, cvt.turn_id, cvt.root_input_item_id, "
            "cvt.logical_turn_ordinal FROM context_views AS v "
            "JOIN context_view_turns AS cvt ON cvt.view_id = v.view_id "
            "WHERE v.view_id = ?",
            (replay_config["configurable"]["history_view_id"],),
        ).fetchone()
        copied_turn = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()
        assert connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall() == before_branches
        assert connection.execute(
            "SELECT count(*) FROM checkpoints WHERE checkpoint_id='history-write-must-not-exist'"
        ).fetchone() == (0,)

    assert after_executions == before_executions
    assert copied_turn == (turn_id, turn_ordinal, root_item_id, source_branch_id)
    assert history_view[1:] == (
        "history_replay",
        "turn-1",
        root_item_id,
        1,
    )


@pytest.mark.asyncio
async def test_stale_fork_anchor_is_rejected_before_target_creation(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    before = await fork_context.sessions.list(limit=100)

    with pytest.raises(KeyError, match="源 rollout 不存在 anchor"):
        await fork_context.forks.fork(
            source.session_id,
            mode="context_fork",
            anchor="anchor-not-in-source",
        )

    after = await fork_context.sessions.list(limit=100)
    assert after.total == before.total == 1
    assert [session.session_id for session in after.items] == [source.session_id]


@pytest.mark.asyncio
async def test_full_copy_without_selector_keeps_complete_history_after_restart(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
    )

    restarted = RolloutCheckpointSaver(
        fork_context.sessions.path_resolver.sessions_root
    )
    restored = await _get_tuple(
        fork_context,
        child.session_id,
        saver=restarted,
    )
    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一", "问题二"]

    source_root = _main_thread_node(fork_context, source.session_id)
    child_root = _main_thread_node(fork_context, child.session_id)
    with (
        sqlite3.connect(source_root / "rollout" / "index.sqlite") as source_connection,
        sqlite3.connect(child_root / "rollout" / "index.sqlite") as child_connection,
    ):
        source_turns = source_connection.execute(
            "SELECT turn_id, status FROM turn_records ORDER BY turn_ordinal"
        ).fetchall()
        target_turns = child_connection.execute(
            "SELECT turn_id, status, turn_ordinal FROM turn_records ORDER BY turn_ordinal"
        ).fetchall()

    assert source_turns == [("turn-1", "completed"), ("turn-2", "active")]
    assert [row[1] for row in target_turns] == ["completed", "cancelled"]
    assert [row[2] for row in target_turns] == [1, 2]
    assert all(row[0] not in {"turn-1", "turn-2"} for row in target_turns)
    mapping_rows = _identity_mapping_rows(fork_context, child.session_id)
    turn_mappings = [row for row in mapping_rows if row[3] == "turn"]
    assert {row[4] for row in turn_mappings} == {"turn-1", "turn-2"}
    assert all(row[5] not in {"turn-1", "turn-2"} for row in turn_mappings)


@pytest.mark.asyncio
async def test_interrupted_fork_materialization_is_rolled_back_on_next_open(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await fork_context.sessions.create(
        SessionCreateRequest(title="中断 fork 源会话")
    )
    target = await fork_context.sessions.create(
        SessionCreateRequest(title="中断 fork 目标会话")
    )
    source_thread_id = _main_thread_id(fork_context, source.session_id)
    target_thread_id = _main_thread_id(fork_context, target.session_id)
    source_snapshot_sha256 = hashlib.sha256(
        b"synthetic-source-snapshot:interrupted-fork"
    ).hexdigest()
    materialization_id, _fork_id = (
        fork_context.saver._storage.begin_fork_materialization(
            target_session_id=target.session_id,
            source_session_id=source.session_id,
            source_checkpoint_id=None,
            source_view_id=None,
            fork_mode="context_fork",
            relationship="detached",
            checkpoint_ns="",
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            source_snapshot_sha256=source_snapshot_sha256,
        )
    )

    # 新的 RolloutStorage 实例模拟进程重启：内存中的 active 标记不存在，
    # initialize 必须按 journal 清理目标，而不是把半成品当成空 checkpoint。
    restarted_storage = RolloutStorage(
        fork_context.sessions.path_resolver.sessions_root
    )
    restarted_storage.initialize(target.session_id)
    target_root = _main_thread_node(fork_context, target.session_id)
    with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT status, source_thread_id, target_thread_id, checkpoint_ns, "
            "source_snapshot_sha256 FROM fork_materializations "
            "WHERE materialization_id = ?",
            (materialization_id,),
        ).fetchone() == (
            "aborted",
            source_thread_id,
            target_thread_id,
            "",
            source_snapshot_sha256,
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert (target_root / "rollout" / "rollout.jsonl").read_bytes() == b""


@pytest.mark.asyncio
async def test_context_fork_anchor_selects_completed_target_turn(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id)
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]


@pytest.mark.asyncio
async def test_default_fork_does_not_materialize_running_tail(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id)
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][2] == "cp2"
    child_root = _main_thread_node(fork_context, child.session_id)
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM turns WHERE turn_id = 'turn-2'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_fork_rejects_explicit_running_turn(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    with pytest.raises(ValueError, match="运行中的 Turn 不支持 fork"):
        await fork_context.forks.fork(source.session_id, turn_id="turn-2")


@pytest.mark.asyncio
async def test_context_fork_turn_id_resolves_latest_active_lineage_view(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id, turn_id="turn-1")
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][3] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["history_prefix_fork", "full_rollout_copy"])
async def test_non_default_fork_modes_honor_turn_id_boundary(
    fork_context: ForkIntegrationContext,
    mode: str,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(
        source.session_id,
        mode=mode,  # type: ignore[arg-type]
        turn_id="turn-1",
    )
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]


@pytest.mark.asyncio
async def test_pinned_fork_keeps_source_deletion_blocked(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    child = await fork_context.forks.fork(
        source.session_id,
        pinned=True,
        place_under_source=True,
    )
    with pytest.raises(RuntimeError, match=child.session_id):
        await fork_context.sessions.delete(source.session_id)


@pytest.mark.asyncio
async def test_pinned_fork_retention_released_when_child_is_deleted(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    child = await fork_context.forks.fork(
        source.session_id,
        pinned=True,
        place_under_source=True,
    )

    active_refs = _retention_refs(fork_context, source.session_id)
    assert len(active_refs) == 1
    assert active_refs[0][0] == "fork"
    assert active_refs[0][2] is not None
    assert active_refs[0][3] == child.session_id
    assert active_refs[0][4] == "active"

    await fork_context.sessions.delete(child.session_id)

    released_refs = _retention_refs(fork_context, source.session_id)
    assert released_refs[0][4] == "released"
    await fork_context.sessions.delete(source.session_id)


@pytest.mark.asyncio
async def test_context_and_history_preflight_rejects_before_target_creation(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    before = await fork_context.sessions.list(limit=100)
    assert before.total == 1

    with pytest.raises(ValueError, match="运行中的 Turn 不支持 fork"):
        await fork_context.forks.fork(source.session_id, turn_id="turn-2")

    with pytest.raises(KeyError, match="fork source checkpoint 不存在"):
        await fork_context.forks.fork(
            source.session_id,
            mode="history_prefix_fork",
            checkpoint_id="checkpoint-does-not-exist",
        )

    after = await fork_context.sessions.list(limit=100)
    assert after.total == before.total == 1
    assert [session.session_id for session in after.items] == [source.session_id]


@pytest.mark.asyncio
async def test_full_copy_cancelled_historical_is_not_resumable_but_new_replay_is(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await fork_context.sessions.create(
        SessionCreateRequest(title="取消态 full-copy 源会话")
    )
    accepted = fork_context.saver.accept_turn(
        source.session_id,
        accepted_ingress_id="cancelled-ingress-1",
        acceptance_idempotency_key="cancelled-acceptance-1",
        job_id="cancelled-job-1",
        admission_idempotency_key="cancelled-admission-1",
        payload="取消后只能新 Turn 回放",
        turn_id="turn-cancelled-source",
        root_item_id="item-cancelled-source",
        initial_execution_id="execution-cancelled-source",
    )
    source_root = _main_thread_node(fork_context, source.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        item_offset, item_length = connection.execute(
            "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
            ("item-cancelled-source",),
        ).fetchone()
        item_line = (source_root / "rollout" / "rollout.jsonl").read_bytes()[
            item_offset : item_offset + item_length
        ]
        connection.execute(
            "INSERT INTO item_parts(item_id, part_id, part_ordinal, part_semantic_kind, "
            "content_prefix_hash, content_hash, locator_json, line_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-cancelled-source",
                "part-cancelled-source",
                0,
                "text",
                None,
                "sha256:jcs:v1:part-placeholder",
                '{"json_pointer":"/payload"}',
                hashlib.sha256(item_line).hexdigest(),
                "2026-08-15T00:00:00+00:00",
            ),
        )
        connection.commit()
    fork_context.saver.converge_execution(
        source.session_id,
        turn_id="turn-cancelled-source",
        execution_id=str(accepted["initial_execution_id"]),
        outcome="cancelled",
        turn_status="cancelled",
    )

    child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
    )
    child_thread_id = fork_context.saver._storage.main_thread_id(child.session_id)
    child_root = _main_thread_node(fork_context, child.session_id)
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        target_turn_id, target_root_id, target_status = connection.execute(
            "SELECT turn_id, root_input_item_id, status FROM turn_records"
        ).fetchone()
        execution_ids = connection.execute(
            "SELECT execution_id FROM executions WHERE turn_id = ?",
            (target_turn_id,),
        ).fetchall()
        part_row = connection.execute(
            "SELECT item_id, part_id, part_ordinal FROM item_parts"
        ).fetchone()
    assert target_turn_id != "turn-cancelled-source"
    assert target_root_id != "item-cancelled-source"
    assert target_status == "cancelled"
    assert execution_ids
    assert part_row[0] != "item-cancelled-source"
    assert part_row[1] != "part-cancelled-source"
    assert part_row[2] == 0

    mapping_rows = _identity_mapping_rows(fork_context, child.session_id)
    part_mappings = [row for row in mapping_rows if row[3] == "content_part"]
    assert len(part_mappings) == 1
    part_lineage = json.loads(str(part_mappings[0][8]))
    assert part_mappings[0][4] == "item-cancelled-source:part-cancelled-source"
    assert part_mappings[0][5] == f"{part_row[0]}:{part_row[1]}"
    assert part_mappings[0][6] is not None
    assert part_mappings[0][7] is not None
    assert part_lineage["source"]["ordinal"] == part_lineage["target"]["ordinal"]

    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.resume_turn(
            child.session_id,
            thread_id=child_thread_id,
            job_id="job-fork-copy-resume-rejected",
            turn_id=target_turn_id,
        )
    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.dispatch_replay(
            child.session_id,
            thread_id=child_thread_id,
            turn_id=target_turn_id,
        )

    replayed = fork_context.saver.replay_as_new_turn(
        child.session_id,
        thread_id=child_thread_id,
        source_turn_id=target_turn_id,
        acceptance_idempotency_key="child-replay-acceptance-1",
    )
    assert replayed["turn_id"] != target_turn_id
    assert replayed["replay_of_turn_id"] == target_turn_id
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        replay_row = connection.execute(
            "SELECT root_input_item_id, status, replay_of_turn_id FROM turn_records "
            "WHERE turn_id = ?",
            (replayed["turn_id"],),
        ).fetchone()
    assert replay_row[0] != target_root_id
    assert replay_row[1] == "active"
    assert replay_row[2] == target_turn_id


@pytest.mark.asyncio
async def test_full_copy_interrupted_turn_is_cancelled_historical_with_source_job_ref(
    fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context)
    source_thread_id = fork_context.saver._storage.main_thread_id(source.session_id)
    fork_context.saver.converge_execution(
        source.session_id,
        thread_id=source_thread_id,
        turn_id="turn-2",
        execution_id="execution-turn-2",
        outcome="interrupted",
        turn_status="interrupted",
    )

    job_start_calls = 0

    def reject_job_start(*_args: object, **_kwargs: object) -> None:
        nonlocal job_start_calls
        job_start_calls += 1
        raise AssertionError("fork must not create or start a Job")

    monkeypatch.setattr(JobService, "start_job", reject_job_start)
    child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
        checkpoint_id="cp3",
    )
    assert job_start_calls == 0

    identity_rows = _identity_mapping_rows(fork_context, child.session_id)
    turn_mapping = next(
        row
        for row in identity_rows
        if row[3] == "turn" and row[4] == "turn-2"
    )
    execution_mapping = next(
        row
        for row in identity_rows
        if row[3] == "execution" and row[4] == "execution-turn-2"
    )
    target_turn_id = str(turn_mapping[5])
    target_execution_id = str(execution_mapping[5])
    lineage = json.loads(str(execution_mapping[8]))
    source_job_id = "job-turn-2"
    expected_job_ref = {
        "type": "GlobalEntityRef",
        "session_id": source.session_id,
        "thread_id": source_thread_id,
        "entity_type": "job",
        "local_id": source_job_id,
    }
    assert lineage["source"]["job_ref"] == expected_job_ref
    assert lineage["target"]["job_ref"] == expected_job_ref
    assert "job_id" not in lineage["source"]
    assert "job_id" not in lineage["target"]
    assert target_execution_id != "execution-turn-2"
    assert lineage["target"]["session_id"] == child.session_id

    source_root = _main_thread_node(fork_context, source.session_id)
    target_root = _main_thread_node(fork_context, child.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        source_turn_status = connection.execute(
            "SELECT status FROM turn_records WHERE turn_id = ?",
            ("turn-2",),
        ).fetchone()[0]
        source_execution = connection.execute(
            "SELECT session_id, thread_id, turn_id, execution_id, job_id, "
            "admission_idempotency_key, admission_payload_hash, accepted_ingress_id, outcome "
            "FROM executions WHERE execution_id = ?",
            ("execution-turn-2",),
        ).fetchone()
        source_acceptance = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key, payload_hash "
            "FROM turn_acceptances "
            "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
            (source.session_id, source_thread_id, "turn-2"),
        ).fetchone()
        source_payload_hash = source_acceptance[2]
    target_thread_id = fork_context.saver._storage.main_thread_id(child.session_id)
    with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
        target_turn = connection.execute(
            "SELECT session_id, thread_id, turn_id, status, initial_execution_id, "
            "accepted_ingress_id, acceptance_idempotency_key "
            "FROM turn_records WHERE turn_id = ?",
            (target_turn_id,),
        ).fetchone()
        target_acceptance = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key, payload_hash, "
            "source_session_id, source_accepted_ingress_id, "
            "source_acceptance_idempotency_key, identity_origin "
            "FROM turn_acceptances WHERE session_id = ? AND thread_id = ? "
            "AND turn_id = ?",
            (child.session_id, target_thread_id, target_turn_id),
        ).fetchone()
        target_execution = connection.execute(
            "SELECT session_id, thread_id, turn_id, execution_id, job_id, "
            "admission_idempotency_key, admission_payload_hash, accepted_ingress_id, outcome "
            "FROM executions WHERE execution_id = ?",
            (target_execution_id,),
        ).fetchone()
        target_terminal_commit = connection.execute(
            "SELECT outcome, metadata_json FROM storage_commits "
            "WHERE commit_kind = 'terminal_convergence' AND subject_id = ? "
            "ORDER BY commit_id DESC LIMIT 1",
            (target_turn_id,),
        ).fetchone()
        target_execution_history = connection.execute(
            "SELECT execution_id, job_id, outcome FROM executions WHERE turn_id = ? "
            "ORDER BY execution_ordinal",
            (target_turn_id,),
        ).fetchall()
        target_payload_hash = target_acceptance[2]

    assert source_execution[:6] == (
        source.session_id,
        source_thread_id,
        "turn-2",
        "execution-turn-2",
        source_job_id,
        "admission-turn-2",
    )
    assert source_turn_status == "interrupted"
    assert source_execution[7:] == ("u2", "interrupted")
    assert source_execution[6] == user_turn_admission_payload_hash(
        session_id=source.session_id,
        thread_id=source_thread_id,
        turn_id="turn-2",
        execution_id="execution-turn-2",
        job_id=source_job_id,
        accepted_ingress_id="u2",
        admission_idempotency_key="admission-turn-2",
        payload_hash=str(source_payload_hash),
    )
    assert target_turn == (
        child.session_id,
        target_thread_id,
        target_turn_id,
        "cancelled",
        target_execution_id,
        target_acceptance[0],
        target_acceptance[1],
    )
    assert target_execution[:5] == (
        child.session_id,
        target_thread_id,
        target_turn_id,
        target_execution_id,
        source_job_id,
    )
    assert target_execution[8] == "cancelled"
    assert target_acceptance[0] != source_acceptance[0]
    assert target_acceptance[1] != source_acceptance[1]
    assert target_acceptance[2] == source_acceptance[2]
    assert target_acceptance[3:] == (
        source.session_id,
        source_acceptance[0],
        source_acceptance[1],
        "fork_copied",
    )
    assert target_execution[7] == target_acceptance[0]
    assert lineage["source"]["admission_idempotency_key"] == source_execution[5]
    assert lineage["source"]["admission_payload_hash"] == source_execution[6]
    assert lineage["target"]["admission_idempotency_key"] == target_execution[5]
    assert lineage["target"]["admission_payload_hash"] == target_execution[6]
    assert lineage["source"]["accepted_ingress_id"] == source_acceptance[0]
    assert lineage["target"]["accepted_ingress_id"] == target_acceptance[0]
    assert target_terminal_commit is not None
    assert target_terminal_commit[0] == "cancelled"
    assert json.loads(str(target_terminal_commit[1])) == {
        "historical": True,
        "reason": "fork_source_runtime_not_copied",
    }
    assert target_execution[5] != source_execution[5]
    assert target_execution[6] != source_execution[6]
    assert target_execution[7] != source_execution[7]
    assert target_payload_hash == source_payload_hash
    assert target_execution[6] == user_turn_admission_payload_hash(
        session_id=child.session_id,
        thread_id=target_thread_id,
        turn_id=target_turn_id,
        execution_id=target_execution_id,
        job_id=source_job_id,
        accepted_ingress_id=str(target_execution[7]),
        admission_idempotency_key=str(target_execution[5]),
        payload_hash=str(target_payload_hash),
    )
    target_admission = fork_context.saver.execution_admission(
        child.session_id,
        thread_id=target_thread_id,
        execution_id=target_execution_id,
    )
    assert target_admission.session_id == child.session_id
    assert target_admission.thread_id == target_thread_id
    assert target_admission.turn_id == target_turn_id
    assert target_admission.job_id == source_job_id
    assert target_admission.admission_idempotency_key == target_execution[5]

    for job_id in (source_job_id, "job-target-resume-turn-2"):
        with pytest.raises(ValueError, match="turn_not_resumable"):
            fork_context.saver.resume_turn(
                child.session_id,
                thread_id=target_thread_id,
                job_id=job_id,
                turn_id=target_turn_id,
            )
    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.dispatch_replay(
            child.session_id,
            thread_id=target_thread_id,
            turn_id=target_turn_id,
        )

    with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT status FROM turn_records WHERE turn_id = ?",
            (target_turn_id,),
        ).fetchone() == ("cancelled",)
        assert connection.execute(
            "SELECT execution_id, job_id, outcome FROM executions WHERE turn_id = ? "
            "ORDER BY execution_ordinal",
            (target_turn_id,),
        ).fetchall() == target_execution_history
    assert job_start_calls == 0


@pytest.mark.asyncio
async def test_production_http_pinned_fork_and_preflight_have_exact_side_effects(
    production_fork_container: AppContainer,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    catalog = context.sessions.path_resolver.catalog_store
    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": context.sessions.workspace_id, "X-Request-ID": "fork-http-proof"},
    ) as client:
        before = (await context.sessions.list(limit=100)).total
        with catalog.read_transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM session_creation_records), "
                "(SELECT COUNT(*) FROM fork_retention_claims)"
            ).fetchone()
        rejected = await client.post(
            f"/api/v1/sessions/{source.session_id}/fork-context",
            json={"pinned": True, "turn_id": "turn-2"},
        )
        assert rejected.status_code == 400, rejected.text
        assert "运行中的 Turn" in rejected.json()["detail"]
        assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == "fork-http-proof"
        assert (await context.sessions.list(limit=100)).total == before
        with catalog.read_transaction() as connection:
            assert connection.execute(
                "SELECT (SELECT COUNT(*) FROM session_creation_records), "
                "(SELECT COUNT(*) FROM fork_retention_claims)"
            ).fetchone() == counts
        assert _retention_refs(context, source.session_id) == []

        response = await client.post(
            f"/api/v1/sessions/{source.session_id}/fork-context",
            json={"pinned": True, "turn_id": "turn-1", "place_under_source": True},
        )
        assert response.status_code == 200, response.text
        child = response.json()["data"]
        assert child["context_source_session_id"] == source.session_id
        assert child["parent_session_id"] == source.session_id
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "fork-http-proof"
        target_root = _main_thread_node(context, child["session_id"])
        with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
            assert connection.execute("SELECT status FROM fork_materializations").fetchall() == [("committed",)]
            assert connection.execute(
                "SELECT COUNT(*) FROM item_catalog WHERE execution_id IS NULL OR execution_id = ''"
            ).fetchone() == (0,)
        assert len(_retention_refs(context, source.session_id)) == 1
        first = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context")
        second = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context")
        assert first.status_code == second.status_code == 200
        assert first.json()["data"]["session_id"] != second.json()["data"]["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["prepared", "target_committed", "committed"])
async def test_production_lifespan_recovers_unpublished_fork(
    production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch,
    crash_state: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    target, record = await context.sessions.prepare_context_fork(
        idempotency_key=f"lifespan-fork-{crash_state}", title="待恢复 fork",
        agent_id=source.current_agent_id, parent_session_id=None,
        context_source_session_id=source.session_id,
    )
    fork_id = f"lifespan-fork-{crash_state}"
    context.saver.begin_pinned_fork_retention_claim(
        fork_id=fork_id, source_session_id=source.session_id,
        target_session_id=target.session_id,
    )
    storage = context.saver._storage
    method = "materialize_fork_checkpoint" if crash_state == "prepared" else "complete_fork_materialization"
    original = getattr(storage, method)

    def crash(**_kwargs: object) -> None:
        raise RuntimeError("模拟进程在 fork journal 边界中断")

    if crash_state != "committed":
        monkeypatch.setattr(storage, method, crash)
    try:
        if crash_state == "committed":
            await context.saver.afork(source_session_id=source.session_id,
                                     target_session_id=target.session_id,
                                     creation_record=record, fork_id=fork_id,
                                     relationship="pinned")
        else:
            with pytest.raises(RuntimeError, match="模拟进程"):
                await context.saver.afork(source_session_id=source.session_id,
                                         target_session_id=target.session_id,
                                         creation_record=record, fork_id=fork_id,
                                         relationship="pinned")
    finally:
        monkeypatch.setattr(storage, method, original)
    assert context.sessions.path_resolver.catalog_store.get_creation_record(
        record.session_creation_idempotency_key
    ).state == "preparing"
    # 新容器从同一 durable record 装配，实际 main lifespan 必须在 ready 前收敛。
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        durable = restarted.session_service.path_resolver.catalog_store.get_creation_record(
            record.session_creation_idempotency_key
        )
        claim = restarted.session_service.path_resolver.catalog_store.get_fork_retention_claim(fork_id)
        if crash_state == "prepared":
            assert durable.state == "aborted"
            assert claim.state == "released"
            assert (await restarted.session_service.list(limit=100)).total == 1
            assert _retention_refs(_production_fork_context(restarted), source.session_id) == []
        else:
            assert durable.state == "published"
            assert claim.state == "active"
            assert (await restarted.session_service.get(target.session_id)).context_source_session_id == source.session_id
            restored = await _get_tuple(_production_fork_context(restarted), target.session_id)
            assert restored is not None
            assert [message.content for message in restored.checkpoint["channel_values"]["messages"]] == ["问题一", "回答一"]


@pytest.mark.asyncio
async def test_fork_notification_failure_never_aborts_published_target(
    fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context)
    original = fork_context.sessions._notify_changed

    def fail_notification(action: str, session_id: str) -> None:
        original(action, session_id)
        raise RuntimeError("通知失败")

    monkeypatch.setattr(fork_context.sessions, "_notify_changed", fail_notification)
    with pytest.raises(RuntimeError, match="通知失败"):
        await fork_context.forks.fork(source.session_id, pinned=True)
    assert (await fork_context.sessions.list(limit=100)).total == 2
    refs = _retention_refs(fork_context, source.session_id)
    assert len(refs) == 1 and refs[0][4] == "active"


async def _seed_retry_tool_source(context: ForkIntegrationContext) -> tuple[SessionDTO, str]:
    """通过真实 notice/user admission、lost/resume 和 checkpoint 生成执行谱系。"""
    source = await context.sessions.create(SessionCreateRequest(title="重试和工具源会话"))
    owner = context.saver.resolve_main_thread_owner(source.session_id)
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="检查内部任务结果",
        sections=(PromptSection("control_context", {"source": "fork-test"}),
                  PromptSection("generated_session_result", "内部任务已经完成")),
        display_content="内部任务已经完成。",
    )
    notice = HumanMessage(
        content=prepared.content, id="retry-notice",
        response_metadata={"message_id": "retry-notice", "created_at": "2026-10-04T12:00:00+00:00",
                           "message_metadata": prepared.metadata},
    )
    internal = context.saver.admit_internal_execution(
        source.session_id, thread_id=owner.thread_id, job_id="job-internal-notice",
        intent=InternalExecutionIntent(message_id="retry-notice", execution_id="execution-internal-notice",
                                       admission_idempotency_key="admission-internal-notice"),
        message=notice,
    )
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(internal, langgraph_thread_id=owner.thread_id)):
        config = await context.saver.aput(
            build_checkpoint_config(owner.thread_id), _checkpoint("cp-notice", [notice]),
            {"source": "input", "step": 1, "parents": {}}, {"messages": "1", "counter": "1"},
        )
    context.saver.converge_execution(
        source.session_id, thread_id=owner.thread_id, turn_id=None,
        execution_id=internal.execution_id, outcome="completed_empty", turn_status=None,
    )
    accepted = context.saver.accept_turn(
        source.session_id, thread_id=owner.thread_id, accepted_ingress_id="retry-user",
        acceptance_idempotency_key="acceptance-retry-user", job_id="job-retry-initial",
        admission_idempotency_key="admission-retry-initial", payload="检查工作区",
        turn_id="turn-retry", root_item_id="item-retry-user",
        initial_execution_id="execution-retry-initial",
    )
    initial = context.saver.execution_admission(
        source.session_id, thread_id=owner.thread_id, execution_id=str(accepted["initial_execution_id"]),
    )
    user = HumanMessage(content="检查工作区", id="retry-user", response_metadata={"turn_id": "turn-retry"})
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(initial, langgraph_thread_id=owner.thread_id)):
        config = await context.saver.aput(
            config, _checkpoint("cp-retry-user", [notice, user]),
            {"source": "input", "step": 2, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    context.saver.mark_execution_lost(
        source.session_id, thread_id=owner.thread_id, turn_id="turn-retry",
        execution_id=initial.execution_id,
    )
    resumed = context.saver.resume_turn(
        source.session_id, thread_id=owner.thread_id, turn_id="turn-retry", job_id="job-retry-resumed",
    )
    retry_id = str(resumed["execution_id"])
    retry = context.saver.execution_admission(
        source.session_id, thread_id=owner.thread_id, execution_id=retry_id,
    )
    messages = [
        notice, user,
        AIMessage(content="检查文件", id="retry-call",
                  tool_calls=[{"name": "read_file", "args": {"path": "README.md"}, "id": "retry-tool-call"}]),
        ToolMessage(content="文件内容", id="retry-result", tool_call_id="retry-tool-call"),
        AIMessage(content=[{"type": "text", "text": "检查完成"}, {"type": "text", "text": "结果已确认"}],
                  id="retry-final"),
    ]
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(retry, langgraph_thread_id=owner.thread_id)):
        await context.saver.aput(
            config, _checkpoint("cp-retry-final", messages),
            {"source": "loop", "step": 3, "parents": {}}, {"messages": "5", "counter": "5"},
        )
    context.saver.finalize_turn(
        session_id=source.session_id, thread_id=owner.thread_id, turn_id="turn-retry",
        final_message_id="retry-final",
    )
    return source, retry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "history_prefix_fork", "full_rollout_copy"])
async def test_fork_preserves_real_retry_internal_and_closed_tool_lineage(
    production_fork_container: AppContainer, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, retry_id = await _seed_retry_tool_source(context)
    source_owner = context.saver.resolve_main_thread_owner(source.session_id)
    source_items = context.saver._storage.read_items(source.session_id, source_owner.thread_id)
    child = await context.forks.fork(source.session_id, mode=mode)
    target_owner = context.saver.resolve_main_thread_owner(child.session_id)
    target_items = context.saver._storage.read_items(child.session_id, target_owner.thread_id)
    assert len(target_items) == len(source_items)
    mappings = {(row[3], row[4]): row[5] for row in _identity_mapping_rows(context, child.session_id)}
    with sqlite3.connect(_main_thread_node(context, child.session_id) / "rollout" / "index.sqlite") as connection:
        executions = connection.execute(
            "SELECT execution_id, turn_id, execution_origin, job_id, admission_idempotency_key, "
            "admission_payload_hash, resumed_from_execution_id, accepted_ingress_id FROM executions"
        ).fetchall()
        assert len(executions) == 3
        by_id = {row[0]: row for row in executions}
        initial = by_id[mappings[("execution", "execution-retry-initial")]]
        retry = by_id[mappings[("execution", retry_id)]]
        internal = by_id[mappings[("execution", "execution-internal-notice")]]
        assert initial[2] == retry[2] == "user_turn"
        assert initial[3] == "job-retry-initial" and retry[3] == "job-retry-resumed"
        assert retry[6] == initial[0]
        assert internal[1:4] == (None, "internal", "job-internal-notice")
        assert internal[7] is None
        turn = connection.execute(
            "SELECT turn_id, initial_execution_id, last_execution_id, status, final_item_id "
            "FROM turn_records"
        ).fetchone()
        assert turn[:4] == (initial[1], initial[0], retry[0], "completed")
        assert connection.execute("SELECT COUNT(*) FROM turn_acceptances").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE execution_id IS NULL OR execution_id NOT IN "
            "(SELECT execution_id FROM executions)"
        ).fetchone() == (0,)
        payload_hash = connection.execute("SELECT payload_hash FROM turn_acceptances").fetchone()[0]
        for row in (initial, retry):
            assert row[5] == user_turn_admission_payload_hash(
                session_id=child.session_id, thread_id=target_owner.thread_id, turn_id=row[1],
                execution_id=row[0], job_id=row[3], admission_idempotency_key=row[4],
                accepted_ingress_id=row[7], payload_hash=payload_hash,
            )
        notice_hash = connection.execute(
            "SELECT content_hash FROM item_catalog WHERE execution_id = ? AND semantic_kind = 'runtime_notice'",
            (internal[0],),
        ).fetchone()[0]
        assert internal[5] == notice_hash
    with sqlite3.connect(_main_thread_node(context, child.session_id) / "rollout" / "index.sqlite") as connection:
        connection_execution_by_item = dict(connection.execute("SELECT item_id, execution_id FROM item_catalog"))
    calls = [item for item in target_items if item.semantic_kind == SemanticKind.TOOL_CALL]
    results = [item for item in target_items if item.semantic_kind == SemanticKind.TOOL_RESULT]
    assert len(calls) == len(results) == 1
    call_id = calls[0].payload["tool_calls"][0]["id"]
    assert call_id == results[0].tool_call_id == results[0].payload["tool_call_id"]
    assert call_id != "retry-tool-call"
    assert connection_execution_by_item[calls[0].item_id] == connection_execution_by_item[results[0].item_id] == retry[0]
    assert results[0].producer_ref["invocation_id"] == retry[0]
    restored = await _get_tuple(context, child.session_id)
    assert restored is not None
    messages = restored.checkpoint["channel_values"]["messages"]
    assistant_call = next(message for message in messages if isinstance(message, AIMessage) and message.tool_calls)
    result = next(message for message in messages if isinstance(message, ToolMessage))
    assert assistant_call.tool_calls[0]["id"] == result.tool_call_id == call_id
    assert messages[-1].content == [{"type": "text", "text": "检查完成"}, {"type": "text", "text": "结果已确认"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "history_prefix_fork", "full_rollout_copy"])
async def test_detached_fork_uses_frozen_lineage_after_source_deletion(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, retry_id = await _seed_retry_tool_source(context)
    source_node = _main_thread_node(context, source.session_id)
    capture = context.saver._capture_fork_source_snapshot

    async def capture_then_delete(**kwargs: object):
        snapshot = await capture(**kwargs)
        await context.sessions.delete(source.session_id)
        assert not source_node.exists()
        return snapshot

    monkeypatch.setattr(context.saver, "_capture_fork_source_snapshot", capture_then_delete)
    child = await context.forks.fork(source.session_id, mode=mode)
    assert child.context_source_session_id == source.session_id
    assert (await context.sessions.list(limit=100)).total == 1
    execution_sources = {row[4] for row in _identity_mapping_rows(context, child.session_id)
                         if row[3] == "execution"}
    assert execution_sources == {"execution-retry-initial", retry_id, "execution-internal-notice"}
    assert _fork_materialization_rows(context, child.session_id)[0][2] == "committed"
    assert await _get_tuple(context, child.session_id) is not None


@pytest.mark.asyncio
async def test_generation_fork_reuses_one_durable_target_after_source_deletion(
    production_fork_container: AppContainer,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    origin = SessionGenerationOriginDTO(
        generator_id="generator-fork", run_id="run-fork", idempotency_key="generation-key-fork",
        generator_type_id="context-fork", generator_type_version="1",
    )
    results = await asyncio.gather(*(context.forks.fork(
        source.session_id, generation_origin=origin, title="幂等生成副本", turn_id="turn-1",
    ) for _ in range(3)))
    assert len({session.session_id for session in results}) == 1
    target = results[0]
    assert target.generation_origin == origin
    catalog = context.sessions.path_resolver.catalog_store
    with catalog.read_transaction() as connection:
        assert tuple(connection.execute(
            "SELECT COUNT(*) FROM session_creation_records WHERE purpose = 'context_fork'"
        ).fetchone()) == (1,)
    assert len(_fork_materialization_rows(context, target.session_id)) == 1
    await context.sessions.delete(source.session_id)
    restarted_service = SessionContextForkService(session_service=context.sessions, checkpointer=context.saver)
    repeated = await restarted_service.fork(
        source.session_id, generation_origin=origin, title="幂等生成副本", turn_id="turn-1",
    )
    assert repeated.session_id == target.session_id
    assert (await context.sessions.list(limit=100)).total == 1


def _fork_intent_state(context: ForkIntegrationContext) -> tuple[object, ...]:
    """请求冲突前后比较权威 creation/retention/navigation 与物理节点清单。"""
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        rows = tuple(tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                     for table in ("session_creation_records", "fork_retention_claims", "nodes", "catalog_metadata"))
    entries = tuple(sorted(str(path.relative_to(context.workspace / ".boxteam"))
                           for path in (context.workspace / ".boxteam").rglob("*")))
    return rows, entries


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["published", "preparing"])
@pytest.mark.parametrize("drift", ["mode", "pinned", "title", "parent_node_id", "place_under_source",
                                 "turn_id", "anchor_mode", "checkpoint_id", "anchor", "checkpoint_ns"])
async def test_generation_fork_intent_drift_rejects_before_any_side_effect(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    state: str, drift: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    origin = SessionGenerationOriginDTO(generator_id="generator-intent", run_id="run-intent",
                                       idempotency_key="key-intent", generator_type_id="context-fork",
                                       generator_type_version="1")
    entered, release = asyncio.Event(), asyncio.Event()
    capture = context.saver._capture_fork_source_snapshot

    async def wait_before_capture(**kwargs: object):
        entered.set()
        await release.wait()
        return await capture(**kwargs)

    if state == "preparing":
        monkeypatch.setattr(context.saver, "_capture_fork_source_snapshot", wait_before_capture)
    initial = asyncio.create_task(context.forks.fork(
        source.session_id, generation_origin=origin, title="冻结分叉意图", turn_id="turn-1",
    ))
    if state == "preparing":
        await asyncio.wait_for(entered.wait(), timeout=10)
    else:
        await initial
        await context.sessions.delete(source.session_id)
    overrides: dict[str, object] = {
        "mode": "full_rollout_copy", "pinned": True, "title": "另一标题",
        "parent_node_id": source.session_id, "place_under_source": True,
        "turn_id": "turn-2", "anchor_mode": "before", "checkpoint_id": "cp1",
        "anchor": "u1", "checkpoint_ns": "other-namespace",
    }
    request: dict[str, object] = {"title": "冻结分叉意图", "turn_id": "turn-1", drift: overrides[drift]}
    if drift in {"checkpoint_id", "anchor"}:
        request["turn_id"] = None
    repeated_service = SessionContextForkService(session_service=context.sessions, checkpointer=context.saver)
    before = _fork_intent_state(context)
    try:
        with pytest.raises(RuntimeError, match="请求意图冲突"):
            await repeated_service.fork(source.session_id, generation_origin=origin, **request)
        assert _fork_intent_state(context) == before
    finally:
        release.set()
        await initial


@pytest.mark.asyncio
async def test_generation_implicit_latest_fork_replays_frozen_request_after_source_deletion(
    production_fork_container: AppContainer,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    origin = SessionGenerationOriginDTO(generator_id="generator-latest", run_id="run-latest",
                                       idempotency_key="key-latest", generator_type_id="context-fork",
                                       generator_type_version="1")
    target = await context.forks.fork(source.session_id, generation_origin=origin)
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        (raw_intent,) = connection.execute(
            "SELECT request_intent_json FROM session_creation_records WHERE session_id = ?", (target.session_id,)
        ).fetchone()
    intent = json.loads(raw_intent)
    assert intent["title"] is None and intent["turn_id"] is None and intent["checkpoint_id"] is None
    assert "source_checkpoint_id" not in intent and "source_view_id" not in intent
    assert "request_intent_json" not in json.loads((context.sessions.path_resolver.resolve_session_node(target.session_id)
                                                   / "session.json").read_text())
    await context.sessions.delete(source.session_id)
    repeated_service = SessionContextForkService(session_service=context.sessions, checkpointer=context.saver)
    before = _fork_intent_state(context)
    repeated = await repeated_service.fork(source.session_id, generation_origin=origin, title="")
    assert repeated.session_id == target.session_id
    assert _fork_intent_state(context) == before


@pytest.mark.asyncio
async def test_folder_move_keeps_context_fork_kind_across_restart(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 fork 发布后移动 folder，只改 catalog 父关系，重启保持来源与 manifest。"""
    context = _production_fork_context(production_fork_container)
    parent = await _seed_source(context)
    child = await context.forks.fork(
        parent.session_id, title="上下文副本", place_under_source=True, turn_id="turn-1",
    )
    resolver = context.sessions.path_resolver
    manifest_path = resolver.resolve_session_node(child.session_id) / "session.json"
    manifest_before = manifest_path.read_bytes()
    journal_before = _fork_materialization_rows(context, child.session_id)
    assert len(journal_before) == 1 and journal_before[0][2] == "committed"
    assert child.parent_session_id == parent.session_id
    catalog = production_fork_container.session_catalog_service
    folder_receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="子目录", parent_node_id=parent.session_id,
    ))
    assert folder_receipt.state == "committed", folder_receipt.error_detail
    folder_id = folder_receipt.result_node_id
    assert folder_id is not None
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=child.session_id, parent_node_id=folder_id,
    ))
    assert receipt.state == "committed", receipt.error_detail
    assert (await context.sessions.get(child.session_id)).parent_session_id == parent.session_id
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=folder_id, parent_node_id=None,
    ))
    assert receipt.state == "committed", receipt.error_detail
    moved = await context.sessions.get(child.session_id)
    assert moved.kind == "context_fork" and moved.title == "上下文副本"
    assert moved.parent_session_id is None
    assert moved.context_source_session_id == parent.session_id
    assert manifest_path.read_bytes() == manifest_before
    assert json.loads(manifest_before)["kind"] == "context_fork"
    assert resolver.get_node(folder_id).parent_node_id is None
    assert resolver.get_node(child.session_id).parent_node_id == folder_id
    assert _fork_materialization_rows(context, child.session_id) == journal_before

    # 清空原 catalog 装配后从同一物理工作区重启，不用拷贝或修改 manifest 造状态。
    production_fork_container.workspace_activity_service.store.close()
    resolver.catalog_store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        restored = await restarted.session_service.get(child.session_id)
        assert restored.kind == "context_fork" and restored.title == "上下文副本"
        assert restored.parent_session_id is None
        assert restored.context_source_session_id == parent.session_id
        restarted_resolver = restarted.session_service.path_resolver
        assert restarted_resolver.get_node(child.session_id).parent_node_id == folder_id
        assert restarted_resolver.get_node(folder_id).parent_node_id is None
        assert (restarted_resolver.resolve_session_node(child.session_id)
                / "session.json").read_bytes() == manifest_before
        assert _fork_materialization_rows(_production_fork_context(restarted), child.session_id) == journal_before


@pytest.mark.asyncio
async def test_moving_parent_session_carries_complete_child_tree(
    production_fork_container: AppContainer,
) -> None:
    """真实 fork 随父会话逻辑移动，物理节点、子关系与来源保持一致。"""
    context = _production_fork_context(production_fork_container)
    parent = await _seed_source(context)
    child = await context.forks.fork(
        parent.session_id, title="Child", place_under_source=True, turn_id="turn-1",
    )
    resolver = context.sessions.path_resolver
    child_path_before = resolver.resolve_session_node(child.session_id)
    manifest_before = (child_path_before / "session.json").read_bytes()
    catalog = production_fork_container.session_catalog_service
    folder_receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="目标文件夹", parent_node_id=None,
    ))
    assert folder_receipt.state == "committed", folder_receipt.error_detail
    folder_id = folder_receipt.result_node_id
    assert folder_id is not None
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=parent.session_id, parent_node_id=folder_id,
    ))
    assert receipt.state == "committed", receipt.error_detail
    moved_parent_path = resolver.resolve_session_node(parent.session_id)
    moved_child_path = resolver.resolve_session_node(child.session_id)
    assert (await context.sessions.get(parent.session_id)).parent_session_id is None
    moved = await context.sessions.get(child.session_id)
    assert moved.parent_session_id == parent.session_id
    assert moved.kind == "context_fork" and moved.title == "Child"
    assert moved.context_source_session_id == parent.session_id
    assert moved_parent_path.parent == child_path_before.parent
    assert moved_child_path.parent == moved_parent_path.parent
    assert resolver.get_node(parent.session_id).parent_node_id == folder_id
    assert resolver.get_node(child.session_id).parent_node_id == parent.session_id
    assert moved_child_path.name == child_path_before.name
    assert moved_child_path == child_path_before
    assert (moved_child_path / "session.json").read_bytes() == manifest_before
    assert _fork_materialization_rows(context, child.session_id)[0][2] == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "history_prefix_fork", "full_rollout_copy"])
async def test_fork_rejects_internal_admission_hash_without_notice_preimage(
    production_fork_container: AppContainer, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context)
    with sqlite3.connect(_main_thread_node(context, source.session_id) / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE executions SET admission_payload_hash = (SELECT content_hash FROM item_catalog "
            "WHERE item_id = 'item-retry-user') WHERE execution_id = 'execution-internal-notice'"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="internal admission hash 与冻结 notice 不一致"):
        await context.forks.fork(source.session_id, mode=mode)
    assert (await context.sessions.list(limit=100)).total == 1
    assert _retention_refs(context, source.session_id) == []


@pytest.mark.asyncio
async def test_pinned_preparing_claim_blocks_source_deletion_before_capture(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    generation = context.saver._storage._source_lifecycle_generation(source.session_id)
    entered, proceed = asyncio.Event(), asyncio.Event()
    capture = context.saver._capture_fork_source_snapshot

    async def paused_capture(**kwargs: object):
        entered.set()
        await proceed.wait()
        return await capture(**kwargs)

    monkeypatch.setattr(context.saver, "_capture_fork_source_snapshot", paused_capture)
    pending = asyncio.create_task(context.forks.fork(source.session_id, pinned=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        catalog = context.sessions.path_resolver.catalog_store
        with catalog.read_transaction() as connection:
            claim = tuple(connection.execute(
                "SELECT fork_retention_claim_id, target_session_id, source_lifecycle_generation, state "
                "FROM fork_retention_claims WHERE source_session_id = ?", (source.session_id,),
            ).fetchone())
        assert claim[2:] == (generation, "preparing")
        assert (await context.sessions.get(source.session_id)).session_id == source.session_id
        with pytest.raises(RuntimeError, match="source_retention_operation_pending"):
            await context.sessions.delete(source.session_id)
        assert context.saver._storage._source_lifecycle_generation(source.session_id) == generation
        assert _main_thread_node(context, source.session_id).is_dir()
    finally:
        proceed.set()
        child = await pending
    active = catalog.get_fork_retention_claim(claim[0])
    assert active.target_session_id == child.session_id == claim[1]
    assert active.source_lifecycle_generation == generation and active.state == "active"
    assert len(_retention_refs(context, source.session_id)) == 1


@pytest.mark.asyncio
async def test_source_deletion_before_pinned_claim_aborts_prepared_target(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    prepare = context.sessions.prepare_context_fork

    async def prepare_then_delete(**kwargs: object):
        prepared = await prepare(**kwargs)
        await context.sessions.delete(source.session_id)
        return prepared

    monkeypatch.setattr(context.sessions, "prepare_context_fork", prepare_then_delete)
    with pytest.raises(KeyError, match=source.session_id):
        await context.forks.fork(source.session_id, pinned=True)
    assert (await context.sessions.list(limit=100)).total == 0
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        assert tuple(connection.execute("SELECT COUNT(*) FROM fork_retention_claims").fetchone()) == (0,)
        assert [row[0] for row in connection.execute(
            "SELECT state FROM session_creation_records WHERE purpose = 'context_fork'"
        )] == ["aborted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
@pytest.mark.parametrize("fault", ["completion_proof", "publish_parent_cas"])
async def test_unpublished_fork_proof_or_publish_failure_releases_exact_retention(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, mode: str, fault: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context)
    if fault == "completion_proof":
        afork = context.saver.afork

        async def corrupt_completion(**kwargs: object):
            result = await afork(**kwargs)
            return replace(result, source_snapshot_sha256="0" * 64)

        monkeypatch.setattr(context.saver, "afork", corrupt_completion)
    else:
        capture = context.saver._capture_fork_source_snapshot

        async def drift_parent_revision(**kwargs: object):
            snapshot = await capture(**kwargs)
            context.sessions.path_resolver.update_node_name(source.session_id, "发布前修改父节点")
            return snapshot

        monkeypatch.setattr(context.saver, "_capture_fork_source_snapshot", drift_parent_revision)
    failure = "committed fork journal.*snapshot 合同不一致" if fault == "completion_proof" else "父节点 revision 已漂移"
    with pytest.raises(RuntimeError, match=failure):
        await context.forks.fork(source.session_id, mode=mode, pinned=True, place_under_source=True)
    assert (await context.sessions.list(limit=100)).total == 1
    assert context.sessions.path_resolver.child_nodes(source.session_id) == []
    refs = _retention_refs(context, source.session_id)
    assert len(refs) == 1 and refs[0][4] == "released"
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        assert [row[0] for row in connection.execute("SELECT state FROM fork_retention_claims")] == ["released"]
        assert [row[0] for row in connection.execute(
            "SELECT state FROM session_creation_records WHERE purpose = 'context_fork'"
        )] == ["aborted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["target_committed", "committed"])
async def test_production_lifespan_recovers_detached_fork_after_source_deletion(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, crash_state: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context)
    target, record = await context.sessions.prepare_context_fork(
        idempotency_key=f"detached-recovery-{crash_state}", title="源已删除的待恢复副本",
        agent_id=source.current_agent_id, parent_session_id=None, context_source_session_id=source.session_id,
    )
    capture = context.saver._capture_fork_source_snapshot

    async def capture_then_delete(**kwargs: object):
        snapshot = await capture(**kwargs)
        await context.sessions.delete(source.session_id)
        return snapshot

    monkeypatch.setattr(context.saver, "_capture_fork_source_snapshot", capture_then_delete)
    completion = context.saver._storage.complete_fork_materialization

    def crash(**_kwargs: object) -> None:
        raise RuntimeError("detached target_committed 后中断")

    if crash_state == "target_committed":
        monkeypatch.setattr(context.saver._storage, "complete_fork_materialization", crash)
        with pytest.raises(RuntimeError, match="target_committed 后中断"):
            await context.saver.afork(source_session_id=source.session_id,
                                     target_session_id=target.session_id, creation_record=record)
        monkeypatch.setattr(context.saver._storage, "complete_fork_materialization", completion)
    else:
        await context.saver.afork(source_session_id=source.session_id,
                                 target_session_id=target.session_id, creation_record=record)
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        durable = restarted.session_service.path_resolver.catalog_store.get_creation_record(
            record.session_creation_idempotency_key
        )
        assert durable.state == "published"
        assert (await restarted.session_service.list(limit=100)).total == 1
        assert (await restarted.session_service.get(target.session_id)).context_source_session_id == source.session_id
        restored = await _get_tuple(_production_fork_context(restarted), target.session_id)
        assert restored is not None and len(restored.checkpoint["channel_values"]["messages"]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "history_prefix_fork", "full_rollout_copy"])
async def test_fork_of_imported_internal_lineage_uses_local_committed_proof(
    production_fork_container: AppContainer, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context)
    first = await context.forks.fork(source.session_id, mode=mode)
    first_checkpoint = await _get_tuple(context, first.session_id)
    assert first_checkpoint is not None
    with sqlite3.connect(_main_thread_node(context, first.session_id) / "rollout" / "index.sqlite") as connection:
        head = connection.execute(
            "SELECT head_checkpoint_id FROM branches WHERE branch_id = "
            "(SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = '')"
        ).fetchone()
    assert head == (first_checkpoint.checkpoint["id"],)
    assert first_checkpoint.checkpoint["id"] != "cp-retry-final"
    await context.sessions.delete(source.session_id)
    second = await context.forks.fork(first.session_id)
    assert second.context_source_session_id == first.session_id
    restored = await _get_tuple(context, second.session_id)
    assert restored is not None and len(restored.checkpoint["channel_values"]["messages"]) == 5
    with sqlite3.connect(_main_thread_node(context, second.session_id) / "rollout" / "index.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE execution_id NOT IN (SELECT execution_id FROM executions)"
        ).fetchone() == (0,)
