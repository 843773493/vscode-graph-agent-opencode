"""target release journal 的 durable 前像与 terminal CAS；不装配业务 runtime。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_catalog_store.contracts import (
    ForkRetentionClaim,
    ForkSourceCaptureRecord,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.fork_retention_release import (
    commit_fork_retention_release,
    prepare_fork_retention_release,
    read_fork_retention_releases,
)
from tests.support.canonical_id_at import session_id_at, thread_id_at
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING


@pytest.fixture(params=["new_session_main", "new_child_thread"])
def release_inputs(request: pytest.FixtureRequest) -> tuple[ForkRetentionClaim, ForkSourceCaptureRecord]:
    created = datetime(2026, 6, 1, tzinfo=UTC)
    source, target = session_id_at(created), session_id_at(created)
    source_thread, target_main = thread_id_at(created), thread_id_at(created)
    target_thread = target_main if request.param == "new_session_main" else thread_id_at(created)
    assert (target_thread == target_main) == (request.param == "new_session_main")
    frozen_request = json.dumps({
        "relationship": "pinned", "checkpoint_ns": "analysis", "creation_kind": request.param,
        "target_main_thread_id": target_main,
    }, sort_keys=True)
    claim = ForkRetentionClaim(
        fork_retention_claim_id="release-claim", workspace_id="workspace-release",
        source_session_id=source, target_session_id=target, source_lifecycle_generation=1,
        state="active", release_reason=None, record_created_at=created.isoformat(),
        record_updated_at=created.isoformat(),
    )
    capture = ForkSourceCaptureRecord(
        copy_operation_id="copy-release", creation_key="create-release",
        source_session_id=source, source_thread_id=source_thread,
        target_session_id=target, target_thread_id=target_thread,
        snapshot_id="snapshot-release", snapshot_relative_locator="fork-source-captures/snapshot-release",
        fork_id=claim.fork_retention_claim_id, materialization_id="material-release",
        request_json=frozen_request, preimage_hash=hashlib.sha256(frozen_request.encode()).hexdigest(),
        state="materialized", snapshot_sha256="1" * 64, marker_sha256="2" * 64,
        abort_reason=None,
    )
    return claim, capture


@pytest.fixture
def control(tmp_path: Path, release_inputs: tuple[ForkRetentionClaim, ForkSourceCaptureRecord]):
    store = SessionControlStore(tmp_path / "session-control.sqlite")
    store.initialize_main_thread(
        json.loads(release_inputs[1].request_json)["target_main_thread_id"], datetime(2026, 6, 1, tzinfo=UTC), TEST_THREAD_GRAPH_BINDING,
    )
    store.initialize_fence("active", 1)
    try:
        yield store
    finally:
        store.close()


def test_release_record_survives_reopen_and_commits_same_preimage(control, release_inputs) -> None:
    claim, capture = release_inputs
    record = prepare_fork_retention_release(control, claim=claim, capture=capture, release_reason="target 删除")
    reopened = SessionControlStore(control.database_path)
    try:
        assert read_fork_retention_releases(reopened) == (record,)
        terminal = commit_fork_retention_release(reopened, record)
        assert terminal.state == "committed"
        assert prepare_fork_retention_release(
            reopened, claim=claim, capture=capture, release_reason="target 删除",
        ) == terminal
        with pytest.raises(RuntimeError, match="不同冻结前像"):
            prepare_fork_retention_release(reopened, claim=claim, capture=capture, release_reason="另一个操作")
        with pytest.raises(RuntimeError, match="CAS 前像冲突"):
            commit_fork_retention_release(reopened, replace(record, capture_preimage_hash="3" * 64))
        assert read_fork_retention_releases(reopened) == (terminal,)
    finally:
        reopened.close()


def test_release_record_rejects_unrelated_capture_and_corrupted_preimage(control, release_inputs) -> None:
    claim, capture = release_inputs
    with pytest.raises(RuntimeError, match="匹配的 pinned capture"):
        prepare_fork_retention_release(control, claim=claim, capture=replace(capture, fork_id="other-claim"), release_reason="target 删除")
    assert read_fork_retention_releases(control) == ()
    bad_request = {**json.loads(capture.request_json), "target_main_thread_id": thread_id_at(datetime(2026, 6, 1, tzinfo=UTC))}
    assert bad_request["target_main_thread_id"] != json.loads(capture.request_json)["target_main_thread_id"]
    with pytest.raises(RuntimeError, match="main_thread_id 不一致"):
        prepare_fork_retention_release(
            control, claim=claim, capture=replace(capture, request_json=json.dumps(bad_request, sort_keys=True)), release_reason="target 删除",
        )
    assert read_fork_retention_releases(control) == ()
    prepare_fork_retention_release(control, claim=claim, capture=capture, release_reason="target 删除")
    control.connection.execute("UPDATE fork_retention_releases SET source_thread_id = ?", (thread_id_at(datetime(2026, 6, 1, tzinfo=UTC)),))
    with pytest.raises(RuntimeError, match="冻结前像已损坏"):
        read_fork_retention_releases(control)
