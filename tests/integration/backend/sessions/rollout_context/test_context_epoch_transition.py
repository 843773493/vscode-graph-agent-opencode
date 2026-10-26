"""真实 checkpoint/rewind/compaction → published source → prepare/seal 的同 owner 合同。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.skill_runtime import WorkspaceSkillsMiddleware
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.lifecycle import LifetimeScope
from app.core.model_request_context import SummaryCandidateScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    ProviderProfileChangeRequiresRebuildError,
)
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
    RebuildContextEpochIntent,
)
from app.domain.itemized.plans.contributions import SourceBinding
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceSnapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
    ContextSourceRebuildState,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_source_groups,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)

RULES_URI = "boxteam://workspace/agent-spec/epoch-test"
SKILL_URI = "boxteam://workspace/test/resources/skills/debugging/SKILL.md"
SKILL_FRONTMATTER = "---\nname: debugging\ndescription: 调试规则\n---\n"


@dataclass
class PublishedResourceOwner:
    """注入的来源 owner 只发布 immutable 内存快照，不持有文件路径。"""

    snapshots: dict[str, WorkspaceFileResourceSnapshot] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def snapshot(self, uri: str) -> WorkspaceFileResourceSnapshot:
        self.reads.append(uri)
        return self.snapshots[uri]

    def publish(self, uri: str, revision: str, content: str, *, available: bool = True) -> None:
        self.snapshots[uri] = WorkspaceFileResourceSnapshot(uri, revision, content, available)


@dataclass
class EpochCase:
    saver: RolloutCheckpointSaver
    owner: ContextSourceOwnerKey
    connection: sqlite3.Connection
    accepted: dict[str, object]
    wire: Callable[[str, str], dict[str, object]]
    activation: Callable[..., dict[str, object]]
    publisher: PublishedResourceOwner
    descriptors: tuple[ContextSourceDescriptor, ...]
    stack: ExitStack
    manager: ContextSourceManager = field(init=False)
    user: HumanMessage = field(init=False)
    first: dict[str, object] = field(init=False)
    config: RunnableConfig = field(init=False)

    def bind_manager(self) -> None:
        self.manager = ContextSourceManager(owner=self.owner, control_state_port=self.saver)
        for descriptor in self.descriptors:
            self.manager.register(descriptor, tracking_status="tracked")
        reactor = ContextSourceReactor(
            sources=self.publisher, context_sources=self.manager,
            lifetime_scope=LifetimeScope("epoch-source-snapshots"), reactor_id="epoch-source-snapshots",
        )
        WorkspaceSkillsMiddleware(
            catalog=None, context_source_manager=self.manager, context_source_reactor=reactor,
        )

    @contextmanager
    def scope(self) -> Iterator[None]:
        admission = self.saver.execution_admission(
            self.owner.session_id, thread_id=self.owner.thread_id,
            execution_id=str(self.accepted["initial_execution_id"]),
        )
        with (
            session_thread_owner_scope(
                MutationIntentOwner(self.owner.session_id, self.owner.thread_id),
                langgraph_thread_id=self.owner.thread_id,
            ),
            execution_admission_scope(admission, langgraph_thread_id=self.owner.thread_id),
        ):
            yield

    def prepare(
        self, key: str, *, model: str = "epoch-provider", supplier=None,
        request_messages: tuple[BaseMessage, ...] = (),
        summary_candidate: SummaryCandidateScope | None = None,
    ) -> dict[str, object]:
        selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
        with self.scope():
            return self.saver.prepare_context_for_provider(
                self.owner.session_id, self.owner.thread_id,
                turn_id=str(self.accepted["turn_id"]), execution_id=str(self.accepted["initial_execution_id"]),
                plan_creation_idempotency_key="epoch-create:" + key, seal_idempotency_key="epoch-seal:" + key,
                context_source_manager=self.manager, tool_set_supplier=supplier or (lambda: selected),
                request_messages=request_messages, summary_candidate=summary_candidate,
                **self.wire(model, "chat_completions"),
                **self.activation(
                    self.saver, plan_creation_idempotency_key="epoch-create:" + key,
                ),
            )

    def checkpoint(self, key: str, *, compaction: bool = False, messages: list[BaseMessage] | None = None) -> None:
        checkpoint = empty_checkpoint()
        checkpoint["id"] = key
        checkpoint["channel_values"] = {"messages": [self.user] if messages is None else messages}
        if compaction:
            checkpoint["channel_values"]["_summarization_event"] = {
                "cutoff_index": len(checkpoint["channel_values"]["messages"]),
                "cutoff_message_id": checkpoint["channel_values"]["messages"][-1].id,
                "summary_message": HumanMessage(content="已完成的对话摘要", id=f"summary-{key}"),
            }
        checkpoint["channel_versions"] = {name: key for name in checkpoint["channel_values"]}
        checkpoint["updated_channels"] = list(checkpoint["channel_values"])
        with self.scope():
            self.config = self.saver.put(
                self.config, checkpoint,
                {"source": "compaction" if compaction else "loop", "step": 1},
                checkpoint["channel_versions"],
            )

    def rewind(self, checkpoint: str, *, anchor: str | None = None, anchor_mode: str = "inclusive") -> None:
        with self.scope():
            self.config = self.saver.rewind(
                self.config, checkpoint_id=checkpoint, source_anchor=anchor, anchor_mode=anchor_mode,
            )

    def restart(self) -> None:
        self.saver = self.stack.enter_context(RolloutCheckpointSaver(
            self.saver._storage.storage.sessions_dir,
        ))
        self.bind_manager()

    def snapshot(self, prepared: dict[str, object]):
        return self.saver.get_context_assembly(
            self.owner.session_id, self.owner.thread_id, assembly_id=str(prepared["assembly_id"]),
        )

    def source_rows(self):
        return self.connection.execute(
            "SELECT source_id, tracking_status, latest_revision, latest_visible_committed_revision, state_revision "
            ", binding_revision, source_kind, name, created_at, updated_at "
            "FROM context_source_control_states ORDER BY source_id"
        ).fetchall()

    def selected_rows(self):
        return tuple(self.connection.execute(query).fetchall() for query in (
            "SELECT * FROM context_assemblies ORDER BY assembly_id",
            "SELECT * FROM assembly_item_refs ORDER BY assembly_id, ref_ordinal",
            "SELECT * FROM context_assembly_contributions ORDER BY assembly_id, contribution_ordinal",
            "SELECT * FROM context_assembly_selections ORDER BY assembly_id, plan_ordinal",
            ("SELECT detail.* FROM context_plan_details AS detail "
             "JOIN context_assemblies AS assembly ON assembly.assembly_id = detail.assembly_id "
             "ORDER BY detail.detail_ref"),
        ))

    def checkpoint_versions(self):
        return (
            self.connection.execute(
                "SELECT * FROM checkpoint_context_source_snapshots ORDER BY checkpoint_id"
            ).fetchall(),
            self.connection.execute(
                "SELECT * FROM checkpoint_context_source_states ORDER BY checkpoint_id, source_id"
            ).fetchall(),
        )

    def dispatch_rows(self):
        return (
            self.connection.execute(
                "SELECT model_call_id, assembly_id, dispatch_state FROM model_calls ORDER BY model_call_id"
            ).fetchall(),
            tuple((key, tuple(item["assembly_id"] for item in queue))
                  for key, queue in sorted(self.saver._prepared_dispatches.items())),
        )

    def pending_rows(self):
        return self.connection.execute(
            "SELECT transition_id, target_view_id, reason, status, consumed_assembly_id "
            "FROM context_epoch_transitions ORDER BY rowid"
        ).fetchall()

    def active_view(self):
        return self.connection.execute(
            "SELECT head_view_id FROM branches WHERE status='active'"
        ).fetchone()[0]

    def advance(self):
        self.publisher.publish(RULES_URI, "rules:2", "基础规则第二版")
        self.publisher.publish(SKILL_URI, "skill:2", SKILL_FRONTMATTER + "v2\n")
        self.manager.observe("workspace:rules", "基础规则第二版", revision="rules:2")
        self.manager.observe("skill:debugging", "v2\n", revision="skill:2")
        return self.prepare("second")

    def load_snapshot(self, *, root_eligible: bool = False) -> SkillCatalogBinding:
        uri = "boxteam://workspace/test/resources/skills/one-shot/SKILL.md"
        self.manager.register(ContextSourceDescriptor(
            source_id="skill:one-shot", source_kind="skill", name="one-shot",
            description="仅本次加载", internal_locator="/绝不读取/one-shot/SKILL.md",
            resource_uri=uri,
            root_placement="root_eligible" if root_eligible else "tail_only",
        ))
        binding = SkillCatalogBinding(
            "one-shot", "resource:one-shot", "entry:one-shot", uri,
            "snapshot:1", "仅一次的冻结正文\n",
        )
        self.manager.install_skill_activation_snapshot(
            SkillCatalogActivationSnapshot("catalog:snapshot", {"one-shot": binding})
        )
        assert self.manager.load_skill("one-shot", mode="snapshot").tracked is False
        return binding


@pytest.fixture
def epoch_db(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        accepted = saver.accept_turn(
            session, thread_id=saver._storage.storage.main_thread_id(session), accepted_ingress_id="epoch-ingress", acceptance_idempotency_key="epoch-acceptance",
            job_id="epoch-job", admission_idempotency_key="epoch-admission",
            payload="真实用户输入", payload_kind="text", root_item_id="item-epoch-ingress",
        )
        with saver._storage.storage._connect(
            saver._storage.storage.main_thread_id(session), "", session_id=session,
        ) as owner_connection:
            database_path = owner_connection.execute("PRAGMA database_list").fetchone()[2]
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield saver, session, connection, accepted
        finally:
            connection.close()


@pytest.fixture
def rebuild_reads(monkeypatch):
    reads: list[ContextSourceRebuildState | None] = []
    load = RolloutCheckpointSaver.load_context_source_rebuild_state

    def observe(saver, owner, *, checkpoint_ns=""):
        result = load(saver, owner, checkpoint_ns=checkpoint_ns)
        reads.append(result)
        return result

    monkeypatch.setattr(RolloutCheckpointSaver, "load_context_source_rebuild_state", observe)
    return reads


@pytest.fixture
def epoch_case(epoch_db, frozen_provider_wire, frozen_provider_activation, request):
    saver, session, connection, accepted = epoch_db
    thread = saver._storage.storage.main_thread_id(session)
    publisher = PublishedResourceOwner()
    publisher.publish(RULES_URI, "rules:1", "基础规则第一版")
    publisher.publish(SKILL_URI, "skill:1", SKILL_FRONTMATTER + "v1\n")
    descriptors = (
        ContextSourceDescriptor(
            source_id="workspace:rules", source_kind="workspace_rules", name="RULES.md",
            description="受信工作区规则", internal_locator="/绝不读取/RULES.md",
            resource_uri=RULES_URI, root_placement="root_eligible",
        ),
        ContextSourceDescriptor(
            source_id="skill:debugging", source_kind="skill", name="debugging",
            description="调试规则", internal_locator="/绝不读取/SKILL.md", resource_uri=SKILL_URI,
        ),
    ) if getattr(request, "param", True) else ()
    with ExitStack() as stack:
        case = EpochCase(saver, ContextSourceOwnerKey(session, thread), connection,
                         accepted, frozen_provider_wire, frozen_provider_activation,
                         publisher, descriptors, stack)
        case.config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        case.bind_manager()
        if descriptors:
            case.manager.observe("workspace:rules", "基础规则第一版", revision="rules:1")
            case.manager.observe("skill:debugging", "v1\n", revision="skill:1")
        case.first = case.prepare("first")
        case.user = next(message for message in case.first["messages"] if isinstance(message, HumanMessage))
        assert "item-" + case.user.id == accepted["root_input_item_id"]
        case.checkpoint("checkpoint-first")
        yield case


def test_tracking_contract_survives_provider_retry_and_cold_append(epoch_case):
    case = epoch_case
    case.load_snapshot()
    prepared = case.prepare("tracking-contract")
    original = case.snapshot(prepared)
    refs = [ref for frame in original.seal_binding.prefix.state.frames for ref in frame.item_refs]
    assert original.seal_binding.format_version == 4
    assert {ref.source_tracking_at_seal for ref in refs if ref.ref_type == "source_item"} == {"tracked", "untracked"}
    assert any(ref.ref_type == "canonical_item" for ref in refs)
    assert all(ref.source_tracking_at_seal is None for ref in refs if ref.ref_type == "canonical_item")
    sealed_bytes = canonical_json_bytes(original.to_dict())
    rows = case.selected_rows()
    case.restart()
    assert case.prepare("tracking-contract")["assembly_id"] == original.assembly_id
    assert case.selected_rows() == rows
    assert canonical_json_bytes(case.snapshot(prepared).to_dict()) == sealed_bytes
    following = case.snapshot(case.prepare("tracking-following"))
    assert following.seal_binding.format_version == 4
    assert following.seal_binding.prefix.state.epoch == original.seal_binding.prefix.state.epoch
    old_frames = original.seal_binding.prefix.state.frames
    assert following.seal_binding.prefix.state.frames[:len(old_frames)] == old_frames
    assert following.seal_binding.prefix.parent.assembly_id == original.assembly_id
    assert canonical_json_bytes(case.snapshot(prepared).to_dict()) == sealed_bytes


@pytest.mark.parametrize("epoch_case", (False, True), indirect=True, ids=("no-sources", "tracked-sources"))
def test_prepare_reuses_empty_rebuild_read_only_within_each_request(epoch_case, rebuild_reads):
    case = epoch_case
    before = case.source_rows()
    assert len(before) == len(case.descriptors)
    rebuild_reads.clear()
    prepared = case.prepare("fresh-read:first")
    assert rebuild_reads == [None]
    next_prepared = case.prepare("fresh-read:second")
    assert rebuild_reads == [None, None]
    assert next_prepared["assembly_id"] != prepared["assembly_id"]
    assert case.snapshot(next_prepared).seal_binding.prefix.state.epoch.ordinal == 1
    assert case.source_rows() == before
    assert case.pending_rows() == []


def test_prepare_pending_reuses_empty_read_but_default_freeze_reads_again(epoch_case, rebuild_reads):
    case = epoch_case
    case.manager.observe("workspace:rules", "待发送第三版", revision="rules:3")
    before = case.source_rows()
    rebuild_reads.clear()
    pending = case.manager.prepare_pending()
    assert rebuild_reads == [None]
    assert pending is not None and tuple(delta.revision for delta in pending.deltas) == ("rules:3",)
    assert case.manager.prepare_sources() == pending
    assert rebuild_reads == [None, None]
    assert case.source_rows() == before


@pytest.mark.parametrize("minimal_system", (False, True))
@pytest.mark.parametrize("with_transition", (False, True))
def test_summary_reads_rebuild_once_without_applying_sources_or_epoch(
    epoch_case, rebuild_reads, minimal_system, with_transition,
):
    case = epoch_case
    if with_transition:
        case.advance()
        case.rewind("checkpoint-first")
    else:
        case.manager.observe("workspace:rules", "待摘要的第二版", revision="rules:2")
    before, pending, view = case.source_rows(), case.pending_rows(), case.active_view()
    items = case.saver._storage.storage.read_items(case.owner.session_id, case.owner.thread_id)
    messages = tuple(project_summary_source(group) for group in summary_source_groups(items).values())
    rebuild_reads.clear()
    result = case.prepare(
        "summary-read", request_messages=messages,
        summary_candidate=SummaryCandidateScope(0, "initial", False, minimal_system, canonical_messages=messages),
    )
    assert len(rebuild_reads) == 1
    assert (rebuild_reads[0] is not None) is with_transition
    snapshot = case.snapshot(result)
    assert snapshot.selection_policy == "summary_candidate"
    assert snapshot.seal_binding.source_revisions == ()
    assert snapshot.seal_binding.prefix.consumed_transition is None
    assert case.source_rows() == before
    assert case.pending_rows() == pending
    assert case.active_view() == view
    rebuild_reads.clear()
    following = case.snapshot(case.prepare("summary-following"))
    assert len(rebuild_reads) == 1
    assert (rebuild_reads[0] is not None) is with_transition
    if with_transition:
        assert following.seal_binding.prefix.consumed_transition.transition_id == pending[0][0]
        assert case.pending_rows()[0][3:] == ("consumed", following.assembly_id)
    else:
        assert following.seal_binding.prefix.consumed_transition is None
        assert {row[0]: row[3] for row in case.source_rows()}["workspace:rules"] == "rules:2"


def test_rewind_reconciles_only_from_frozen_control_state(epoch_case, rebuild_reads):
    case = epoch_case
    second = case.advance()
    second_bytes = canonical_json_bytes(case.snapshot(second).to_dict())
    old_assembly_count = case.connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    old_item_count = case.connection.execute("SELECT count(*) FROM item_catalog").fetchone()
    case.rewind("checkpoint-first")
    target_view = case.active_view()
    (pending,) = case.pending_rows()
    assert pending[2:] == ("rewind", "pending", None)
    assert case.connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == old_assembly_count
    assert case.connection.execute("SELECT count(*) FROM item_catalog").fetchone() == old_item_count
    assert all(row[2] == row[3] and row[2].endswith(":1") for row in case.source_rows())
    assert case.snapshot(second).seal_binding.prefix.state.epoch.ordinal == 1
    rebuild_reads.clear()
    rebuilt = case.prepare("rewind")
    assert len(rebuild_reads) == 1
    assert rebuild_reads[0] is not None
    assert rebuild_reads[0].transition.transition_id == pending[0]
    snapshot = case.snapshot(rebuilt)
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 2
    assert snapshot.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert snapshot.seal_binding.prefix.consumed_transition.consumed
    assert case.pending_rows()[0][3:] == ("consumed", snapshot.assembly_id)
    assert case.active_view() == target_view
    assert rebuilt["messages"][0] == SystemMessage(
        content=snapshot.seal_binding.root.content, id="root:" + snapshot.seal_binding.prefix.state.epoch.token,
    )
    assert json.loads(rebuilt["messages"][0].content)["sources"][0]["body"] == "基础规则第二版"
    assert isinstance(rebuilt["messages"][-1], HumanMessage)
    assert json.loads(rebuilt["messages"][-1].content)["sources"][0]["body"] == "v2\n"
    assert len([message for message in rebuilt["messages"] if isinstance(message, SystemMessage)]) == 1
    assert all(row[2] == row[3] and row[2].endswith(":2") for row in case.source_rows())
    assert case.publisher.reads == [RULES_URI, SKILL_URI]
    (skill_delta,) = tuple(
        delta for delta in case.manager._last_model_call_receipt.deltas
        if delta.source_id == "skill:debugging"
    )
    assert (skill_delta.kind, skill_delta.content) == ("rebuild", "v2\n")
    assert case.manager.prepare_pending() is None
    assert canonical_json_bytes(case.snapshot(second).to_dict()) == second_bytes


@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
def test_real_rebuild_intent_uses_boundary_transaction_and_rolls_back_before_retry(
    epoch_case, monkeypatch, boundary,
):
    case = epoch_case
    before = tuple(case.connection.iterdump())
    jsonl = case.saver._storage.storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    before_jsonl = jsonl.read_bytes()
    before_facades = tuple(
        (owner, facade.consumed_keys) for owner, facade in case.saver._mutation_intent_facades.items()
    )
    receipts = []
    consume = case.saver.consume_mutation_intent
    fail = True

    def observe(intent, connection=None):
        if not isinstance(intent, RebuildContextEpochIntent):
            return consume(intent, connection)
        assert isinstance(connection, sqlite3.Connection) and connection.in_transaction
        assert intent.owner == MutationIntentOwner(case.owner.session_id, case.owner.thread_id)
        assert intent.checkpoint_ns == ""
        assert connection.execute(
            "SELECT history_view_revision, last_control_sequence FROM database_meta"
        ).fetchone() == (intent.view_revision, intent.control_revision)
        assert connection.execute(
            "SELECT view_id, branch_id FROM context_views WHERE view_id=?", (intent.target_view_id,),
        ).fetchone()[0] == intent.target_view_id
        consume(intent, connection)
        receipts.append(intent)
        assert connection.execute(
            "SELECT source_checkpoint_id, target_view_id, reason, status FROM context_epoch_transitions "
            "WHERE target_view_id=?", (intent.target_view_id,),
        ).fetchone() == (
            intent.source_checkpoint_id, intent.target_view_id, boundary, "pending",
        )
        if fail:
            raise RuntimeError("injected typed rebuild boundary failure")

    def rebuild():
        if boundary == "rewind":
            case.rewind("checkpoint-first")
        else:
            case.checkpoint("checkpoint-typed-compaction", compaction=True,
                            messages=[case.user, AIMessage(content="typed 事务摘要", id="typed-summary")])

    monkeypatch.setattr(case.saver, "consume_mutation_intent", observe)
    with pytest.raises(RuntimeError, match="injected typed rebuild"):
        rebuild()
    assert len(receipts) == 1
    assert tuple(case.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl
    assert tuple(
        (owner, facade.consumed_keys) for owner, facade in case.saver._mutation_intent_facades.items()
    ) == before_facades
    fail = False
    rebuild()
    assert len(receipts) == 2
    intent = receipts[-1]
    assert case.pending_rows()[0][1:4] == (intent.target_view_id, boundary, "pending")
    case.restart()
    rebuilt = case.snapshot(case.prepare("typed-rebuild:" + boundary))
    assert case.pending_rows()[0][3:] == ("consumed", rebuilt.assembly_id)
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ("rewind", "arewind"))
@pytest.mark.parametrize("target_status", ("missing", "pruned"))
async def test_explicit_rewind_target_refusal_keeps_storage_and_runtime_before_valid_retry(
    epoch_case, monkeypatch, entrypoint, target_status,
):
    case = epoch_case
    case.advance()
    target = "checkpoint-unavailable"
    if target_status == "pruned":
        case.checkpoint(target)
    case.checkpoint("checkpoint-current")
    if target_status == "pruned":
        assert case.connection.execute(
            "UPDATE checkpoints SET status='pruned' WHERE checkpoint_id=?", (target,),
        ).rowcount == 1
        case.connection.commit()
    before = tuple(case.connection.iterdump())
    jsonl = case.saver._storage.storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    before_jsonl = jsonl.read_bytes()
    before_view, before_config = case.active_view(), deepcopy(case.config)
    cache_names = (
        "_context_reconciliations", "_prepared_dispatches",
        "_request_only_content", "_plan_request_content",
    )
    before_caches = tuple(deepcopy(getattr(case.saver, name)) for name in cache_names)
    before_facades = tuple(
        (owner, facade, facade.consumed_keys)
        for owner, facade in case.saver._mutation_intent_facades.items()
    )
    before_receipt = case.manager._last_model_call_receipt
    before_pending = case.manager.prepare_pending()
    before_reads = tuple(case.publisher.reads)
    intents = []
    consume = case.saver.consume_mutation_intent

    def observe(intent, connection=None):
        intents.append(intent)
        return consume(intent, connection)

    monkeypatch.setattr(case.saver, "consume_mutation_intent", observe)
    with case.scope(), pytest.raises(KeyError, match="context-boundary-checkpoint-unavailable") as error:
        if entrypoint == "rewind":
            case.saver.rewind(case.config, checkpoint_id=target)
        else:
            await case.saver.arewind(case.config, checkpoint_id=target)
    assert target in str(error.value)
    assert case.owner.session_id in str(error.value)
    assert case.owner.thread_id in str(error.value)
    assert "checkpoint_ns=''" in str(error.value)
    assert tuple(case.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl
    assert case.active_view() == before_view
    assert case.config == before_config
    assert tuple(getattr(case.saver, name) for name in cache_names) == before_caches
    assert tuple(
        (owner, facade, facade.consumed_keys)
        for owner, facade in case.saver._mutation_intent_facades.items()
    ) == before_facades
    assert case.manager._last_model_call_receipt == before_receipt
    assert case.manager.prepare_pending() == before_pending
    assert tuple(case.publisher.reads) == before_reads
    assert intents == []
    with case.scope():
        if entrypoint == "rewind":
            case.config = case.saver.rewind(case.config, checkpoint_id="checkpoint-first")
        else:
            case.config = await case.saver.arewind(case.config, checkpoint_id="checkpoint-first")
    assert case.config == {"configurable": {"thread_id": case.owner.thread_id, "checkpoint_ns": ""}}
    assert case.active_view() != before_view
    (intent,) = intents
    assert isinstance(intent, RebuildContextEpochIntent)
    assert intent.owner == MutationIntentOwner(case.owner.session_id, case.owner.thread_id)
    assert (intent.checkpoint_ns, intent.source_checkpoint_id, intent.target_view_id) == (
        "", "checkpoint-first", case.active_view(),
    )
    assert case.connection.execute(
        "SELECT checkpoint_ns, source_checkpoint_id, target_view_id, reason, control_sequence, status "
        "FROM context_epoch_transitions WHERE target_view_id=?", (intent.target_view_id,),
    ).fetchone() == ("", "checkpoint-first", intent.target_view_id, "rewind", intent.control_revision, "pending")
    assert case.connection.execute(
        "SELECT history_view_revision, last_control_sequence FROM database_meta"
    ).fetchone() == (intent.view_revision, intent.control_revision)
    case.restart()
    rebuilt = case.snapshot(case.prepare("valid-rewind-after-refusal"))
    assert rebuilt.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == 2
    assert case.pending_rows()[0][3:] == ("consumed", rebuilt.assembly_id)


def test_source_failure_keeps_view_pending_versions_and_retry_is_atomic(epoch_case, rebuild_reads):
    case = epoch_case
    second = case.advance()
    old_snapshot = canonical_json_bytes(case.snapshot(second).to_dict())
    case.rewind("checkpoint-first")
    # activation 是 seal 的已提交前置；原子性基线从冻结完成后开始。
    with case.scope():
        case.activation(case.saver, plan_creation_idempotency_key="epoch-create:retry")
    before, pending, view = case.source_rows(), case.pending_rows(), case.active_view()
    selected, dispatch, versions = case.selected_rows(), case.dispatch_rows(), case.checkpoint_versions()
    before_items = case.connection.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence").fetchall()
    before_details = case.connection.execute("SELECT assembly_id, detail_ref FROM context_assemblies ORDER BY rowid").fetchall()
    def detail_files():
        root = case.saver._storage.storage.root(case.owner.session_id, thread_id=case.owner.thread_id)
        return tuple(
            (path.relative_to(root).as_posix(), path.read_bytes()) for path in sorted(root.rglob("*"))
            if path.is_file() and (
                "context-plan-details" in path.parts
            )
        )

    before_detail_files = detail_files()
    # prepare 可登记新 unsealed draft；既有 registry 的完整行及 seal 游标不能改变。
    plan_ids = tuple(row[0] for row in case.connection.execute(
        "SELECT plan_id FROM context_plans ORDER BY plan_id"
    ).fetchall())
    placeholders = ",".join("?" for _ in plan_ids)
    contribution_ids = tuple(row[0] for row in case.connection.execute(
        "SELECT contribution_id FROM context_contributions ORDER BY contribution_id"
    ).fetchall())
    contribution_placeholders = ",".join("?" for _ in contribution_ids)
    next_source_ordinal = case.connection.execute(
        "SELECT max(source_ordinal) + 1 FROM context_contributions"
    ).fetchone()[0]
    frozen = case.manager.prepare_pending()
    expected_drafts = {
        (f"source-update:{delta.source_id}:{delta.revision}:rebuild"
         + (f":{delta.selection_sequence}:{delta.control_revision}" if delta.source_kind == "skill" else "")): delta
        for delta in frozen.deltas
    }
    state_revisions = {row[0]: row[4] for row in case.source_rows()}
    assert set(expected_drafts) == {
        "source-update:workspace:rules:rules:2:rebuild",
        f"source-update:skill:debugging:skill:2:rebuild:0:{state_revisions['skill:debugging']}",
    }
    seal_queries = (
        ("SELECT * FROM item_catalog ORDER BY item_sequence", ()),
        ("SELECT * FROM item_projections ORDER BY item_sequence", ()),
        ("SELECT * FROM context_plan_details ORDER BY detail_ref", ()),
        ((f"SELECT * FROM context_contributions WHERE contribution_id IN ({contribution_placeholders}) "
          "ORDER BY assembly_id, contribution_id"), contribution_ids),
        ("SELECT * FROM storage_commits ORDER BY commit_id", ()),
        ("SELECT * FROM database_meta ORDER BY singleton_id", ()),
        ("SELECT * FROM model_calls ORDER BY model_call_id", ()),
        (f"SELECT * FROM context_plans WHERE plan_id IN ({placeholders}) ORDER BY plan_id", plan_ids),
        (f"SELECT * FROM context_plan_refs WHERE plan_id IN ({placeholders}) ORDER BY plan_id, ref_type, ref_id", plan_ids),
        (f"SELECT * FROM context_plan_contributions WHERE plan_id IN ({placeholders}) ORDER BY plan_id, contribution_id", plan_ids),
        (f"SELECT * FROM tool_set_snapshots WHERE plan_id IN ({placeholders}) ORDER BY plan_id, tool_set_snapshot_id", plan_ids),
        ("SELECT plan_id, assembly_id FROM tool_set_snapshots WHERE assembly_id IS NOT NULL ORDER BY plan_id, tool_set_snapshot_id", ()),
    )
    before_seal_facts = tuple(case.connection.execute(query, params).fetchall() for query, params in seal_queries)
    case.connection.execute(
        "CREATE TRIGGER refuse_restored_source BEFORE UPDATE ON context_source_control_states "
        "WHEN NEW.source_id='skill:debugging' BEGIN SELECT RAISE(ABORT, 'injected source failure'); END"
    )
    case.connection.commit()
    rebuild_reads.clear()
    with pytest.raises(sqlite3.IntegrityError, match="injected source failure"):
        case.prepare("retry")
    assert len(rebuild_reads) == 1 and rebuild_reads[0] is not None
    assert case.source_rows() == before
    assert case.pending_rows() == pending
    assert case.active_view() == view
    assert case.connection.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence").fetchall() == before_items
    assert case.connection.execute("SELECT assembly_id, detail_ref FROM context_assemblies ORDER BY rowid").fetchall() == before_details
    assert case.connection.execute("SELECT plan_state, assembly_id FROM context_plans WHERE plan_id LIKE 'plan-prepared:%' ORDER BY rowid DESC LIMIT 1").fetchone() == ("unsealed", None)
    assert case.selected_rows() == selected
    assert case.dispatch_rows() == dispatch
    assert case.checkpoint_versions() == versions
    assert canonical_json_bytes(case.snapshot(second).to_dict()) == old_snapshot
    assert tuple(case.connection.execute(query, params).fetchall() for query, params in seal_queries) == before_seal_facts
    assert detail_files() == before_detail_files
    new_contributions = case.connection.execute(
        f"SELECT * FROM context_contributions WHERE contribution_id NOT IN ({contribution_placeholders}) "
        "ORDER BY source_ordinal", contribution_ids,
    ).fetchall()
    assert len(new_contributions) == 2
    for ordinal, row in enumerate(new_contributions, next_source_ordinal):
        delta = expected_drafts[row[0]]
        assert row[:14] == (
            row[0], delta.source_kind, delta.revision,
            contribution_content_hash("notice", delta.content),
            payload_content_length("structured_content", delta.content),
            1, "notice", "internal", "public", None, None,
            ordinal, "direct", "immutable",
        )
        assert json.loads(row[14]) == {
            "root_placement": {"workspace:rules": "root_eligible", "skill:debugging": "tail_only"}[delta.source_id],
            "source_binding": SourceBinding(source_ref=delta.source_id).to_dict(),
            "extensions": {},
        }
        assert isinstance(row[15], str) and row[15]
        assert case.connection.execute(
            "SELECT assembly_id FROM context_assembly_contributions WHERE contribution_id=?", (row[0],),
        ).fetchall() == []
    new_plans = case.connection.execute(
        f"SELECT plan_id, plan_state, assembly_id, seal_idempotency_key, seal_hash, seal_input_hash "
        f"FROM context_plans WHERE plan_id NOT IN ({placeholders}) ORDER BY plan_id", plan_ids,
    ).fetchall()
    assert len(new_plans) == 1
    assert new_plans[0][1:] == ("unsealed", None, None, None, None)
    new_registration = case.saver.get_context_plan_registration(
        case.owner.session_id, thread_id=case.owner.thread_id, plan_id=new_plans[0][0],
    )
    assert new_registration.plan_state == "unsealed" and new_registration.assembly_id is None
    assert case.connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots WHERE plan_id=?", (new_plans[0][0],),
    ).fetchall() == [(None,)]
    case.connection.execute("DROP TRIGGER refuse_restored_source")
    case.connection.commit()
    rebuild_reads.clear()
    rebuilt = case.prepare("retry")
    assert len(rebuild_reads) == 1 and rebuild_reads[0] is not None
    snapshot = case.snapshot(rebuilt)
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 2
    after = case.source_rows()

    def refuse_desired(*_args):
        pytest.fail("sealed attempt retry 不得重新冻结 source/ToolSet")

    case.manager.bind_published_revision_supplier(refuse_desired)
    retry = case.prepare("retry", supplier=refuse_desired)
    assert retry["assembly_id"] == rebuilt["assembly_id"]
    assert retry["messages"] == rebuilt["messages"]
    assert case.source_rows() == after
    assert len(case.pending_rows()) == 1
    assert case.pending_rows()[0][3:] == ("consumed", snapshot.assembly_id)


def test_restart_consumes_pending_and_sealed_retry_never_reads_live_snapshot(epoch_case, rebuild_reads, monkeypatch):
    case = epoch_case
    case.advance()
    case.rewind("checkpoint-first")
    pending = case.pending_rows()
    case.restart()
    rebuild_reads.clear()
    rebuilt = case.prepare("restart")
    assert len(rebuild_reads) == 1
    assert rebuild_reads[0] is not None
    assert rebuild_reads[0].transition.transition_id == pending[0][0]
    snapshot_bytes = canonical_json_bytes(case.snapshot(rebuilt).to_dict())
    assert case.pending_rows()[0][0] == pending[0][0]
    assert case.pending_rows()[0][3] == "consumed"
    case.restart()

    def refuse_desired(*_args, **_kwargs):
        pytest.fail("重启 sealed retry 必须复用持久 binding 和旧 request bytes")

    case.manager.bind_published_revision_supplier(refuse_desired)
    monkeypatch.setattr(case.saver, "load_context_source_rebuild_state", refuse_desired)
    monkeypatch.setattr(case.manager, "prepare_sources", refuse_desired)
    retry = case.prepare("restart", supplier=refuse_desired)
    assert retry["assembly_id"] == rebuilt["assembly_id"]
    assert retry["messages"] == rebuilt["messages"]
    assert canonical_json_bytes(case.snapshot(retry).to_dict()) == snapshot_bytes


def test_unavailable_published_source_retains_committed_rewind_without_dispatch(epoch_case):
    case = epoch_case
    case.advance()
    case.rewind("checkpoint-first")
    before, pending, view = case.source_rows(), case.pending_rows(), case.active_view()
    selected, dispatch, versions = case.selected_rows(), case.dispatch_rows(), case.checkpoint_versions()
    assemblies = case.connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall()
    case.publisher.publish(SKILL_URI, "skill:2", SKILL_FRONTMATTER + "v2\n", available=False)
    with pytest.raises(RuntimeError, match="tracked-source-unavailable"):
        case.prepare("unavailable")
    assert case.source_rows() == before
    assert case.pending_rows() == pending
    assert case.active_view() == view
    assert case.connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall() == assemblies
    assert case.selected_rows() == selected
    assert case.dispatch_rows() == dispatch
    assert case.checkpoint_versions() == versions


def test_snapshot_activation_after_cutoff_is_not_restored_or_refrozen(epoch_case):
    case = epoch_case
    binding = case.load_snapshot()
    visible = case.prepare("snapshot-after-cutoff")
    assert json.loads(visible["messages"][-1].content)["sources"][0]["body"] == binding.body
    old_bytes = canonical_json_bytes(case.snapshot(visible).to_dict())
    case.rewind("checkpoint-first")
    rebuilt = case.prepare("without-post-cutoff-snapshot")
    assert all(message.content != binding.body for message in rebuilt["messages"])
    assert all(source.source_id != "skill:one-shot" for source in case.snapshot(rebuilt).seal_binding.source_revisions)
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:one-shot"] == ("untracked", None, None)
    assert binding.display_uri not in case.publisher.reads
    assert case.manager.prepare_pending() is None
    assert canonical_json_bytes(case.snapshot(visible).to_dict()) == old_bytes


def test_same_checkpoint_earlier_user_anchor_removes_snapshot_after_restart(epoch_case):
    case = epoch_case
    binding = case.load_snapshot()
    visible = case.prepare("snapshot-before-same-checkpoint-cut")
    original = case.snapshot(visible)
    assert json.loads(visible["messages"][-1].content)["sources"][0]["body"] == binding.body
    (original_ref, original_block), = (
        (ref, block) for frame in original.seal_binding.prefix.state.frames if frame.role == "user" and frame.source_protocol is not None
        for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
        if ref.source_identity == "skill:one-shot"
    )
    assert (original_block.body, original_ref.source_revision) == (
        binding.body, binding.activation_revision,
    )
    old_bytes = canonical_json_bytes(original.to_dict())
    checkpoint_id = "checkpoint-snapshot-later-than-user-anchor"
    case.checkpoint(
        checkpoint_id,
        messages=[case.user, AIMessage(content="snapshot 后的真实回答", id="snapshot-after-user")],
    )
    assert case.connection.execute(
        "SELECT source_assembly_id FROM checkpoint_context_source_snapshots WHERE checkpoint_id=?",
        (checkpoint_id,),
    ).fetchone() == (original.assembly_id,)
    case.rewind(checkpoint_id, anchor=case.user.id)
    (pending,) = case.pending_rows()
    assert pending[2:] == ("rewind", "pending", None)
    assert case.connection.execute(
        "SELECT source_checkpoint_id FROM context_epoch_transitions WHERE transition_id=?",
        (pending[0],),
    ).fetchone() == (checkpoint_id,)
    control = json.loads(case.connection.execute(
        "SELECT payload_json FROM control_events WHERE control_kind='rewind' ORDER BY control_sequence DESC LIMIT 1"
    ).fetchone()[0])
    assert control["source_checkpoint_id"] == checkpoint_id
    assert control["source_anchor"] == case.user.id
    assert case.connection.execute(
        "SELECT message_id FROM messages WHERE message_sequence=?",
        (control["cutoff_message_sequence"],),
    ).fetchone() == (case.user.id,)
    case.publisher.publish(binding.display_uri, "snapshot:wrong", "不得读取的当前正文", available=False)
    case.publisher.reads.clear()
    case.restart()
    rebuilt = case.prepare("snapshot-excluded-by-same-checkpoint-anchor")
    snapshot = case.snapshot(rebuilt)
    assert all(message.content != binding.body for message in rebuilt["messages"])
    assert all(
        ref.source_identity != "skill:one-shot"
        for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs
    )
    assert all(source.source_id != "skill:one-shot" for source in snapshot.seal_binding.source_revisions)
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == original.seal_binding.prefix.state.epoch.ordinal + 1
    assert case.pending_rows()[0][3:] == ("consumed", snapshot.assembly_id)
    assert {row[0]: row[1] for row in case.source_rows()}["skill:one-shot"] == "untracked"
    assert case.publisher.reads == [RULES_URI, SKILL_URI]
    assert binding.display_uri not in case.publisher.reads
    assert case.manager.prepare_pending() is None
    assert canonical_json_bytes(case.snapshot(visible).to_dict()) == old_bytes


@pytest.mark.parametrize("cut", ("inclusive", "before", "complete"))
def test_promoted_snapshot_root_preserves_activation_frontier_through_actual_rewind(epoch_case, cut):
    case = epoch_case
    binding = case.load_snapshot(root_eligible=True)
    visible = case.prepare("root-snapshot-activation")
    activated = case.snapshot(visible)
    (activation_frame, activation_ref, activation_block), = (
        (frame, ref, block) for frame in activated.seal_binding.prefix.state.frames if frame.role == "user" and frame.source_protocol is not None
        for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
        if ref.source_identity == "skill:one-shot"
    )
    assert (activation_frame.role, activation_block.body, activation_ref.source_revision) == (
        "user", binding.body, binding.activation_revision,
    )
    activation_frontier = case.connection.execute(
        "SELECT ref_id, source_revision, source_cut_position_kind, source_cut_item_id "
        "FROM context_assembly_selections WHERE assembly_id=? AND ref_id=?",
        (activated.assembly_id, activation_ref.ref_id),
    ).fetchone()
    assert activation_frontier == (
        activation_ref.ref_id, binding.activation_revision,
        "after_canonical", case.accepted["root_input_item_id"],
    )
    messages = [case.user, AIMessage(content="source 注入后的真实 canonical 回答", id="snapshot-root-later-answer")]
    case.checkpoint("checkpoint-before-root-promotion", messages=messages)
    tools = ToolSetSnapshot(
        selection_revision=sha256_jcs({"epoch_marker": True}),
        tools=({"type": "function", "function": {
            "name": "epoch_marker", "description": "触发合法工具集边界",
            "parameters": {"type": "object", "properties": {}},
        }},),
    )
    promoted = case.prepare("root-snapshot-promoted", supplier=lambda: tools)
    promoted_snapshot = case.snapshot(promoted)
    assert promoted_snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.TOOLSET_CHANGED
    assert promoted_snapshot.seal_binding.prefix.state.epoch.ordinal == activated.seal_binding.prefix.state.epoch.ordinal + 1
    assert [block["body"] for block in json.loads(promoted["messages"][0].content)["sources"]
            if block["source_identity"] == "skill:one-shot"] == [binding.body]
    assert all(ref.source_identity != "skill:one-shot"
               for frame in promoted_snapshot.seal_binding.prefix.state.frames if frame.role == "user"
               for ref in frame.item_refs)
    (provenance,) = tuple(
        source for source in promoted_snapshot.seal_binding.root.sources
        if source.source_identity == "skill:one-shot"
    )
    assert provenance.revision == binding.activation_revision
    contribution = next(
        source for source in promoted_snapshot.contributions
        if source.source_binding is not None and source.source_binding.source_ref == "skill:one-shot"
    )
    (root_entry,) = tuple(
        entry for entry in promoted_snapshot.selection
        if entry.included and entry.contribution_id == contribution.contribution_id
    )
    assert root_entry.ref.ref_id != activation_ref.ref_id
    assert case.connection.execute(
        "SELECT source_revision, source_cut_position_kind, source_cut_item_id "
        "FROM context_assembly_selections WHERE assembly_id=? AND ref_id=?",
        (promoted_snapshot.assembly_id, root_entry.ref.ref_id),
    ).fetchone() == activation_frontier[1:]
    immutable = tuple(canonical_json_bytes(value.to_dict()) for value in (activated, promoted_snapshot))
    checkpoint = "checkpoint-promoted-snapshot-root"
    case.checkpoint(checkpoint, messages=messages)
    case.rewind(
        checkpoint, anchor=None if cut == "complete" else case.user.id,
        anchor_mode="before" if cut == "before" else "inclusive",
    )
    (pending,) = case.pending_rows()
    assert case.connection.execute(
        "SELECT canonical_cut_kind, canonical_cut_item_id FROM context_epoch_transitions WHERE transition_id=?",
        (pending[0],),
    ).fetchone() == (
        ("complete", None) if cut == "complete" else
        ("before_canonical" if cut == "before" else "after_canonical", case.accepted["root_input_item_id"])
    )
    case.publisher.publish(binding.display_uri, "snapshot:wrong", "不得读取的当前正文", available=False)
    case.publisher.reads.clear()
    case.restart()
    rebuilt = case.prepare("root-snapshot-rewind:" + cut, supplier=lambda: tools)
    snapshot = case.snapshot(rebuilt)
    sources = {source.source_identity: source.revision for source in snapshot.seal_binding.root.sources}
    assert ("skill:one-shot" in sources) == (cut == "complete")
    if cut == "complete":
        assert sources["skill:one-shot"] == binding.activation_revision
        assert [block["body"] for block in json.loads(rebuilt["messages"][0].content)["sources"]
                if block["source_identity"] == "skill:one-shot"] == [binding.body]
    else:
        assert all(binding.body not in message.content for message in rebuilt["messages"])
    assert {source.source_id: source.revision for source in snapshot.seal_binding.source_revisions} == {
        "workspace:rules": "rules:1", "skill:debugging": "skill:1",
    }
    assert sources["workspace:rules"] == "rules:1"
    assert any(ref.source_identity == "skill:debugging" for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs)
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == promoted_snapshot.seal_binding.prefix.state.epoch.ordinal + 1
    assert snapshot.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert case.pending_rows()[-1][3:] == ("consumed", snapshot.assembly_id)
    assert case.publisher.reads == [RULES_URI, SKILL_URI]
    assert binding.display_uri not in case.publisher.reads
    assert case.manager.prepare_pending() is None
    assert tuple(canonical_json_bytes(case.snapshot(value).to_dict()) for value in (visible, promoted)) == immutable


@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
def test_visible_snapshot_is_carried_from_committed_detail_after_restart(epoch_case, boundary):
    case = epoch_case
    binding = case.load_snapshot()
    visible = case.prepare("snapshot-before-cutoff")
    old_bytes = canonical_json_bytes(case.snapshot(visible).to_dict())
    old_epoch = case.snapshot(visible).seal_binding.prefix.state.epoch.ordinal
    case.checkpoint("checkpoint-snapshot-visible")
    if boundary == "rewind":
        case.rewind("checkpoint-snapshot-visible")
    else:
        case.checkpoint("checkpoint-snapshot-compacted", compaction=True,
                        messages=[case.user, AIMessage(content="压缩后的摘要", id="snapshot-summary")])
    (pending,) = case.pending_rows()
    assert pending[2:] == (boundary, "pending", None)
    case.publisher.publish(binding.display_uri, "snapshot:wrong", "不得读取的当前正文", available=False)
    case.publisher.reads.clear()
    case.restart()
    rebuilt = case.prepare("carried-snapshot:" + boundary)
    snapshot = case.snapshot(rebuilt)
    assert snapshot.assembly_id != visible["assembly_id"]
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason(boundary)
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == old_epoch + 1
    assert snapshot.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert snapshot.seal_binding.prefix.consumed_transition.consumed
    assert case.pending_rows()[0][3:] == ("consumed", snapshot.assembly_id)
    (ref, block), = ((ref, block) for frame in snapshot.seal_binding.prefix.state.frames if frame.role == "user" and frame.source_protocol is not None
                    for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                    if ref.source_identity == "skill:one-shot")
    assert (block.body, ref.source_revision) == (binding.body, binding.activation_revision)
    assert all(source.source_id != "skill:one-shot" for source in snapshot.seal_binding.source_revisions)
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:one-shot"] == ("untracked", "snapshot:1", "snapshot:1")
    assert case.publisher.reads == [RULES_URI, SKILL_URI]
    assert case.manager.prepare_pending() is None
    assert canonical_json_bytes(case.snapshot(visible).to_dict()) == old_bytes


def test_earlier_anchor_retains_only_first_untracked_revision_and_visible_control_baseline(epoch_case):
    case = epoch_case
    first = case.snapshot(case.first)
    (first_frame, first_ref), = tuple(
        (frame, ref) for frame in first.seal_binding.prefix.state.frames for ref in frame.item_refs
        if ref.source_identity == "skill:debugging"
    )
    answer = AIMessage(content="第一版来源之后的真实 canonical 回答 B", id="source-revision-answer-b")
    case.checkpoint("checkpoint-between-source-revisions", messages=[case.user, answer])
    second = case.advance()
    second_snapshot = case.snapshot(second)
    (_second_frame, second_ref), = tuple(
        (frame, ref) for frame in second_snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs
        if ref.source_identity == "skill:debugging" and ref.source_revision == "skill:2"
    )
    assert first_ref.source_revision == "skill:1"
    assert first_ref.ref_id != second_ref.ref_id
    (answer_item,) = case.connection.execute(
        "SELECT item_id FROM item_catalog WHERE "
        "json_extract(projection_manifest_json, '$.projection_identity.projection_message_id')=?",
        (answer.id,),
    ).fetchone()
    assert case.connection.execute(
        "SELECT ref_id, source_revision, source_cut_position_kind, source_cut_item_id "
        "FROM context_assembly_selections WHERE assembly_id=? AND ref_id IN (?, ?) ORDER BY plan_ordinal",
        (second_snapshot.assembly_id, first_ref.ref_id, second_ref.ref_id),
    ).fetchall() == [
        (first_ref.ref_id, "skill:1", "after_canonical", case.accepted["root_input_item_id"]),
        (second_ref.ref_id, "skill:2", "after_canonical", answer_item),
    ]
    assert case.manager.load_skill("debugging", mode="untrack").tracked is False
    checkpoint = "checkpoint-after-both-source-revisions"
    case.checkpoint(checkpoint, messages=[
        case.user, answer, AIMessage(content="第二版来源后的真实 canonical 回答 C", id="source-revision-answer-c"),
    ])
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:debugging"] == (
        "untracked", "skill:2", "skill:2",
    )
    checkpoint_control = case.connection.execute(
        "SELECT tracking_status, latest_visible_committed_revision, latest_revision "
        "FROM checkpoint_context_source_states WHERE checkpoint_id=? AND source_id='skill:debugging'",
        (checkpoint,),
    ).fetchone()
    assert checkpoint_control == ("untracked", "skill:2", "skill:2")
    immutable = tuple(canonical_json_bytes(value.to_dict()) for value in (first, second_snapshot))
    case.rewind(checkpoint, anchor=answer.id)
    (pending,) = case.pending_rows()
    # registration 按目标 checkpoint 恢复，observed/audit revision 保留；
    # 只有真正被 cut 保留的最后 ref/revision 能成为 visible committed 基准。
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:debugging"] == (
        "untracked", "skill:2", "skill:1",
    )
    assert case.connection.execute(
        "SELECT tracking_status, latest_visible_committed_revision, latest_revision "
        "FROM checkpoint_context_source_states WHERE checkpoint_id=? AND source_id='skill:debugging'",
        (checkpoint,),
    ).fetchone() == checkpoint_control
    case.publisher.publish(SKILL_URI, "skill:wrong", "不可读取的当前来源", available=False)
    case.publisher.reads.clear()
    case.restart()
    rebuilt = case.prepare("source-revision-cut-after-restart")
    snapshot = case.snapshot(rebuilt)
    retained = tuple(
        (ref.ref_id, ref.source_revision, frame.content)
        for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs
        if ref.source_identity == "skill:debugging"
    )
    expected = json.loads(first_frame.content)
    expected["sources"][0]["source_tracking_at_seal"] = "untracked"
    assert retained == ((first_ref.ref_id, "skill:1", canonical_json_bytes(expected).decode()),)
    assert all(ref.ref_id != second_ref.ref_id for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs)
    assert {source.source_id: source.revision for source in snapshot.seal_binding.source_revisions} == {
        "workspace:rules": "rules:2",
    }
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:debugging"] == (
        "untracked", "skill:2", "skill:1",
    )
    assert case.pending_rows()[0][3:] == ("consumed", snapshot.assembly_id)
    assert snapshot.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert case.publisher.reads == [RULES_URI]
    assert case.manager.prepare_pending() is None
    assert tuple(canonical_json_bytes(case.snapshot(value).to_dict()) for value in (case.first, second)) == immutable


def test_untrack_before_and_after_checkpoint_changes_actual_rewind_tracking(epoch_case):
    case = epoch_case
    second = case.advance()
    expected_members = tuple(
        (ref.ref_id, ref.source_revision, replace(block, source_tracking_at_seal="untracked"))
        for frame in case.snapshot(second).seal_binding.prefix.state.frames
        if frame.role == "user" and frame.source_protocol is not None
        for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
        if ref.source_identity == "skill:debugging"
    )
    case.checkpoint("checkpoint-tracked")
    binding = SkillCatalogBinding("debugging", "resource:debugging", "entry:debugging", SKILL_URI, "skill:2", "v2\n")
    case.manager.install_skill_activation_snapshot(SkillCatalogActivationSnapshot("catalog:2", {"debugging": binding}))
    assert case.manager.load_skill("debugging", mode="untrack").tracked is False
    case.checkpoint("checkpoint-untracked")
    case.rewind("checkpoint-tracked")
    assert {row[0]: row[1] for row in case.source_rows()}["skill:debugging"] == "tracked"
    restored = case.prepare("tracked-again")
    assert json.loads(restored["messages"][-1].content)["sources"][0]["body"] == "v2\n"
    case.rewind("checkpoint-untracked")
    assert {row[0]: row[1] for row in case.source_rows()}["skill:debugging"] == "untracked"
    case.publisher.reads.clear()
    frozen = case.prepare("untracked")
    snapshot = case.snapshot(frozen)
    assert tuple((ref.ref_id, ref.source_revision, block)
                 for frame in snapshot.seal_binding.prefix.state.frames
                 if frame.role == "user" and frame.source_protocol is not None
                 for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                 if ref.source_identity == "skill:debugging") == expected_members
    assert all(source.source_id != "skill:debugging" for source in snapshot.seal_binding.source_revisions)
    assert case.publisher.reads == [RULES_URI]
    assert case.manager.prepare_pending() is None


def test_actual_compaction_checkpoint_defers_epoch_and_materializes_full_sources(epoch_case, rebuild_reads):
    case = epoch_case
    second = case.advance()
    old_snapshot = canonical_json_bytes(case.snapshot(second).to_dict())
    before_assemblies = case.connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    case.checkpoint("checkpoint-compacted", compaction=True, messages=[case.user, AIMessage(content="压缩摘要", id="summary-epoch")])
    (pending,) = case.pending_rows()
    assert pending[2:] == ("compaction", "pending", None)
    assert case.connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == before_assemblies
    assert case.snapshot(second).seal_binding.prefix.state.epoch.ordinal == 1
    rebuild_reads.clear()
    rebuilt = case.prepare("compaction")
    assert len(rebuild_reads) == 1
    assert rebuild_reads[0] is not None
    assert rebuild_reads[0].transition.transition_id == pending[0]
    snapshot = case.snapshot(rebuilt)
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.COMPACTION
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 2
    assert snapshot.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert json.loads(rebuilt["messages"][0].content)["sources"][0]["body"] == "基础规则第二版"
    assert json.loads(rebuilt["messages"][-1].content)["sources"][0]["body"] == "v2\n"
    assert case.pending_rows()[0][3] == "consumed"
    assert canonical_json_bytes(case.snapshot(second).to_dict()) == old_snapshot


def test_history_replay_never_replays_csm_or_opens_pending_epoch(epoch_case):
    case = epoch_case
    second = case.advance()
    case.manager.load_skill("debugging", mode="untrack")
    states, snapshots = case.source_rows(), canonical_json_bytes(case.snapshot(second).to_dict())
    with case.scope():
        case.saver.history_replay(case.config, turn_id=str(case.accepted["turn_id"]))
    assert case.source_rows() == states
    assert case.pending_rows() == []
    assert canonical_json_bytes(case.snapshot(second).to_dict()) == snapshots
    assert case.snapshot(second).seal_binding.prefix.state.epoch.ordinal == 1


@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
def test_history_replay_preserves_existing_pending_transition(epoch_case, boundary):
    case = epoch_case
    second = case.advance()
    if boundary == "rewind":
        case.rewind("checkpoint-first")
    else:
        case.checkpoint("checkpoint-history-compacted", compaction=True,
                        messages=[case.user, AIMessage(content="历史回放前的摘要", id="history-summary")])
    pending, states = case.pending_rows(), case.source_rows()
    selected, dispatch = case.selected_rows(), case.dispatch_rows()
    snapshots = tuple(canonical_json_bytes(case.snapshot(prepared).to_dict())
                      for prepared in (case.first, second))
    branches = case.connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall()
    view = case.active_view()
    assert pending[0][2:] == (boundary, "pending", None)
    with case.scope():
        history_config = case.saver.history_replay(case.config, turn_id=str(case.accepted["turn_id"]))
        history = case.saver.get_tuple(history_config)
        assert history is not None and history.config["configurable"]["history_view_id"] == history_config["configurable"]["history_view_id"]
    assert case.pending_rows() == pending
    assert case.source_rows() == states
    assert case.selected_rows() == selected
    assert case.dispatch_rows() == dispatch
    assert tuple(canonical_json_bytes(case.snapshot(prepared).to_dict())
                 for prepared in (case.first, second)) == snapshots
    assert case.snapshot(second).seal_binding.prefix.state.epoch.ordinal == 1
    assert case.active_view() == view
    assert case.connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall() == branches
    previous_turn = case.accepted["turn_id"]
    assert case.saver.mark_turn_terminal_status(
        session_id=case.owner.session_id, thread_id=case.owner.thread_id,
        turn_id=str(previous_turn), status="cancelled",
    )
    case.accepted = case.saver.accept_turn(
        case.owner.session_id, thread_id=case.owner.thread_id,
        accepted_ingress_id="history-next:" + boundary,
        acceptance_idempotency_key="history-next:accept:" + boundary,
        job_id="history-next:job:" + boundary,
        admission_idempotency_key="history-next:admit:" + boundary,
        payload="历史展示后的真实新用户轮", root_item_id="item-history-next-" + boundary,
    )
    assert case.accepted["turn_id"] != previous_turn
    key = "history-next:seal:" + boundary
    prepared = case.prepare(key)
    rebuilt = case.snapshot(prepared)
    assert rebuilt.turn_id == case.accepted["turn_id"]
    assert rebuilt.execution_id == case.accepted["initial_execution_id"]
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == 2
    assert rebuilt.seal_binding.prefix.consumed_transition.transition_id == pending[0][0]
    assert case.pending_rows()[0][3:] == ("consumed", rebuilt.assembly_id)
    case.restart()

    def refuse_live_inputs(*_args):
        pytest.fail("历史展示后真实新 Turn 的已封存重试不得再冻结实时输入")

    case.manager.bind_published_revision_supplier(refuse_live_inputs)
    retried = case.prepare(key, supplier=refuse_live_inputs)
    assert retried["assembly_id"] == prepared["assembly_id"]
    assert canonical_json_bytes(case.snapshot(retried).to_dict()) == canonical_json_bytes(rebuilt.to_dict())


def test_replay_before_as_new_turn_seals_real_admission_and_reuses_after_restart(epoch_case):
    case = epoch_case
    source_turn = str(case.accepted["turn_id"])
    source_root = str(case.accepted["root_input_item_id"])
    source_execution = str(case.accepted["initial_execution_id"])
    binding = case.load_snapshot()
    visible = case.prepare("replay-before-source-snapshot")
    original = case.snapshot(visible)
    old_snapshots = tuple(canonical_json_bytes(case.snapshot(prepared).to_dict())
                          for prepared in (case.first, visible))
    case.checkpoint("checkpoint-replay-before", messages=[
        case.user, AIMessage(content="重放前的真实回答", id="replay-before-answer"),
    ])
    assert case.connection.execute(
        "SELECT source_assembly_id FROM checkpoint_context_source_snapshots WHERE checkpoint_id=?",
        ("checkpoint-replay-before",),
    ).fetchone() == (original.assembly_id,)
    assert case.saver.mark_turn_terminal_status(
        session_id=case.owner.session_id, thread_id=case.owner.thread_id,
        turn_id=source_turn, status="cancelled",
    )
    (old_root,) = case.saver._storage.storage.read_items(
        case.owner.session_id, case.owner.thread_id, item_ids=(source_root,),
    )
    old_root_bytes = canonical_json_bytes(old_root.to_dict())
    versions = case.checkpoint_versions()
    with case.scope():
        anchor, prefix_messages = case.saver.materialize_turn_anchor(
            case.config, turn_id=source_turn, anchor_mode="before",
        )
        assert anchor.checkpoint_id == "checkpoint-replay-before"
        assert anchor.state_header_checkpoint_id == "checkpoint-first"
        assert prefix_messages == []
        case.config = case.saver.rewind_to_turn(case.config, turn_id=source_turn, anchor_mode="before")
    (pending,) = case.pending_rows()
    assert pending[2:] == ("rewind", "pending", None)
    cut = ("before_canonical", source_root)
    assert case.connection.execute(
        "SELECT canonical_cut_kind, canonical_cut_item_id FROM context_epoch_transitions WHERE transition_id=?",
        (pending[0],),
    ).fetchone() == cut
    assert {row[0]: row[1:4] for row in case.source_rows()}["skill:one-shot"] == (
        "untracked", binding.activation_revision, None,
    )
    # 真实 replay port 分配新 Turn/root/admission，旧 identity 只进入 lineage。
    case.accepted = case.saver.replay_as_new_turn(
        case.owner.session_id, thread_id=case.owner.thread_id, source_turn_id=source_turn,
        acceptance_idempotency_key="epoch-replay-before:accept",
    )
    replay = case.accepted
    assert replay["operation"] == "replay_as_new_turn" and replay["replay_of_turn_id"] == source_turn
    assert replay["turn_id"] != source_turn
    assert replay["root_input_item_id"] != source_root
    assert replay["initial_execution_id"] != source_execution
    admission = case.saver.execution_admission(
        case.owner.session_id, thread_id=case.owner.thread_id,
        execution_id=str(replay["initial_execution_id"]),
    )
    assert (admission.session_id, admission.thread_id, admission.execution_origin, admission.turn_id) == (
        case.owner.session_id, case.owner.thread_id, "user_turn", replay["turn_id"],
    )
    assert case.connection.execute(
        "SELECT root_input_item_id, initial_execution_id, replay_of_turn_id, status "
        "FROM turn_records WHERE turn_id=?", (replay["turn_id"],),
    ).fetchone() == (replay["root_input_item_id"], admission.execution_id, source_turn, "open")
    assert case.connection.execute(
        "SELECT COUNT(*) FROM model_calls WHERE execution_id=?", (admission.execution_id,),
    ).fetchone() == (0,)
    (new_root,) = case.saver._storage.storage.read_items(
        case.owner.session_id, case.owner.thread_id, item_ids=(str(replay["root_input_item_id"]),),
    )
    assert (new_root.payload_kind, new_root.payload) == (old_root.payload_kind, old_root.payload)
    assert (new_root.turn_id, new_root.turn_scope) == (replay["turn_id"], "turn_root")
    assert case.connection.execute(
        "SELECT membership.item_id FROM context_view_items AS membership "
        "JOIN item_catalog AS item ON item.item_id=membership.item_id "
        "WHERE membership.view_id=? AND membership.visible=1 AND item.semantic_kind='user_input'",
        (case.active_view(),),
    ).fetchall() == [(replay["root_input_item_id"],)]
    lineage = [(f"{case.owner.thread_id}:turn:{source_turn}",
                f"{case.owner.thread_id}:turn:{replay['turn_id']}", 1)]
    assert case.connection.execute(
        "SELECT source_ref, target_ref, replay_input FROM item_relations WHERE relation='replay_input'",
    ).fetchall() == lineage
    assert case.connection.execute(
        "SELECT canonical_cut_kind, canonical_cut_item_id FROM context_epoch_transitions WHERE transition_id=?",
        (pending[0],),
    ).fetchone() == cut
    case.publisher.publish(binding.display_uri, "snapshot:wrong", "重放不得读取的旧来源", available=False)
    case.publisher.publish(RULES_URI, "rules:replay", "重放时已发布的基础规则")
    case.publisher.reads.clear()
    key = "replay-before-new-turn"
    prepared = case.prepare(key)
    rebuilt = case.snapshot(prepared)
    assert (rebuilt.turn_id, rebuilt.execution_id) == (replay["turn_id"], admission.execution_id)
    assert rebuilt.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert rebuilt.seal_binding.prefix.state.epoch.ordinal == original.seal_binding.prefix.state.epoch.ordinal + 1
    assert rebuilt.seal_binding.prefix.consumed_transition.transition_id == pending[0]
    assert rebuilt.seal_binding.prefix.consumed_transition.consumed
    assert json.loads(rebuilt.seal_binding.root.content)["sources"][0]["body"] == "重放时已发布的基础规则"
    selected = {entry.ref.ref_id for entry in rebuilt.selection
                if entry.included and entry.ref.ref_type == "canonical_item"}
    assert replay["root_input_item_id"] in selected and source_root not in selected
    assert all(message.content != binding.body for message in prepared["messages"])
    assert all(ref.source_identity != "skill:one-shot"
               for frame in rebuilt.seal_binding.prefix.state.frames for ref in frame.item_refs)
    assert {source.source_id: source.revision for source in rebuilt.seal_binding.source_revisions} == {
        "workspace:rules": "rules:replay", "skill:debugging": "skill:1",
    }
    consumed, states = case.pending_rows(), case.source_rows()
    assert consumed[0][3:] == ("consumed", rebuilt.assembly_id)
    assert case.publisher.reads == [RULES_URI, SKILL_URI]
    assert case.manager.prepare_pending() is None
    assert case.checkpoint_versions() == versions
    assert tuple(canonical_json_bytes(case.snapshot(value).to_dict()) for value in (case.first, visible)) == old_snapshots
    assert canonical_json_bytes(case.saver._storage.storage.read_items(
        case.owner.session_id, case.owner.thread_id, item_ids=(source_root,),
    )[0].to_dict()) == old_root_bytes
    sealed_bytes = canonical_json_bytes(rebuilt.to_dict())
    assemblies = case.connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall()
    case.restart()
    case.publisher.reads.clear()

    def refuse_live_inputs(*_args):
        pytest.fail("真实 replay 新 Turn 的 sealed 重试不得再读取来源或重新编译 ToolSet")

    case.manager.bind_published_revision_supplier(refuse_live_inputs)
    retried = case.prepare(key, supplier=refuse_live_inputs)
    assert retried["assembly_id"] == prepared["assembly_id"]
    assert canonical_json_bytes(case.snapshot(retried).to_dict()) == sealed_bytes
    assert case.publisher.reads == []
    assert case.pending_rows() == consumed and case.source_rows() == states
    assert case.checkpoint_versions() == versions
    assert case.connection.execute("SELECT assembly_id FROM context_assemblies ORDER BY rowid").fetchall() == assemblies
    assert case.connection.execute(
        "SELECT source_ref, target_ref, replay_input FROM item_relations WHERE relation='replay_input'",
    ).fetchall() == lineage
    assert case.connection.execute(
        "SELECT root_input_item_id, initial_execution_id, status, replay_of_turn_id "
        "FROM turn_records WHERE turn_id=?", (source_turn,),
    ).fetchone() == (source_root, source_execution, "cancelled", None)


def test_tool_group_cut_rejects_rewind_before_view_control_or_pending_change(epoch_case):
    case = epoch_case
    call = AIMessage(content="", id="epoch-call", tool_calls=[{"name": "read_file", "args": {}, "id": "epoch-tool"}])
    result = ToolMessage(content="完成", id="epoch-result", tool_call_id="epoch-tool", name="read_file")
    case.checkpoint("checkpoint-tools", messages=[case.user, call, result])
    states, pending, view = case.source_rows(), case.pending_rows(), case.active_view()
    branches = case.connection.execute("SELECT branch_id, status FROM branches ORDER BY rowid").fetchall()
    with pytest.raises(ToolProtocolBoundaryConflict, match="tool-protocol-boundary-conflict"):
        case.rewind("checkpoint-tools", anchor=call.id)
    assert case.active_view() == view
    assert case.source_rows() == states
    assert case.pending_rows() == pending
    assert case.connection.execute("SELECT branch_id, status FROM branches ORDER BY rowid").fetchall() == branches


def test_desired_provider_profile_switch_requires_actual_rebuild(epoch_case):
    case = epoch_case
    first_snapshot = canonical_json_bytes(case.snapshot(case.first).to_dict())
    dispatch, applied = case.dispatch_rows(), case.source_rows()
    with pytest.raises(ProviderProfileChangeRequiresRebuildError):
        case.prepare("profile-only", model="desired-provider")
    assert case.pending_rows() == []
    assert case.snapshot(case.first).seal_binding.prefix.state.epoch.ordinal == 1
    assert canonical_json_bytes(case.snapshot(case.first).to_dict()) == first_snapshot
    assert case.dispatch_rows() == dispatch
    assert case.source_rows() == applied
    case.rewind("checkpoint-first")
    rebuilt = case.prepare("profile-rewind", model="desired-provider")
    snapshot = case.snapshot(rebuilt)
    assert snapshot.seal_binding.prefix.state.epoch.reason == EpochReason.REWIND
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 2
    assert snapshot.seal_binding.prefix.state.provider_profile != case.snapshot(case.first).seal_binding.prefix.state.provider_profile
    desired_wire = case.wire("desired-provider", "chat_completions")
    assert snapshot.provider_version == desired_wire["provider_version"]
    assert snapshot.seal_binding.prefix.state.provider_profile == "chat_completions:" + desired_wire["provider_version"]
    assert canonical_json_bytes(case.snapshot(case.first).to_dict()) == first_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("source_phase", ["pending", "consumed", "continued"])
async def test_full_copy_preserves_original_versions_transition_and_prefix(
    epoch_case, session_bundle_factory, monkeypatch, source_phase, fork_deadlines,
):
    case = epoch_case
    prepared = [case.first, case.advance()]
    case.rewind("checkpoint-first")
    if source_phase != "pending":
        prepared.append(case.prepare("source-rebuild"))
    if source_phase == "continued":
        prepared.append(case.prepare("source-continued"))
    originals = tuple(case.snapshot(value) for value in prepared)
    original_bytes = tuple(canonical_json_bytes(value.to_dict()) for value in originals)
    source_states, source_pending = case.source_rows(), case.pending_rows()
    source_versions = case.checkpoint_versions()
    source_checkpoint_ids = {row[0] for row in source_versions[0]}
    source_control_payloads = case.connection.execute(
        "SELECT * FROM context_source_control_states ORDER BY source_id"
    ).fetchall()
    # fixture 的原 Turn 尚未 final；full copy 保留原历史状态，目标不能续 dispatch。
    assert case.connection.execute(
        "SELECT final_item_id FROM turn_records WHERE turn_id=?", (case.accepted["turn_id"],),
    ).fetchone() == (None,)
    source_reads = tuple(case.publisher.reads)
    source_jsonl = case.saver._storage.storage.jsonl_path(
        case.owner.session_id, "", thread_id=case.owner.thread_id,
    )
    original_jsonl = source_jsonl.read_bytes()
    target_session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(case.saver._storage.storage.sessions_dir, target_session)
    await copy_rollout_snapshot(
        case.saver, case.owner.session_id, target_session, fork_deadlines=fork_deadlines,
    )
    with RolloutCheckpointSaver(case.saver._storage.storage.sessions_dir) as saver:
        target_thread = saver._storage.storage.main_thread_id(target_session)
        with saver._storage.storage.read_connection(target_session, target_thread, "") as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT * FROM context_source_control_states ORDER BY source_id").fetchall() == source_control_payloads
            versions = tuple(connection.execute(query).fetchall() for query in (
                "SELECT * FROM checkpoint_context_source_snapshots ORDER BY checkpoint_id",
                "SELECT * FROM checkpoint_context_source_states ORDER BY checkpoint_id,source_id",
            ))
            assert [row for row in versions[0] if row[0] in source_checkpoint_ids] == list(source_versions[0])
            assert [row for row in versions[1] if row[0] in source_checkpoint_ids] == list(source_versions[1])
            (target_head,) = connection.execute(
                "SELECT branch.head_checkpoint_id FROM checkpoint_namespace_state AS namespace "
                "JOIN branches AS branch ON branch.branch_id=namespace.active_branch_id "
                "WHERE namespace.checkpoint_ns=''"
            ).fetchone()
            assert target_head not in source_checkpoint_ids
            target_head_headers = tuple(row for row in versions[0] if row[0] == target_head)
            assert len(target_head_headers) == 1
            assert target_head_headers[0][1:6] == ("", target_session, target_thread, 0, None)
            assert all(row[0] != target_head for row in versions[1])
            assert len(versions[0]) == len(source_checkpoint_ids) + 1
            assert connection.execute("SELECT transition_id,target_view_id,source_checkpoint_id,status,consumed_assembly_id FROM context_epoch_transitions").fetchall() == case.connection.execute("SELECT transition_id,target_view_id,source_checkpoint_id,status,consumed_assembly_id FROM context_epoch_transitions").fetchall()
        for original in originals:
            restored = saver.get_context_assembly(target_session, target_thread, assembly_id=original.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(original.to_dict())
        with pytest.raises(ValueError, match="turn_not_resumable"):
            saver.dispatch_replay(target_session, thread_id=target_thread, turn_id=case.accepted["turn_id"])
    assert source_jsonl.read_bytes() == original_jsonl
    assert tuple(canonical_json_bytes(case.snapshot(value).to_dict()) for value in prepared) == original_bytes
    assert case.source_rows() == source_states and case.pending_rows() == source_pending
    assert case.checkpoint_versions() == source_versions
    assert tuple(case.publisher.reads) == source_reads
