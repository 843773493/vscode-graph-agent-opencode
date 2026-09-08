"""真实 Saver 生成再冻结为 schema2 的确定性集成数据；不是历史 Provider 记录。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.request_plan import ContextContribution
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.legacy_hash import (
    legacy_hashes,
)
from tests.integration.backend.sessions.rollout_context.schema_legacy_fixtures import (
    OLD_DETAIL_DDL,
    stamp_legacy_version,
)
from tests.integration.backend.sessions.rollout_context.schema_v4_helpers import (
    freeze_schema3_database,
)

__all__ = ["OLD_DETAIL_DDL", "Schema2Artifact", "artifact_manifest", "assert_original_artifacts_unchanged", "create_schema2_artifact"]


def artifact_manifest(root: Path) -> dict[str, dict[str, object]]:
    """字节级原件快照放在子进程，不能用额外 fd 释放父进程活跃 SQLite 锁。"""
    result = subprocess.run([sys.executable, "-c", """
import json
import sys
from pathlib import Path
from app.services.infrastructure.rollout_context.migration.artifacts import artifact_manifest
print(json.dumps(artifact_manifest(Path(sys.argv[1]))))
""", str(root)], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def assert_original_artifacts_unchanged(root: Path, before: dict) -> None:
    """允许只读 SQLite 新建协调文件；所有既有 artifact（含非空 WAL）必须原样。"""
    after = artifact_manifest(root)
    assert {path: after[path] for path in before} == before
    extra = set(after) - set(before)
    assert extra <= {"index.sqlite-shm", "index.sqlite-wal"}
    if "index.sqlite-wal" in extra:
        assert after["index.sqlite-wal"]["size"] == 0


@dataclass(frozen=True)
class Schema2Artifact:
    saver: RolloutCheckpointSaver
    session_id: str
    root: Path
    assembly_id: str
    body: object

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"


def create_schema2_artifact(saver: RolloutCheckpointSaver, session_id: str) -> Schema2Artifact:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "schema2-checkpoint"
    checkpoint["channel_values"] = {"messages": [
        HumanMessage(content="迁移不能改变 canonical 输入", id="schema2-user", response_metadata={"turn_id": "schema2-turn"}),
        AIMessage(content="迁移不能改变 canonical 输出", id="schema2-output"),
    ]}
    checkpoint["channel_versions"] = {"messages": "1"}
    saver.put(build_checkpoint_config(session_id), checkpoint, {"source": "integration"}, {"messages": "1"})
    body = [{"type": "text", "text": "迁移后的 request-only 正文必须精确恢复"}]
    saver.register_context_contribution(session_id, ContextContribution(
        contribution_id="schema2-contribution", source_kind="environment", source_revision="schema2-revision",
        body=body, content_hash=contribution_content_hash("prompt", body),
    ), request_content=body)
    plan = saver.compose_committed_context_plan(session_id, plan_id="schema2-plan", tool_snapshot=(
        {"tool_id": "echo", "name": "echo", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}},
    ))
    draft = saver.create_context_plan(session_id, replace(plan, plan_creation_idempotency_key="schema2-create")).draft
    assert draft is not None
    sealed = saver.seal_context_plan(session_id, draft, turn_id="schema2-turn", execution_id=saver.execution_for_turn(session_id, turn_id="schema2-turn"), provider_version="integration-schema2", seal_idempotency_key="schema2-seal", request_only_content={"schema2-contribution": body})
    root = saver._storage.root(session_id)
    freeze_schema3_database(root / "index.sqlite")
    with closing(sqlite3.connect(root / "index.sqlite")) as connection, connection:
        connection.row_factory = sqlite3.Row
        details = [dict(row) for row in connection.execute("SELECT * FROM context_plan_details")]
        mapping = {row["detail_ref"]: row["detail_id"] for row in details}
        connection.execute("DROP TABLE context_plan_details")
        connection.execute(OLD_DETAIL_DDL)
        for row in details:
            source_path = root.parent / row["relative_path"]
            current = json.loads(source_path.read_bytes())
            old = {key: current[key] for key in ("detail", "created_at", "detail_content_hash", "protection", "redacted_stable_digest", "sensitive", "source_revision", "protected_body")}
            old.update(format_version=1, assembly_id=row["assembly_id"], content_length=row["content_length"], gc_after=row["expires_at"])
            relative = f"rollout/context-plan-details/{row['assembly_id']}/{row['detail_id']}.json"
            (root.parent / relative).write_bytes(canonical_json_bytes(old))
            source_path.unlink()
            connection.execute("INSERT INTO context_plan_details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row["detail_id"], session_id, row["checkpoint_ns"], row["assembly_id"], relative,
                sha256_jcs(old), row["source_revision"], row["content_length"], row["redacted_stable_digest"],
                row["protection"], row["availability"], row["required"], row["sensitive"], row["status"], row["created_at"], row["expires_at"],
            ))
        for table in ("context_assemblies", "assembly_item_refs", "context_assembly_selections"):
            for key, old_id in mapping.items():
                connection.execute(f"UPDATE {table} SET detail_ref=? WHERE detail_ref=?", (old_id, key))
        for row in list(connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies")):
            value = json.loads(row["snapshot_json"])
            for entry in value["selection"]:
                if entry["detail_ref"] is not None:
                    entry["detail_ref"] = entry["detail_ref"]["detail_id"]
                if entry["ref"]["ref_type"] != "tool_set":
                    entry["ref"]["detail_ref"] = entry["detail_ref"]
            for ref in value["refs"]:
                ref["detail_ref"] = next((entry["detail_ref"] for entry in value["selection"] if entry["ref"]["ref_id"] == ref["ref_id"] and entry["ref"]["ref_type"] == ref["ref_type"]), None)
            value["plan_hash"], value["request_hash"] = legacy_hashes(value)
            connection.execute("UPDATE context_assemblies SET snapshot_json=?,plan_hash=?,request_hash=? WHERE assembly_id=?", (canonical_json_bytes(value).decode(), value["plan_hash"], value["request_hash"], row["assembly_id"]))
        connection.row_factory = None
        stamp_legacy_version(connection, 2)
    return Schema2Artifact(saver, session_id, root, sealed.assembly_id, body)
