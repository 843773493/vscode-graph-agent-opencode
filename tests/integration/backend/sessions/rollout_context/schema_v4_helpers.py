"""真实 Saver 数据装入冻结 schema3 DDL；不是旧 Provider 记录或改版号假库。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextContribution
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    quote,
    rows,
)

SCHEMA3_DDL_PATH = Path("tests/integration/backend/sessions/rollout_context/schema_v4_fixtures/schema3.sql")


@dataclass(frozen=True)
class Schema3Artifact:
    saver: RolloutCheckpointSaver
    session_id: str
    root: Path
    assembly_id: str | None
    plan_id: str | None
    body: object

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"


def freeze_schema3_database(index: Path) -> None:
    """新建真实历史结构并显式装入相同 sealed 业务行；不调用当前 schema owner。"""
    with closing(sqlite3.connect(index)) as generated, closing(sqlite3.connect(":memory:")) as historical:
        historical.executescript((Path.cwd() / SCHEMA3_DDL_PATH).read_text())
        tables = [row[0] for row in historical.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        # schema3 seal 只持久化 included ToolSetSnapshot；schema4 的真实
        # draft 行包含更多候选，不能把这部分新生命周期伪装成旧库事实。
        included_tools = {
            (row["assembly_id"], ref["ref_id"])
            for row in rows(generated, "context_assemblies")
            for ref in json.loads(row["snapshot_json"])["tool_set_refs"]
        }
        for table in tables:
            if table == "schema_migrations":
                continue
            columns = tuple(row[1] for row in historical.execute(f"PRAGMA table_info({quote(table)})"))
            for row in rows(generated, table):
                if table == "tool_set_snapshots" and (row["assembly_id"], row["tool_set_snapshot_id"]) not in included_tools:
                    continue
                if table == "database_meta":
                    row["schema_version"] = 3
                historical.execute(
                    f"INSERT INTO {quote(table)}({','.join(quote(column) for column in columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})", tuple(row[column] for column in columns),
                )
        timestamp = historical.execute("SELECT created_at FROM database_meta").fetchone()[0]
        historical.execute(
            "INSERT INTO schema_migrations(from_version,to_version,migration_name,migration_checksum,status,started_at,completed_at) "
            "VALUES(0,3,'rollout_sqlite_v3',?,'completed',?,?)",
            (hashlib.sha256(b"rollout_sqlite_v3").hexdigest(), timestamp, timestamp),
        )
        historical.commit()
        assert historical.execute("PRAGMA foreign_key_check").fetchall() == []
        historical.backup(generated)
        generated.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def create_schema3_artifact(
    saver: RolloutCheckpointSaver, session_id: str, *, empty: bool = False,
    omit_sources: bool = False,
) -> Schema3Artifact:
    accepted = saver.accept_turn(
        session_id, accepted_ingress_id="schema3-fixture-ingress",
        acceptance_idempotency_key="schema3-fixture-root", payload="升级不能改 canonical 输入",
    )
    if empty:
        root = saver._storage.root(session_id)
        freeze_schema3_database(root / "index.sqlite")
        return Schema3Artifact(saver, session_id, root, None, None, None)
    body = [{"type": "text", "text": "schema3 历史 request-only 正文"}]
    saver.register_context_contribution(session_id, ContextContribution(
        contribution_id="schema3-contribution", source_kind="environment",
        source_revision="revision-3", body=body,
        content_hash=contribution_content_hash("prompt", body),
    ), request_content=body)
    plan = saver.compose_committed_context_plan(session_id, plan_id="schema3-plan", tool_snapshot=(
        {"name": "echo", "description": "工具正文必须保留", "parameters": {"type": "object"}},
    ))
    draft = saver.create_context_plan(session_id, replace(plan, plan_creation_idempotency_key="fixture-create")).draft
    assert draft is not None
    snapshot = saver.seal_context_plan(
        session_id, draft, turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
        provider_version="deterministic-schema3-integration", seal_idempotency_key="fixture-seal",
        request_only_content={"schema3-contribution": body},
        omitted_ref_ids=("schema3-contribution", *(ref.ref_id for ref in draft.tool_set_refs)) if omit_sources else (),
    )
    root = saver._storage.root(session_id)
    freeze_schema3_database(root / "index.sqlite")
    return Schema3Artifact(saver, session_id, root, snapshot.assembly_id, snapshot.plan_id, body)


def immutable_files(root: Path) -> dict[str, bytes]:
    """业务原件不包括 SQLite 本体、协调文件及显式 migration backup。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.name.startswith("index.sqlite")}


def bind_frozen_omitted_source(source: Schema3Artifact, contribution_id: str) -> None:
    """构造旧合同允许的已知 omitted mapping；只写正式 fixture，不是迁移修复。

    当前 Saver 的显式 omission 默认不保留 contribution_id。冻结输入在此保留
    旧 registry 已存在的 identity，仍不创建 included contribution 或 detail。
    传入未知 ID 时专用于篡改拒绝验收，所有 snapshot/hash/派生行同时一致。
    """
    with closing(sqlite3.connect(source.index)) as connection:
        raw = connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone()[0]
        snapshot = ContextAssemblySnapshot.from_dict(json.loads(raw))
        assert not snapshot.contributions
        selection = tuple(
            replace(entry, contribution_id=contribution_id)
            if not entry.included and entry.ref.ref_type == "request_only" else entry
            for entry in snapshot.selection
        )
        changed = replace(snapshot, selection=selection)
        plan = changed.as_sealed_plan()
        changed = replace(changed, plan_hash=plan.plan_hash(), request_hash=context_request_hash(
            plan, changed.provider_version, projector_id=changed.projector_id,
            projector_version=changed.projector_version, target_format=changed.target_format,
            wire_request=changed.request_hash_preimage,
        ))
        changed.validate_hashes()
        connection.execute(
            "UPDATE context_assemblies SET snapshot_json=?,plan_hash=?,request_hash=?",
            (canonical_json_bytes(changed.to_dict()).decode(), changed.plan_hash, changed.request_hash),
        )
        for table in ("assembly_item_refs", "context_assembly_selections"):
            connection.execute(
                f"UPDATE {table} SET contribution_id=? WHERE ref_type='request_only'",
                (contribution_id,),
            )
        connection.commit()
