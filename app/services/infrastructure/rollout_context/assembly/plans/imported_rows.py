"""显式 sealed import 的无正文索引；不借用 draft 生命周期。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    ParsedSourceManifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    json_text,
)
from app.services.infrastructure.rollout_context.assembly.plans.rows import tool_rows


def _ref_rows(sources: ParsedSourceManifest) -> set[tuple[object, ...]]:
    return {
        (ref.ref_type, ref.ref_id, json_text(ref.to_dict())) for ref in sources.refs
    }


def _contribution_rows(sources: ParsedSourceManifest) -> set[tuple[object, ...]]:
    return {
        (item["contribution_id"], json_text(item))
        for item in sources.manifest["contributions"]
    }


def _tools(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot
) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT tool_set_snapshot_id, assembly_id, source_revision, tool_set_schema, "
        "tool_set_schema_version, tool_policy_version, content_length, content_hash, "
        "redacted_stable_digest, protection, availability, tool_policy_json, tools_json "
        "FROM tool_set_snapshots WHERE session_id = ? AND plan_id = ?",
        (snapshot.session_id, snapshot.plan_id),
    ).fetchall()


def write_imported_rows(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot, timestamp: str,
    *, sources: ParsedSourceManifest,
) -> None:
    """只允许新 registry；migration 已有工具行必须精确匹配，不覆盖或补半表。"""
    identity = (snapshot.session_id, snapshot.plan_id)
    for table, columns, rows in (
        ("context_plan_refs", "ref_type, ref_id, ref_json", _ref_rows(sources)),
        (
            "context_plan_contributions",
            "contribution_id, manifest_json",
            _contribution_rows(sources),
        ),
    ):
        placeholders = ", ".join("?" for _ in columns.split(","))
        connection.executemany(
            f"INSERT INTO {table}(session_id, plan_id, {columns}) "
            f"VALUES (?, ?, {placeholders})",
            [(*identity, *row) for row in sorted(rows)],
        )
    expected = tool_rows(snapshot.as_sealed_plan(), snapshot.assembly_id)
    existing = _tools(connection, snapshot)
    if existing:
        if set(existing) != set(expected):
            raise ValueError(
                "source-mismatch: imported ToolSetSnapshot registry 不一致"
            )
        return
    connection.executemany(
        "INSERT INTO tool_set_snapshots(session_id, plan_id, tool_set_snapshot_id, assembly_id, "
        "source_revision, tool_set_schema, tool_set_schema_version, tool_policy_version, "
        "content_length, content_hash, redacted_stable_digest, protection, availability, "
        "tool_policy_json, tools_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(*identity, *row, timestamp) for row in expected],
    )


def validate_imported_rows(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
    *, sources: ParsedSourceManifest,
) -> None:
    identity = (snapshot.session_id, snapshot.plan_id)
    for table, columns, expected in (
        ("context_plan_refs", "ref_type, ref_id, ref_json", _ref_rows(sources)),
        (
            "context_plan_contributions",
            "contribution_id, manifest_json",
            _contribution_rows(sources),
        ),
    ):
        actual = connection.execute(
            f"SELECT {columns} FROM {table} WHERE session_id = ? AND plan_id = ?",
            identity,
        ).fetchall()
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("source-mismatch: imported source registry 不一致")
    if set(_tools(connection, snapshot)) != set(
        tool_rows(snapshot.as_sealed_plan(), snapshot.assembly_id)
    ):
        raise ValueError("source-mismatch: imported ToolSetSnapshot registry 不一致")
