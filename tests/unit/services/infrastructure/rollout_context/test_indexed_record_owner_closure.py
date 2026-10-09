from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadGraphBindingRecord
from app.core.session_creation import SessionCreationResult, SessionCreationService
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationResult, ThreadCreationService
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore

WORKSPACE_ID = "ws-indexed-record-owner"


@pytest.fixture
def indexed_threads(
    tmp_path: Path,
) -> tuple[RolloutContextStore, str, str, str, Path]:
    boxteam_root = tmp_path / ".boxteam"
    sessions_root = boxteam_root / "sessions"
    sessions_root.mkdir(parents=True)
    catalog = SessionCatalogStore(
        boxteam_root / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    session_creator = SessionCreationService(
        store=catalog,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
    )

    async def create_session_and_thread() -> tuple[
        SessionCreationResult,
        ThreadCreationResult,
        SessionControlStore,
    ]:
        record = await session_creator.prepare(
            idempotency_key=f"indexed-owner-session-{create_uuid_hex()}",
            title="indexed record owner",
            parent_node_id=None,
            graph_binding=ThreadGraphBindingRecord(
                graph_id=DEEP_AGENT_GRAPH_BINDING.graph_id,
                graph_revision=DEEP_AGENT_GRAPH_BINDING.graph_revision,
                graph_schema_hash=DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
                capability_profile_hash=DEEP_AGENT_GRAPH_BINDING.capability_profile_hash,
            ),
            session_metadata={
                "kind": "normal",
                "delegation": None,
                "generation_origin": None,
                "current_agent_id": "default",
                "current_provider_id": "default_provider",
                "context_source_session_id": None,
            },
        )
        session = await session_creator.publish_prepared(record)
        session_dir = sessions_root / session.storage_relative_locator.removeprefix(
            "sessions/"
        )
        control = SessionControlStore(session_dir / CONTROL_DATABASE_NAME)
        child_creator = ThreadCreationService(
            store=catalog,
            control_store=control,
            sessions_root=sessions_root,
            workspace_id=WORKSPACE_ID,
            compute_capability_profile_hash=compute_capability_profile_hash,
        )
        child = await child_creator.create(
            idempotency_key=f"indexed-owner-child-{create_uuid_hex()}",
            session_id=session.session_id,
            thread_id=None,
            delegation_id=None,
            initial_state="running",
            session_metadata={
                "graph_binding": {
                    "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
                    "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
                    "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
                    "capability_profile_hash": (
                        DEEP_AGENT_GRAPH_BINDING.capability_profile_hash
                    ),
                },
                "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
                "task_seed": {"task": "read indexed messages"},
                "task_reference": {"source": "unit test"},
            },
            artifact_manifest={"notes/plan.md": "read indexed messages\n"},
        )
        return session, child, control

    session, child, control = asyncio.run(create_session_and_thread())
    storage = RolloutContextStore(
        sessions_root,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    main_thread_id = storage.storage.main_thread_id(session.session_id)
    child_thread_id = child.child_thread_id
    assert storage.storage.root(session.session_id, thread_id=main_thread_id) != storage.storage.root(
        session.session_id,
        thread_id=child_thread_id,
    )
    yield storage, session.session_id, main_thread_id, child_thread_id, sessions_root
    control.close()
    catalog.close()


def _write_indexed_turn(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    label: str,
) -> str:
    turn_id = f"turn-indexed-owner-{label}"
    execution_id = f"execution-indexed-owner-{label}"
    user_message_id = f"indexed-user-{label}"
    assistant_message_id = f"indexed-assistant-{label}"
    saver.accept_turn(
        session_id,
        thread_id=thread_id,
        accepted_ingress_id=f"ingress-indexed-owner-{label}",
        acceptance_idempotency_key=f"acceptance-indexed-owner-{label}",
        job_id=f"job-indexed-owner-{label}",
        admission_idempotency_key=f"admission-indexed-owner-{label}",
        payload=f"question {label}",
        payload_kind="text",
        turn_id=turn_id,
        root_item_id=f"item-{user_message_id}",
        initial_execution_id=execution_id,
    )
    admission = saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=execution_id,
    )
    checkpoint = empty_checkpoint()
    checkpoint["id"] = f"checkpoint-indexed-owner-{label}"
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content=f"question {label}", id=user_message_id),
            AIMessage(content=f"answer {label}", id=assistant_message_id),
        ]
    }
    checkpoint["channel_versions"] = {"messages": checkpoint["id"]}
    checkpoint["updated_channels"] = ["messages"]
    owner = MutationIntentOwner(session_id=session_id, thread_id=thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        saver.put(
            build_checkpoint_config(thread_id),
            checkpoint,
            {"source": "indexed-record-owner-test"},
            {"messages": checkpoint["id"]},
        )
    return turn_id


def test_projection_reads_use_snapshot_session_thread_pair(
    indexed_threads: tuple[RolloutContextStore, str, str, str, Path],
) -> None:
    storage, session_id, main_thread_id, child_thread_id, sessions_root = (
        indexed_threads
    )
    saver = RolloutCheckpointSaver(sessions_root, storage=storage)
    reader = RolloutContextReader(storage)
    for label, thread_id in (
        ("main", main_thread_id),
        ("child", child_thread_id),
    ):
        turn_id = _write_indexed_turn(
            saver,
            session_id=session_id,
            thread_id=thread_id,
            label=label,
        )
        assert storage.storage.root(session_id, thread_id=thread_id).is_dir()
        with reader.open_snapshot(session_id, thread_id) as snapshot:
            message_rows = snapshot.connection.execute(
                "SELECT message_sequence, message_id FROM messages "
                "WHERE turn_id = ? ORDER BY message_sequence",
                (turn_id,),
            ).fetchall()
            assert [str(row[1]) for row in message_rows] == [
                f"indexed-user-{label}",
                f"indexed-assistant-{label}",
            ]
            through_sequence = int(message_rows[-1][0])
            chain = reader.resolve_chain(snapshot, through_sequence)
            branch_id = snapshot.manifest.active_branch_id
            records = reader.read_projection_records(
                snapshot,
                chain=chain,
                through_sequence=through_sequence,
                branch_id=branch_id,
                turn_id=turn_id,
            )
            batch_records = reader.read_projection_records_batch(
                snapshot,
                turn_ids=(turn_id,),
                chain=chain,
            )

            expected_message_ids = [
                f"indexed-user-{label}",
                f"indexed-assistant-{label}",
            ]
            assert [record["message_id"] for record in records] == (
                expected_message_ids
            )
            assert [
                record["message_id"] for record in batch_records[turn_id]
            ] == expected_message_ids

            with pytest.raises(ValueError, match="snapshot owner"):
                storage.checkpoints.read_indexed_records(
                    f"ses_{create_uuid_hex()}",
                    thread_id,
                    "",
                    through_sequence=through_sequence,
                    turn_id=turn_id,
                    snapshot=snapshot,
                )
            with pytest.raises(ValueError, match="snapshot owner"):
                storage.checkpoints.read_indexed_records(
                    session_id,
                    thread_id,
                    "different-namespace",
                    through_sequence=through_sequence,
                    turn_id=turn_id,
                    snapshot=snapshot,
                )

        direct_records = storage.checkpoints.read_indexed_records(
            session_id,
            thread_id,
            "",
            through_sequence=through_sequence,
            sequence_ranges=chain.ranges,
            branch_id=branch_id,
            turn_id=turn_id,
        )
        assert [record["message_id"] for record in direct_records] == (
            expected_message_ids
        )
