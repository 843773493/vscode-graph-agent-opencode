"""canonical 准入、SQLite 队列投影和显式 v5 迁移共用同一 owner。"""
from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.identifier import create_prefixed_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.prompting import internal_message_factory
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    EXECUTION_QUEUE_SCHEMA_SQL,
    SKILL_SELECTION_MIGRATION_SQL,
    VIEW_CREATION_MIGRATION_SQL,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema24,
)
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


@dataclass
class StoreCase:
    workspace: CatalogWorkspaceContext
    saver: RolloutCheckpointSaver
    messages: MessageService
    store: PendingRequestStore
    session: str
    thread: str

    def accept(self, text: str = "不可变正文", *, internal: bool = False):
        now = datetime.now(UTC)
        identity = JobService.reserve_user_execution_identity()
        message_id = create_prefixed_id("msg")
        binding = (InternalExecutionIntent(message_id, create_prefixed_id("exec"), identity.admission_idempotency_key)
                   if internal else identity)
        frozen = ExecutionQueueInput(binding, message_id, "default", "frozen-provider", "after_turn",
                                     "frozen-gateway", "frozen-request", now.isoformat())
        if internal:
            prepared = internal_message_factory.build(kind="checkpoint_reminder", control=text)
            self.saver.admit_internal_execution(self.session, thread_id=self.thread, job_id=identity.job_id,
                intent=binding, queue_input=frozen, message=HumanMessage(id=message_id, content=prepared.content,
                    response_metadata={"message_id": message_id, "created_at": now.isoformat(), "message_metadata": prepared.metadata}))
        else:
            self.messages.accept_user_message(MessageDTO(message_id=message_id, session_id=self.session,
                thread_id=self.thread, role="user", content=text, created_at=now, updated_at=now),
                execution_identity=identity, queue_input=frozen)
        return self.saver.execution_queue_record(self.session, self.thread, identity.job_id)

    def legacy(self, records) -> Path:
        """只为显式旧格式迁移准备输入；关键 acceptance 仍由产品入口生成。"""
        controls = [self.store._control(record, position=index, snapshot_version=7).model_dump(mode="json")
                    | {"provider_id": record.input.provider_id} for index, record in enumerate(records)]
        path = self.workspace.session_dir(self.session) / "pending_requests.json"
        path.write_text(json.dumps({"version": 5, "request_count": len(controls), "snapshot_version": 7})
                        + "\n" + json.dumps(controls) + "\n")
        with self.saver._storage.storage._connect(self.thread, session_id=self.session) as connection:
            connection.execute("DELETE FROM execution_queue")
            connection.commit()
        return path


@pytest.fixture
async def store_case(request: pytest.FixtureRequest) -> AsyncIterator[StoreCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    if context.workspace_root.exists():
        shutil.rmtree(context.workspace_root)
    workspace = build_catalog_workspace(context.output_root, workspace_id=load_or_create_workspace_id(context.workspace_root))
    session = await workspace.create_session("执行队列存储验收")
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    messages = MessageService(main_thread_resolver=workspace.resolver.main_thread_id, checkpointer=saver,
                              workspace_root=workspace.workspace_root)
    store = PendingRequestStore(sessions_dir=workspace.sessions_root, owner=saver,
        input_reader=lambda reference: messages.accepted_job_message(reference, display=False))
    try:
        yield StoreCase(workspace, saver, messages, store, session, workspace.resolver.main_thread_id(session))
    finally:
        workspace.close()


@pytest.mark.parametrize("internal", [False, True])
async def test_cold_projection_reads_canonical_body_and_frozen_identity(store_case: StoreCase, internal: bool):
    case = store_case
    record = case.accept(internal=internal)
    loaded, = await case.store.load(case.session)
    assert (loaded.job_id, loaded.execution_binding, loaded.message_id) == (
        record.cursor.admission.job_id, record.input.execution_binding, record.input.message_id)
    assert (loaded.gateway_id, loaded.request_id, loaded.message_metadata["boxteam_session_provider_id"]) == (
        "frozen-gateway", "frozen-request", "frozen-provider")
    assert loaded.turn_id == record.cursor.admission.turn_id
    assert loaded.content == case.messages.accepted_job_message(loaded, display=False).content
    with case.saver._storage.storage.read_connection(case.session, case.thread) as connection:
        serialized, = connection.execute("SELECT input_json FROM execution_queue").fetchone()
    assert "不可变正文" not in serialized and "content" not in json.loads(serialized)


async def test_summaries_never_read_body_and_policy_is_durable_cas(store_case: StoreCase):
    case = store_case
    records = [case.accept(f"输入 {index}") for index in range(10)]
    def reject(_reference):
        raise AssertionError("摘要不应读取 canonical 正文")
    cold = PendingRequestStore(sessions_dir=case.workspace.sessions_root, owner=case.saver, input_reader=reject)
    summary = await cold.load_summaries(case.session, thread_id=case.thread, limit=2)
    assert summary.request_count == 10 and summary.truncated
    assert [item.enqueue_sequence for item in summary.requests] == [record.cursor.commit_id for record in records[:2]]
    reference = case.store._control(records[0], position=0, snapshot_version=summary.snapshot_version)
    await cold.update_policy(reference, "after_interrupt", expected_snapshot_version=summary.snapshot_version)
    with pytest.raises(RuntimeError, match="过期"):
        await cold.update_policy(reference, "after_turn", expected_snapshot_version=summary.snapshot_version)
    assert (await cold.record(case.session, case.thread, reference.job_id)).delivery_policy == "after_interrupt"


async def test_holder_generation_and_terminal_commit_protect_same_fifo(store_case: StoreCase):
    case = store_case
    first, second = case.accept(), case.accept("后继")
    claim = await case.store.claim(first)
    assert claim is not None
    try:
        assert await case.store.claim(second) is None
        await claim.start()
    finally:
        await claim.close()
    current = await case.store.record(case.session, case.thread, first.cursor.admission.job_id)
    recovered = await case.store.claim(current)
    assert recovered is not None and recovered.record.claim_generation == claim.record.claim_generation + 1
    try:
        with pytest.raises(RuntimeError, match="失效"):
            case.saver.start_execution_queue(case.session, case.thread, job_id=first.cursor.admission.job_id,
                claim_owner=claim.record.claim_owner, claim_generation=claim.record.claim_generation)
        case.saver.mark_job_terminal_status(session_id=case.session, thread_id=case.thread,
            job_id=first.cursor.admission.job_id, status="cancelled")
        assert [row.cursor.admission.job_id for row in await case.store.records()] == [second.cursor.admission.job_id]
    finally:
        await recovered.close()
    successor = await case.store.claim(second)
    assert successor is not None
    await successor.close()


@pytest.mark.parametrize("internal", [False, True])
async def test_v5_migration_reuses_original_admission_and_is_idempotent(store_case: StoreCase, internal: bool):
    case = store_case
    original = case.accept(internal=internal)
    path = case.legacy([original])
    with pytest.raises(RuntimeError, match="migrate_schema"):
        await case.store.records()
    assert await case.store.migrate_all() == 1
    assert not path.exists() and await case.store.migrate_all() == 0
    recovered, = await case.store.records()
    assert recovered.cursor == original.cursor and recovered.input == original.input


@pytest.mark.parametrize("failure", ["version", "binding", "hash", "order", "identity"])
async def test_v5_invalid_input_preserves_original_and_publishes_nothing(store_case: StoreCase, failure: str):
    case = store_case
    records = [case.accept(), case.accept("后继")]
    path = case.legacy(records)
    header, detail = path.read_text().split("\n", 1)
    metadata, values = json.loads(header), json.loads(detail)
    if failure == "version":
        metadata["version"] = 4
    elif failure == "binding":
        values[0].pop("execution_binding")
    elif failure == "hash":
        values[0]["execution_binding"]["admission_idempotency_key"] = "wrong-admission"
    elif failure == "order":
        values.reverse()
    else:
        values[0]["thread_id"] = create_prefixed_id("thr")
    path.write_text(json.dumps(metadata) + "\n" + json.dumps(values) + "\n")
    original = path.read_bytes()
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        await case.store.migrate_schema(case.session)
    assert path.read_bytes() == original
    assert case.saver.execution_queue_snapshot(case.session, case.thread).records == ()


async def test_schema_23_upgrade_then_v5_import_preserves_original_bytes(store_case: StoreCase):
    case = store_case
    record = case.accept()
    path = case.legacy([record])
    storage = case.saver._storage.storage
    original = storage.jsonl_path(case.session, thread_id=case.thread).read_bytes()
    # 模拟已支持的上版结构；历史内容仍来自真实 acceptance。
    import hashlib
    with storage._connect(case.thread, session_id=case.session) as connection:
        restore_schema24(connection)
        connection.executescript("DROP TRIGGER execution_queue_terminal; DROP TRIGGER execution_queue_insert_revision; "
            "DROP TRIGGER execution_queue_update_revision; DROP TABLE execution_queue; DROP TABLE execution_queue_revision;")
        connection.execute("UPDATE database_meta SET schema_version=23")
        connection.execute("UPDATE schema_migrations SET to_version=23,migration_name='rollout_sqlite_v23',migration_checksum=?",
                           (hashlib.sha256(b"rollout_sqlite_v23").hexdigest(),))
        connection.commit()
    storage.migrate_schema(case.thread, session_id=case.session, to_version=24,
                           migration_name="execution_queue_023_024", migration_sql=EXECUTION_QUEUE_SCHEMA_SQL)
    storage.migrate_schema(case.thread, session_id=case.session, to_version=25,
                           migration_name="skill_selection_024_025", migration_sql=SKILL_SELECTION_MIGRATION_SQL)

    storage.migrate_schema(
        case.thread, session_id=case.session, to_version=26,
        migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
    )
    await case.store.migrate_schema(case.session)
    assert not path.exists() and storage.jsonl_path(case.session, thread_id=case.thread).read_bytes() == original
    recovered, = await case.store.records()
    assert recovered.cursor == record.cursor


async def test_missing_frozen_queue_rejects_startup_instead_of_losing_old_active(store_case: StoreCase):
    case = store_case
    record = case.accept()
    storage = case.saver._storage.storage
    original = storage.jsonl_path(case.session, thread_id=case.thread).read_bytes()
    with storage._connect(case.thread, session_id=case.session) as connection:
        connection.execute("DELETE FROM execution_queue")
        connection.commit()
    with pytest.raises(RuntimeError, match="缺少冻结队列输入"):
        case.saver.validate_execution_queue(case.session, case.thread)
    with pytest.raises(RuntimeError, match="artifact=execution_queue") as rejected:
        await case.store.records()
    assert "缺少冻结队列输入" in str(rejected.value.__cause__)
    assert str(storage.root(case.session, thread_id=case.thread)) not in str(rejected.value)
    assert storage.jsonl_path(case.session, thread_id=case.thread).read_bytes() == original
    # 原程序明确结算后才可继续，不能由恢复检查擅自取消或重建原执行。
    admission = record.cursor.admission
    assert case.saver.execution_admission_for_job(case.session, thread_id=case.thread,
        job_id=admission.job_id) == admission
    case.saver.mark_job_terminal_status(session_id=case.session, thread_id=case.thread,
        job_id=admission.job_id, status="cancelled")
    assert await case.store.records() == ()


async def test_control_error_exposes_logical_owner_without_native_path(store_case: StoreCase, monkeypatch):
    case = store_case
    case.accept()
    native = str(case.workspace.session_dir(case.session) / "private-index.sqlite")

    def reject(*args):
        raise OSError(5, "队列读取失败", native)

    monkeypatch.setattr(case.saver, "execution_queue_snapshot", reject)
    with pytest.raises(OSError) as failure:
        await case.store.records()
    assert case.session in str(failure.value) and case.thread in str(failure.value)
    assert "artifact=execution_queue" in str(failure.value) and "errno=5" in str(failure.value)
    assert native not in str(failure.value) and "private-index.sqlite" not in str(failure.value)
    assert failure.value.__cause__.filename == native
