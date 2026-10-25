"""Session child 测试共享的创建、发布与控制存储装具。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.session_control_store import CollaborationMemberInput, SessionControlStore
from app.core.thread_creation import (
    canonical_json_text,
    compute_artifact_manifest_hash,
    compute_thread_creation_preimage_hash,
)
from app.schemas.internal_v2.team import TeamBoardDTO
from app.services.business.session_service import SessionService
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.support.canonical_id_at import thread_id_for_name_at

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
SUBAGENT_TYPE = "general-purpose"


def _control_for(
    session_service: SessionService, session_id: str
) -> SessionControlStore:
    return SessionControlStore(
        session_service.path_resolver.resolve_session_node(session_id)
        / "session-control.sqlite"
    )


def _create_child_thread(
    control: SessionControlStore,
    *,
    owner_session_id: str,
    coordinator_thread_id: str,
    key: str,
    created_at: datetime,
    delegation_id: str | None,
    title: str | None,
    coordinator_session_id: str | None = None,
    team_id: str | None = None,
    publish: bool = True,
    admission_state: str = "pending",
) -> str:
    """通过 owner 创建接口建立 child、member 与 execution intent。"""
    thread_id = thread_id_for_name_at(key, created_at)
    task_seed: dict[str, object] = {"description": title or "普通任务"}
    if team_id is not None:
        task_seed.update(
            trusted_context={
                "team_id": team_id,
                "coordinator_thread_id": coordinator_thread_id,
                "work_mode": "write",
            },
            untrusted_instructions="只完成团队任务。",
        )
    task_reference = (
        {
            "parent_session_id": owner_session_id,
            "parent_thread_id": coordinator_thread_id,
            "delegation_id": delegation_id,
        }
        if delegation_id is not None
        else None
    )
    capability_profile = dict(DEEP_AGENT_CAPABILITY_PROFILE)
    capability_profile["thread_role"] = "child"
    graph_binding = {
        "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
        "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
        "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
        "capability_profile_hash": compute_capability_profile_hash(
            capability_profile=capability_profile
        ),
    }
    session_metadata = {
        "graph_binding": graph_binding,
        "capability_profile": capability_profile,
        "task_seed": task_seed,
        "task_reference": task_reference,
    }
    artifact_manifest: dict[str, str] = {}
    record = control.create_or_get_thread_creation_record(
        idempotency_key=key,
        initial_state="running",
        preimage_hash=compute_thread_creation_preimage_hash(
            workspace_id=WORKSPACE_ID,
            session_id=owner_session_id,
            thread_id=thread_id,
            delegation_id=delegation_id,
            initial_state="running",
            session_metadata=session_metadata,
            artifact_manifest=artifact_manifest,
        ),
        graph_binding=canonical_json_text(graph_binding),
        capability_profile=canonical_json_text(capability_profile),
        created_at=created_at,
        thread_id=thread_id,
        delegation_id=delegation_id,
        task_seed=canonical_json_text(task_seed),
        task_reference=(
            canonical_json_text(task_reference) if task_reference is not None else None
        ),
        owner_session_id=owner_session_id,
        collaboration_member=(
            CollaborationMemberInput(
                coordinator_session_id=(coordinator_session_id or owner_session_id),
                coordinator_thread_id=coordinator_thread_id,
                role="delegated_subagent",
                subagent_type=SUBAGENT_TYPE,
                title=title or "普通任务",
            )
            if delegation_id is not None
            else None
        ),
    )
    if not publish:
        control.abort_thread_creation_record(key, "夹具在发布前中止")
        return record.child_thread_id

    manifest_text = canonical_json_text(artifact_manifest)
    control.freeze_thread_creation_artifact_manifest(
        key,
        artifact_manifest=manifest_text,
        artifact_manifest_hash=compute_artifact_manifest_hash(artifact_manifest),
    )
    (control.database_path.parent / record.final_relative_locator).mkdir(parents=True)
    control.publish_thread_creation_record(key, session_id=owner_session_id)
    if admission_state == "bound":
        claimed = control.claim_initial_execution_intent(
            key,
            claim_owner="unit-test",
            claim_generation=1,
        )
        control.mark_initial_execution_bound(
            key,
            execution_binding_id=claimed.execution_binding_id,
            job_id=claimed.job_id,
            claim_owner="unit-test",
            claim_generation=1,
        )
    else:
        assert admission_state == "pending"
    return record.child_thread_id


def _owner_thread_factory(
    session_service: SessionService,
) -> OwnerThreadCreationFactory:
    return OwnerThreadCreationFactory(
        sessions_root=session_service.path_resolver.sessions_root,
        workspace_id=session_service.workspace_id,
        path_resolver=session_service.path_resolver,
    )


async def _create_real_delegated_child(
    owner,
    *,
    session_id: str,
    coordinator_thread_id: str,
    idempotency_key: str,
    delegation_id: str,
    team_id: str,
):
    capability_profile = dict(DEEP_AGENT_CAPABILITY_PROFILE)
    capability_profile["thread_role"] = "child"
    graph_binding = {
        "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
        "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
        "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
        "capability_profile_hash": compute_capability_profile_hash(
            capability_profile=capability_profile
        ),
    }
    session_metadata = {
        "graph_binding": graph_binding,
        "capability_profile": capability_profile,
        "task_seed": {
            "description": "分析团队任务",
            "gateway_id": None,
            "request_id": None,
            "trusted_context": {
                "team_id": team_id,
                "coordinator_thread_id": coordinator_thread_id,
                "work_mode": "write",
            },
            "untrusted_instructions": "只完成团队任务。",
        },
        "task_reference": {
            "parent_session_id": session_id,
            "parent_thread_id": coordinator_thread_id,
            "delegation_id": delegation_id,
        },
    }
    return await owner.create(
        idempotency_key=idempotency_key,
        session_id=session_id,
        thread_id=None,
        delegation_id=delegation_id,
        initial_state="running",
        session_metadata=session_metadata,
        artifact_manifest={},
        collaboration_member={
            "role": "reviewer",
            "subagent_type": SUBAGENT_TYPE,
            "title": "团队审查 child",
        },
    )


class _TeamBoardStore:
    def __init__(self, board: TeamBoardDTO) -> None:
        self.board = board

    def get(self, session_id: str, team_id: str) -> TeamBoardDTO:
        assert (session_id, team_id) == (self.board.session_id, self.board.team_id)
        return self.board

    def list(self, session_id: str) -> list[TeamBoardDTO]:
        assert session_id == self.board.session_id
        return [self.board]

    def recent_events(self, session_id: str, team_id: str, *, limit: int = 20):
        assert (session_id, team_id) == (self.board.session_id, self.board.team_id)
        return []


def _team_board(session_id: str, coordinator_thread_id: str) -> TeamBoardDTO:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return TeamBoardDTO(
        team_id="team_consumer_projection",
        name="Consumer projection",
        session_id=session_id,
        coordinator_thread_id=coordinator_thread_id,
        version=1,
        created_at=now,
        updated_at=now,
    )


async def _start_real_child_creation_before_publish(
    owner,
    *,
    session_id: str,
    coordinator_thread_id: str,
    idempotency_key: str,
    delegation_id: str,
    team_id: str,
):
    publish_entered = asyncio.Event()
    resume_publish = asyncio.Event()
    original_publish = owner._publish_and_finalize

    async def wait_before_publish(*args, **kwargs):
        publish_entered.set()
        await resume_publish.wait()
        return await original_publish(*args, **kwargs)

    owner._publish_and_finalize = wait_before_publish
    task = asyncio.create_task(
        _create_real_delegated_child(
            owner,
            session_id=session_id,
            coordinator_thread_id=coordinator_thread_id,
            idempotency_key=idempotency_key,
            delegation_id=delegation_id,
            team_id=team_id,
        )
    )
    await asyncio.wait_for(publish_entered.wait(), timeout=10)
    record = owner.control_store.get_thread_creation_record(idempotency_key)
    return task, resume_publish, record
