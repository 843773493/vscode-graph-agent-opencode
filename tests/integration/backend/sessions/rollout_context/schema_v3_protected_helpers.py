"""为 artifact 升级复用独立冻结的旧 protected golden，不运行旧 writer。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.schema_legacy_fixtures import (
    freeze_empty_legacy_database,
)
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    OLD_DETAIL_DDL,
    Schema2Artifact,
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    _DIGEST,
    _OLD_BLOB,
)


def create_protected_schema2_artifact(saver: RolloutCheckpointSaver) -> Schema2Artifact:
    session_id = "upgrade-session"
    saver.accept_turn(session_id, accepted_ingress_id="protected-ingress",
        acceptance_idempotency_key="protected-acceptance", payload="确定性 schema2 protected fixture")
    root = saver._storage.root(session_id)
    freeze_empty_legacy_database(root / "index.sqlite", version=2)
    key = root / ".context-redaction-key"
    key.write_bytes(b"s" * 32)
    key.chmod(0o600)
    envelope = {
        "format_version": 1, "assembly_id": "old-assembly", "content_length": 60,
        "created_at": "2026-09-07T01:00:00+00:00", "gc_after": "2036-09-07T01:00:00+00:00",
        "detail": {"redacted": True, "redacted_stable_digest": _DIGEST},
        "detail_content_hash": None, "protection": "protected", "sensitive": True,
        "redacted_stable_digest": _DIGEST, "source_revision": "producer-v1", "protected_body": True,
    }
    ordinary = Path("context-plan-details/old-assembly/detail-old.json")
    protected = Path("context-plan-details-protected/old-assembly/detail-old.bin")
    for path, raw in ((ordinary, canonical_json_bytes(envelope)), (protected, _OLD_BLOB)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(raw)
    with closing(sqlite3.connect(root / "index.sqlite")) as connection, connection:
        connection.execute("DROP TABLE context_plan_details")
        connection.execute(OLD_DETAIL_DDL)
        connection.execute("INSERT INTO context_plan_details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "detail-old", session_id, "", "old-assembly", "rollout/" + ordinary.as_posix(), sha256_jcs(envelope),
            "producer-v1", 60, _DIGEST, "protected", "available", 0, 1, "available", envelope["created_at"], envelope["gc_after"],
        ))
    return Schema2Artifact(saver, session_id, root, "old-assembly", {"secret": "schema2-protected-fixture", "mode": "旧上下文"})
