"""通过 Saver 和真实 SQLite/JSONL 验证恢复前的提交边界检查。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class CommittedRollout:
    sessions: Path
    session_id: str
    root: Path
    committed: bytes

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def jsonl(self) -> Path:
        return self.root / "rollout.jsonl"


@pytest.fixture
def committed_rollout(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
    frozen_provider_wire,
    frozen_provider_activation,
) -> CommittedRollout:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(sessions)
    owner = saver.forks.source.resolve_main_thread_owner(session_id)
    accepted = saver.accept_turn(
        session_id,
        thread_id=owner.thread_id,
        accepted_ingress_id="ingress-1",
        acceptance_idempotency_key="acceptance-1",
        job_id="job-1",
        admission_idempotency_key="admission-1",
        payload="已提交的用户输入不能在损坏恢复时丢失",
        payload_kind=PayloadKind.TEXT,
    )
    admission = saver.execution_admission(
        session_id, thread_id=owner.thread_id, execution_id=str(accepted["initial_execution_id"]),
    )
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
        prepared = saver.prepare_context_for_provider(
            session_id, owner.thread_id, turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key="commit-recovery-create", seal_idempotency_key="commit-recovery-seal",
            context_source_manager=ContextSourceManager(
                owner=ContextSourceOwnerKey(session_id, owner.thread_id), control_state_port=saver,
            ),
            tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            **frozen_provider_wire("commit-recovery", "chat_completions"),
            **frozen_provider_activation(saver, plan_creation_idempotency_key="commit-recovery-create"),
        )
        saver.register_model_call(
            session_id, thread_id=owner.thread_id, execution_id=admission.execution_id,
            model_call_id=prepared["snapshot"].model_call_id, attempt=1,
            provider="commit-recovery", assembly_id=prepared["assembly_id"],
        )
        saver.update_model_call_outcome(
            session_id, thread_id=owner.thread_id, model_call_id=prepared["snapshot"].model_call_id,
            outcome="completed_empty", dispatch_state="completed",
        )
    saver.converge_execution(
        session_id,
        thread_id=owner.thread_id,
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        outcome="completed_empty",
        turn_status="completed_empty",
    )
    root = saver._storage.storage.root(session_id, thread_id=owner.thread_id)
    return CommittedRollout(
        sessions, session_id, root, (root / "rollout.jsonl").read_bytes()
    )


@pytest.mark.parametrize("validate_jsonl_items", [True, False])
@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE database_meta SET committed_jsonl_offset = 0",
        "UPDATE database_meta SET committed_jsonl_offset = committed_jsonl_offset - 1",
        "UPDATE database_meta SET last_commit_id = last_commit_id - 1",
        (
            "UPDATE storage_commits SET jsonl_offset_before = jsonl_offset_before + 1 "
            "WHERE commit_kind = 'terminal_convergence'"
        ),
        (
            "UPDATE storage_commits SET jsonl_offset_after = jsonl_offset_after + 1 "
            "WHERE commit_kind = 'terminal_convergence'"
        ),
        (
            "UPDATE storage_commits SET jsonl_record_count = 1 "
            "WHERE commit_kind = 'terminal_convergence'"
        ),
        (
            "UPDATE storage_commits SET jsonl_fsync_at = NULL "
            "WHERE commit_kind = 'acceptance'"
        ),
        "DELETE FROM storage_commits WHERE commit_kind = 'terminal_convergence'",
    ],
    ids=[
        "zero-meta",
        "lower-meta",
        "last-commit",
        "chain-before",
        "chain-after",
        "metadata-count",
        "missing-barrier",
        "missing-commit",
    ],
)
def test_corrupt_commit_boundary_preserves_all_jsonl_bytes(
    committed_rollout: CommittedRollout,
    corruption: str,
    validate_jsonl_items: bool,
) -> None:
    rollout = committed_rollout
    # 尾部即使尚未收敛，也应在边界验证失败时保留，供后续恢复审计。
    with rollout.jsonl.open("ab") as stream:
        stream.write(b'{"uncommitted":')
    before = rollout.jsonl.read_bytes()
    with sqlite3.connect(rollout.index) as connection:
        connection.execute(corruption)
    restarted = RolloutCheckpointSaver(rollout.sessions)
    with pytest.raises(RuntimeError):
        restarted._storage.storage.initialize(
            rollout.session_id, validate_jsonl_items=validate_jsonl_items
        )
    assert rollout.jsonl.read_bytes() == before


@pytest.mark.parametrize("validate_jsonl_items", [True, False])
def test_valid_commit_chain_reclaims_only_uncommitted_tail(
    committed_rollout: CommittedRollout,
    validate_jsonl_items: bool,
) -> None:
    rollout = committed_rollout
    with rollout.jsonl.open("ab") as stream:
        stream.write(b'{"unfinished":')
    restarted = RolloutCheckpointSaver(rollout.sessions)
    restarted._storage.storage.initialize(
        rollout.session_id, validate_jsonl_items=validate_jsonl_items
    )
    assert rollout.jsonl.read_bytes() == rollout.committed
    with sqlite3.connect(rollout.index) as connection:
        assert connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta"
        ).fetchone() == (len(rollout.committed),)
        assert connection.execute(
            "SELECT status, final_item_id FROM turn_records"
        ).fetchone() == ("completed_empty", None)
        commits = connection.execute(
            "SELECT commit_kind, commit_mode, jsonl_offset_before, "
            "jsonl_offset_after, jsonl_record_count FROM storage_commits "
            "ORDER BY commit_id"
        ).fetchall()
    assert commits == [
        ("acceptance", "item_bearing", 0, len(rollout.committed), 1),
        ("assembly_sealed", "metadata_only", len(rollout.committed), len(rollout.committed), 0),
        (
            "terminal_convergence",
            "metadata_only",
            len(rollout.committed),
            len(rollout.committed),
            0,
        ),
    ]


@pytest.mark.parametrize("operation", ["initialize", "snapshot"])
@pytest.mark.parametrize(("corruption", "match"), [
    (
        "UPDATE storage_commits SET jsonl_record_count = 2 WHERE commit_kind = 'acceptance'",
        "item catalog 数量不一致",
    ),
    (
        "UPDATE storage_commits SET metadata_json = '[1]' WHERE commit_kind = 'terminal_convergence'",
        "metadata_json 不是 RFC 8785 JCS object",
    ),
    (
        "UPDATE storage_commits SET metadata_json = '{\"compacted\":true}' WHERE commit_kind = 'terminal_convergence'",
        "immutable JSONL 不允许",
    ),
])
def test_warm_owner_revalidates_commit_catalog_and_metadata_before_reclaiming_tail(
    committed_rollout: CommittedRollout, operation: str, corruption: str, match: str,
) -> None:
    rollout = committed_rollout
    storage = RolloutCheckpointSaver(rollout.sessions)._storage.storage
    thread_id = storage.main_thread_id(rollout.session_id)
    with storage.open_read_snapshot(rollout.session_id, thread_id) as snapshot:
        assert snapshot.session_id == rollout.session_id
    # 已成功读过的 owner 也必须重新校验权威记录；复用不能跨调用掩盖后续损坏。
    with sqlite3.connect(rollout.index) as connection:
        connection.execute(corruption)
    with rollout.jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = rollout.jsonl.read_bytes()
    with pytest.raises(RuntimeError, match=match):
        if operation == "initialize":
            storage.initialize(rollout.session_id, thread_id=thread_id)
        else:
            with storage.open_read_snapshot(rollout.session_id, thread_id):
                pytest.fail("后续 commit/catalog 损坏不得复用先前的校验结果")
    assert rollout.jsonl.read_bytes() == before


def test_invalid_catalog_preserves_committed_body_and_tail(
    committed_rollout: CommittedRollout,
) -> None:
    rollout = committed_rollout
    with rollout.jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = rollout.jsonl.read_bytes()
    with sqlite3.connect(rollout.index) as connection:
        connection.execute("UPDATE item_catalog SET jsonl_length = jsonl_length - 1")
    restarted = RolloutCheckpointSaver(rollout.sessions)
    with pytest.raises(RuntimeError, match="JCS line|无法解码|locator|不一致"):
        restarted._storage.storage.initialize(rollout.session_id)
    assert rollout.jsonl.read_bytes() == before


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("UPDATE item_catalog SET source_revision = ''", "source_revision"),
        ("UPDATE item_catalog SET source_revision = 'unrelated'", "source_revision"),
        (
            "UPDATE item_catalog SET payload_length = payload_length + 1",
            "payload_length",
        ),
    ],
)
def test_startup_rejects_catalog_manifest_corruption_without_repair(
    committed_rollout: CommittedRollout,
    corruption: str,
    match: str,
) -> None:
    rollout = committed_rollout
    with sqlite3.connect(rollout.index) as connection:
        connection.execute(corruption)
        damaged_catalog = connection.execute("SELECT * FROM item_catalog").fetchall()
        damaged_projection = connection.execute(
            "SELECT * FROM item_projections"
        ).fetchall()
    with rollout.jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = rollout.jsonl.read_bytes()
    with pytest.raises(RuntimeError, match=match):
        RolloutCheckpointSaver(rollout.sessions)._storage.storage.initialize(rollout.session_id)
    assert rollout.jsonl.read_bytes() == before
    with sqlite3.connect(rollout.index) as connection:
        assert (
            connection.execute("SELECT * FROM item_catalog").fetchall()
            == damaged_catalog
        )
        assert (
            connection.execute("SELECT * FROM item_projections").fetchall()
            == damaged_projection
        )


def test_startup_does_not_rebuild_optional_item_projections(
    committed_rollout: CommittedRollout,
) -> None:
    rollout = committed_rollout
    with sqlite3.connect(rollout.index) as connection:
        item_id = connection.execute("SELECT item_id FROM item_catalog").fetchone()[0]
        connection.execute("DELETE FROM item_projections")
    storage = RolloutCheckpointSaver(rollout.sessions)._storage
    storage.storage.initialize(rollout.session_id)
    # 合同允许只保留最小 catalog；启动不创建第二套事实，也不自动重建可选索引。
    assert rollout.jsonl.read_bytes() == rollout.committed
    assert storage.storage.read_item_projections(rollout.session_id) == []
    with pytest.raises(KeyError, match="item projection 缺少"):
        storage.storage.read_item_projections(rollout.session_id, item_ids=(item_id,))


def test_summary_reads_never_open_jsonl_body(
    committed_rollout: CommittedRollout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = committed_rollout
    original_open = Path.open

    def forbid_jsonl_body(path: Path, *args: object, **kwargs: object):
        if path == rollout.jsonl:
            pytest.fail("summary read 不得打开 canonical JSONL 正文")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_jsonl_body)
    storage = RolloutCheckpointSaver(rollout.sessions)._storage
    projections = storage.storage.read_item_projections(rollout.session_id)
    assert len(projections) == 1
    thread_id = storage.storage.main_thread_id(rollout.session_id)
    with storage.storage.open_read_snapshot(rollout.session_id, thread_id) as snapshot:
        assert snapshot.thread_id == thread_id
        assert snapshot.session_id == rollout.session_id


@pytest.mark.parametrize("text_length", [65535, 65536, 65537])
def test_bounded_projection_preserves_original_text_length(
    committed_rollout: CommittedRollout,
    text_length: int,
) -> None:
    rollout = committed_rollout
    saver = RolloutCheckpointSaver(rollout.sessions)
    accepted = saver.accept_turn(
        rollout.session_id,
        accepted_ingress_id="long-text",
        acceptance_idempotency_key="long-text",
        job_id="long-text-job",
        admission_idempotency_key="long-text-admission",
        payload="界" * text_length,
        payload_kind=PayloadKind.TEXT,
    )
    projections = saver._storage.storage.read_item_projections(
        rollout.session_id, item_ids=(accepted["root_input_item_id"],)
    )
    assert len(projections) == 1
    assert projections[0]["content"] == "界" * min(text_length, 65536)
    assert projections[0]["content_length"] == text_length
    assert projections[0]["content_truncated"] == int(text_length > 65536)


@pytest.mark.parametrize("operation", ["initialize", "snapshot"])
def test_removed_physical_compaction_journal_is_rejected_without_recovery(
    committed_rollout: CommittedRollout,
    operation: str,
) -> None:
    rollout = committed_rollout
    with sqlite3.connect(rollout.index) as connection:
        connection.execute("CREATE TABLE compaction_runs (compaction_id TEXT)")
        connection.execute("INSERT INTO compaction_runs VALUES ('incomplete-old-run')")
    with rollout.jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = rollout.jsonl.read_bytes()
    storage = RolloutCheckpointSaver(rollout.sessions)._storage
    with pytest.raises(
        RuntimeError, match="recovery-required.*物理 compaction journal"
    ):
        if operation == "initialize":
            storage.storage.initialize(rollout.session_id)
        else:
            with storage.storage.open_read_snapshot(
                rollout.session_id, storage.storage.main_thread_id(rollout.session_id)
            ):
                pytest.fail("未完成的旧物理回收不能成为可读 snapshot")
    assert rollout.jsonl.read_bytes() == before


def test_nullable_source_revision_uses_stable_canonical_revision(
    committed_rollout: CommittedRollout,
) -> None:
    rollout = committed_rollout
    saver = RolloutCheckpointSaver(rollout.sessions)
    accepted = saver.accept_turn(
        rollout.session_id,
        accepted_ingress_id="nullable-source",
        acceptance_idempotency_key="nullable-source",
        job_id="nullable-source-job",
        admission_idempotency_key="nullable-source-admission",
        payload="没有外部 revision 的 canonical input",
        extensions={
            "test.nullable-source": {
                "extension_version": "1",
                "protection": "public",
                "value": {"source_revision": None},
            }
        },
    )
    restarted = RolloutCheckpointSaver(rollout.sessions)._storage.storage
    items = restarted.read_items(
        rollout.session_id,
        restarted.main_thread_id(rollout.session_id),
        item_ids=(accepted["root_input_item_id"],),
    )
    assert len(items) == 1
    with sqlite3.connect(rollout.index) as connection:
        revision = connection.execute(
            "SELECT source_revision FROM item_catalog WHERE item_id = ?",
            (items[0].item_id,),
        ).fetchone()[0]
    assert revision == f"canonical:{items[0].item_id}:{items[0].content_hash}"


def test_startup_rejects_unpublished_migration_without_resetting_target(
    committed_rollout: CommittedRollout,
) -> None:
    rollout = committed_rollout
    with sqlite3.connect(rollout.index) as connection:
        connection.execute("UPDATE database_meta SET database_state = 'migrating'")
        catalog = connection.execute("SELECT * FROM item_catalog").fetchall()
    with pytest.raises(RuntimeError, match="rollout-publication-incomplete"):
        RolloutCheckpointSaver(rollout.sessions)._storage.storage.initialize(rollout.session_id)
    assert rollout.jsonl.read_bytes() == rollout.committed
    with sqlite3.connect(rollout.index) as connection:
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == catalog
        assert connection.execute(
            "SELECT database_state FROM database_meta"
        ).fetchone() == ("migrating",)
