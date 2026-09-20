from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugConfigurationBreakpointDTO,
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_fork import (
    NodeDebugTargetValidationError,
    build_workspace_fork_config,
)
from app.services.infrastructure.node_debug_thread_owner import MAIN_THREAD_ID
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
from tests.integration.backend.sessions.itemized_migration_helpers import (
    prepare_migration_workspace,
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
    workspace = prepare_migration_workspace(request)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    sessions_dir = workspace / ".boxteam" / "sessions"
    config_service = ConfigService(workspace_root=workspace)
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
        fork_relationship_checker=saver,
    )
    return ForkIntegrationContext(
        workspace=workspace,
        sessions=sessions,
        saver=saver,
        forks=SessionContextForkService(session_service=sessions, checkpointer=saver),
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
    first_messages = [
        HumanMessage(
            content="问题一",
            id="u1",
            response_metadata={"turn_id": "turn-1"},
        )
    ]
    first_config = await context.saver.aput(
        build_checkpoint_config(source.session_id),
        _checkpoint("cp1", first_messages),
        {"source": "stub", "step": 1, "parents": {}},
        {"messages": "1", "counter": "1"},
    )
    second_messages = [*first_messages, AIMessage(content="回答一", id="a1")]
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
        turn_id="turn-1",
        final_message_id="a1",
    )
    await context.saver.aput_writes(
        second_config,
        [("scratchpad", {"pending": True})],
        "task-pending",
        "parent/child",
    )
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
            MAIN_THREAD_ID,
        )
    context.saver._node_debug_store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=source_session_id,
            thread_id=MAIN_THREAD_ID,
            active_configuration_id=active_id,
            actions=[
                NodeDebugActionRecordDTO(
                    action_id="act_source_only",
                    session_id=source_session_id,
                    thread_id=MAIN_THREAD_ID,
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
            thread_id=MAIN_THREAD_ID,
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


def _fork_origins(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT child_session_id, source_session_id, source_checkpoint_id, "
            "source_view_id, fork_mode, relationship FROM fork_origins"
        ).fetchall()


def _retention_refs(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT reference_kind, reference_id, target_view_id, "
            "owner_session_id, status FROM retention_refs"
        ).fetchall()


def _pending_write_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT task_id, task_path, write_index, channel, status "
            "FROM pending_writes ORDER BY task_path, write_index"
        ).fetchall()


def _fork_materialization_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT materialization_id, fork_id, status, copied_message_count "
            "FROM fork_materializations"
        ).fetchall()


def _identity_mapping_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT fork_id, source_session_id, target_session_id, entity_type, "
            "source_local_id, target_local_id, source_offset, target_offset, lineage_json "
            "FROM fork_identity_mappings ORDER BY entity_type, source_local_id"
        ).fetchall()


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
    context_manifest = store.read_manifest(
        context_child.session_id, MAIN_THREAD_ID
    )
    assert context_manifest is not None
    assert context_manifest.active_configuration_id is None
    assert context_manifest.actions == []
    context_configurations = store.list_configurations(
        context_child.session_id, MAIN_THREAD_ID
    )
    assert [item.name for item in context_configurations] == ["活动方案"]
    assert context_configurations[0].configuration_id != active_id

    assert store.read_manifest(prefix_child.session_id, MAIN_THREAD_ID) is None
    assert store.list_configurations(prefix_child.session_id, MAIN_THREAD_ID) == []

    full_manifest = store.read_manifest(full_child.session_id, MAIN_THREAD_ID)
    assert full_manifest is not None
    assert full_manifest.active_configuration_id is None
    assert full_manifest.actions == []
    full_configurations = store.list_configurations(
        full_child.session_id, MAIN_THREAD_ID
    )
    assert {item.name for item in full_configurations} == {"活动方案", "保存方案"}
    assert {item.configuration_id for item in full_configurations}.isdisjoint(
        {active_id, saved_id}
    )

    for child, expected_count in (
        (context_child, 1),
        (full_child, 2),
    ):
        rollout_root = fork_context.sessions.path_resolver.resolve_session_node(
            child.session_id
        )
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
        assert store.read_launch_claim(child.session_id, MAIN_THREAD_ID) is None
    source_claim = store.read_launch_claim(source.session_id, MAIN_THREAD_ID)
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
            target.session_id, MAIN_THREAD_ID
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
                target.session_id, MAIN_THREAD_ID
            )
            is None
        )
        with restarted._storage._connect(
            target.session_id, "", read_only=True
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
    assert not (target_node / "debug" / "node").exists()
    with sqlite3.connect(target_node / "rollout" / "index.sqlite") as connection:
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
            target.session_id, MAIN_THREAD_ID
        )
        assert manifest is not None
        assert manifest.session_id == target.session_id
        assert manifest.thread_id == MAIN_THREAD_ID
        assert manifest.active_configuration_id is None
        assert manifest.actions == []
        assert not staging.exists()
        with restarted._storage._connect(
            target.session_id, "", read_only=True
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
        target_thread_id=source.session_id,
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

    context_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(context_child.session_id)
    )
    prefix_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(prefix_child.session_id)
    )
    full_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(full_child.session_id)
    )
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
    prefix_pending = [
        item
        for item in [
            item
            async for item in fork_context.saver.alist(
                build_checkpoint_config(prefix_child.session_id)
            )
        ]
        if item.checkpoint["id"] != ""
    ]
    assert any(
        item.checkpoint["pending_sends"] == [{"node": "pending-node", "attempt": 2}]
        and item.pending_writes == [("task-pending", "scratchpad", {"pending": True})]
        for item in prefix_pending
    )
    assert _pending_write_rows(fork_context, prefix_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    full_pending = await fork_context.saver.aget_tuple(
        build_checkpoint_config(full_child.session_id, checkpoint_id="cp2")
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
        child_root = fork_context.sessions.path_resolver.resolve_session_node(
            child.session_id
        )
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
    child_root = (
        fork_context.sessions.path_resolver.resolve_session_node(
            context_child.session_id
        )
        / "rollout"
    )
    assert (child_root / "rollout.jsonl").is_file()
    assert (child_root / "index.sqlite").is_file()
    assert not list(child_root.glob("segment-*.jsonl"))

    await fork_context.sessions.delete(source.session_id)
    for child in (context_child, prefix_child, full_child):
        assert (
            await fork_context.saver.aget_tuple(
                build_checkpoint_config(child.session_id)
            )
            is not None
        )


@pytest.mark.asyncio
async def test_full_copy_localizes_detail_and_source_overlay_lineage(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    source_overlay = SimpleNamespace(
        overlay_id="overlay-source-policy",
        session_id=source.session_id,
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
        base_content=overlay_body,
    )
    detail = fork_context.saver._detail_store.write(
        session_id=source.session_id,
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

    source_overlays = fork_context.saver.list_source_overlays(source.session_id)
    target_overlays = fork_context.saver.list_source_overlays(child.session_id)
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
        plan_id="plan-source-overlay",
    )
    target_plan = fork_context.saver.compose_committed_context_plan(
        child.session_id,
        plan_id="plan-target-overlay",
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
    target_detail_ref.require_owner(child.session_id)
    target_detail = fork_context.saver._storage.get_context_plan_detail(
        child.session_id,
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
        detail_ref=target_detail_ref,
    )["detail"] == {"blocks": [{"type": "text", "text": "source detail"}]}

    source_root = fork_context.sessions.path_resolver.resolve_session_node(
        source.session_id
    )
    target_root = fork_context.sessions.path_resolver.resolve_session_node(
        child.session_id
    )
    assert (source_root / detail.relative_path).is_file()
    assert not (target_root / detail.relative_path).exists()
    assert (target_root / target_detail["relative_path"]).is_file()


@pytest.mark.asyncio
async def test_history_replay_reuses_turn_root_without_execution(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    source_root = fork_context.sessions.path_resolver.resolve_session_node(
        source.session_id
    )
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        before_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        turn_id, turn_ordinal, root_item_id, source_branch_id = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()

    latest = await fork_context.saver.aget_tuple(
        build_checkpoint_config(source.session_id)
    )
    assert latest is not None
    replay_config = fork_context.saver.history_replay(
        latest.config,
        turn_id="turn-1",
        anchor_mode="inclusive",
    )
    assert replay_config["configurable"]["thread_id"] == source.session_id
    assert replay_config["configurable"]["checkpoint_id"] == "cp3"

    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        after_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        history_view = connection.execute(
            "SELECT v.view_id, v.view_kind, cvt.turn_id, cvt.root_input_item_id, "
            "cvt.logical_turn_ordinal FROM context_views AS v "
            "JOIN context_view_turns AS cvt ON cvt.view_id = v.view_id "
            "JOIN branches AS b ON b.head_view_id = v.view_id "
            "WHERE b.status = 'active'"
        ).fetchone()
        copied_turn = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()

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
    restored = await restarted.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一", "问题二"]

    source_root = fork_context.sessions.path_resolver.resolve_session_node(
        source.session_id
    )
    child_root = fork_context.sessions.path_resolver.resolve_session_node(
        child.session_id
    )
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
    materialization_id, _fork_id = (
        fork_context.saver._storage.begin_fork_materialization(
            target_session_id=target.session_id,
            source_session_id=source.session_id,
            source_checkpoint_id=None,
            source_view_id=None,
            fork_mode="context_fork",
            relationship="detached",
        )
    )

    # 新的 RolloutStorage 实例模拟进程重启：内存中的 active 标记不存在，
    # initialize 必须按 journal 清理目标，而不是把半成品当成空 checkpoint。
    restarted_storage = RolloutStorage(
        fork_context.sessions.path_resolver.sessions_root
    )
    restarted_storage.initialize(target.session_id)
    target_root = fork_context.sessions.path_resolver.resolve_session_node(
        target.session_id
    )
    with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT status FROM fork_materializations WHERE materialization_id = ?",
            (materialization_id,),
        ).fetchone() == ("aborted",)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert (target_root / "rollout" / "rollout.jsonl").read_bytes() == b""


@pytest.mark.asyncio
async def test_context_fork_anchor_selects_completed_target_turn(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id)
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

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
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][2] == "cp2"
    child_root = fork_context.sessions.path_resolver.resolve_session_node(
        child.session_id
    )
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
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

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
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

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
        payload="取消后只能新 Turn 回放",
        turn_id="turn-cancelled-source",
        root_item_id="item-cancelled-source",
        initial_execution_id="execution-cancelled-source",
    )
    source_root = fork_context.sessions.path_resolver.resolve_session_node(
        source.session_id
    )
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
    child_root = fork_context.sessions.path_resolver.resolve_session_node(
        child.session_id
    )
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
        fork_context.saver.resume_turn(child.session_id, turn_id=target_turn_id)
    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.dispatch_replay(child.session_id, turn_id=target_turn_id)

    replayed = fork_context.saver.replay_as_new_turn(
        child.session_id,
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
