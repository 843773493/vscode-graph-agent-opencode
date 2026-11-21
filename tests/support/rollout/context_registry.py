"""正式 rollout schema 的 Session、执行准入与 registry 连接前提。"""

import sqlite3
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def registry_db(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(
        sessions,
    )
    accepted = saver.accept_turn(
        session_id,
        thread_id=saver._storage.storage.main_thread_id(session_id), accepted_ingress_id="registry-ingress",
        acceptance_idempotency_key="registry-root",
        job_id="registry-job",
        admission_idempotency_key="registry-admission",
        payload="独立草稿",
        payload_kind="text",
    )
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session_id), "", session_id=session_id
    ) as owner_connection:
        database_path = owner_connection.execute("PRAGMA database_list").fetchone()[2]
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute(
        "SELECT schema_version, database_state FROM database_meta"
    ).fetchone() == (ROLLOUT_SCHEMA_VERSION, "active")
    try:
        yield saver, session_id, connection, accepted
    finally:
        connection.close()
