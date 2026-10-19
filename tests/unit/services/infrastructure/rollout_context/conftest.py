"""CSM 合同共用真实 Saver prepare/seal fixture；applied 只由 assembly 事务写入。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.seal_binding import ContextSealBinding
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    CommittedContextSourceBatch,
    ContextSourceManager,
)


@dataclass
class SourceSealCase:
    saver: RolloutCheckpointSaver
    owner: ContextSourceOwnerKey
    accepted: dict[str, object]
    wire_contract: Callable[[str, str], dict[str, object]]
    activation_contract: Callable[..., dict[str, object]]
    request_number: int = 0

    def manager(
        self, *,
        lifecycle_event_sink: Callable[[ContextSourceEvent], None] | None = None,
        mutation_intent_port: object | None = None,
    ) -> ContextSourceManager:
        return ContextSourceManager(
            owner=self.owner, control_state_port=self.saver,
            lifecycle_event_sink=lifecycle_event_sink,
            mutation_intent_port=mutation_intent_port,
        )

    def seal(
        self, manager: ContextSourceManager, *,
        tool_set: ToolSetSnapshot | None = None,
    ) -> tuple[CommittedContextSourceBatch, ContextSealBinding]:
        batch = manager.prepare_pending()
        assert batch is not None
        self.request_number += 1
        session, thread = self.owner.session_id, self.owner.thread_id
        execution = str(self.accepted["initial_execution_id"])
        selected = tool_set if tool_set is not None else ToolSetSnapshot(
            selection_revision=sha256_jcs({}), tools=(),
        )
        admission = self.saver.execution_admission(
            session, thread_id=thread, execution_id=execution,
        )
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session, thread), langgraph_thread_id=thread,
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            prepared = self.saver.prepare_context_for_provider(
                session, thread, turn_id=str(self.accepted["turn_id"]),
                execution_id=execution,
                plan_creation_idempotency_key=f"source-create:{self.request_number}",
                seal_idempotency_key=f"source-seal:{self.request_number}",
                context_source_manager=manager, tool_set_supplier=lambda: selected,
                **self.wire_contract("source-contract", "chat_completions"),
                **self.activation_contract(
                    self.saver, plan_creation_idempotency_key=f"source-create:{self.request_number}",
                ),
            )
        snapshot = self.saver.get_context_assembly(
            session, thread, assembly_id=str(prepared["assembly_id"]),
        )
        assert isinstance(snapshot.seal_binding, ContextSealBinding)
        receipt = manager.acknowledge_sealed_pending(batch, snapshot.seal_binding)
        return receipt, snapshot.seal_binding


@pytest.fixture
def source_seal_case(
    tmp_path: Path, session_bundle_factory: Callable[[Path, str], Path],
    frozen_provider_wire: Callable[[str, str], dict[str, object]],
    frozen_provider_activation: Callable[..., dict[str, object]],
) -> SourceSealCase:
    sessions = tmp_path / ".boxteam" / "sessions"
    session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session, thread_id=saver._storage.storage.main_thread_id(session), accepted_ingress_id="source-ingress",
        acceptance_idempotency_key="source-acceptance",
        job_id="source-job", admission_idempotency_key="source-admission",
        payload="真实 source seal 合同",
    )
    return SourceSealCase(
        saver, ContextSourceOwnerKey(session, saver._storage.storage.main_thread_id(session)),
        accepted, frozen_provider_wire, frozen_provider_activation,
    )
