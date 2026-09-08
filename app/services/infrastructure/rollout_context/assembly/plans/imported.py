"""仅供显式 migration/fork 事务使用的 sealed registration port。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_rows import (
    validate_imported_rows,
    write_imported_rows,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    parse_source_manifest,
    read_source_manifest,
    validate_source_bindings,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    json_text,
    sealed_hash,
)
from app.services.infrastructure.rollout_context.assembly.plans.privacy import (
    validate_draft_privacy,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    require_owner,
    require_transaction,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)

_PROVENANCE_FIELDS = frozenset(
    {
        "source_session_id",
        "source_plan_id",
        "source_assembly_id",
        "source_snapshot_hash",
        "source_schema_version",
        "audit_id",
    }
)


def _provenance(origin: str, value: Mapping[str, object]) -> dict[str, object]:
    if origin not in {"schema3_import", "fork_import"}:
        raise ValueError("source-mismatch: imported registration origin 非法")
    if not isinstance(value, Mapping) or set(value) != _PROVENANCE_FIELDS:
        raise ValueError("source-mismatch: import provenance 字段不完整或未知")
    result = dict(value)
    if any(
        not isinstance(result[key], str) or not result[key].strip()
        for key in _PROVENANCE_FIELDS - {"source_schema_version"}
    ):
        raise ValueError("source-mismatch: import provenance identity 必须非空")
    version = result["source_schema_version"]
    if type(version) is not int or version != (3 if origin == "schema3_import" else 4):
        raise ValueError("source-mismatch: import provenance schema version 非法")
    if not re.fullmatch(r"sha256:jcs:v1:[0-9a-f]{64}", result["source_snapshot_hash"]):
        raise ValueError("source-mismatch: import provenance snapshot hash 非法")
    json_text(result)
    return result


def _snapshot(raw: str) -> ContextAssemblySnapshot:
    # 不可信恢复数据可能在 domain 异常里携带原值；错误不保留明文上下文。
    try:
        value = json.loads(raw)
        if raw != json_text(value):
            raise ValueError("snapshot 不是规范 JCS")
        if any(item.get("body") is not None for item in value["contributions"]):
            raise ValueError("imported snapshot 不得包含 inline contribution body")
        result = ContextAssemblySnapshot.from_dict(value)
        validate_draft_privacy(result.as_sealed_plan())
        result.validate_hashes()
    except Exception:  # noqa: BLE001 - 恢复边界禁止异常泄露凭据或正文
        failed = True
    else:
        failed = False
    if failed:
        raise ValueError("source-mismatch: imported snapshot schema/hash/privacy 非法")
    return result


def imported_registration_hash(
    snapshot: ContextAssemblySnapshot,
    *,
    detail_key: str | None,
    origin: str,
    source_provenance: Mapping[str, object],
    source_manifest: Mapping[str, object],
) -> str:
    """显式 import 的唯一 hash preimage；runtime 的 sealed_hash 合同不变。"""
    provenance = _provenance(origin, source_provenance)
    sources = parse_source_manifest(
        source_manifest, session_id=snapshot.session_id, plan_id=snapshot.plan_id
    )
    validate_source_bindings(snapshot, sources)
    if origin == "schema3_import" and (
        tuple(
            provenance[key]
            for key in (
                "source_session_id",
                "source_plan_id",
                "source_assembly_id",
                "source_snapshot_hash",
            )
        )
        != (
            snapshot.session_id,
            snapshot.plan_id,
            snapshot.assembly_id,
            sha256_jcs(snapshot.to_dict()),
        )
    ):
        raise ValueError("source-mismatch: schema3 provenance 必须匹配原同一 snapshot")
    if origin == "fork_import" and (
        provenance["source_session_id"] == snapshot.session_id
        or provenance["source_plan_id"] == snapshot.plan_id
        or provenance["source_assembly_id"] == snapshot.assembly_id
    ):
        raise ValueError("source-mismatch: fork import 必须使用 target-local identity")
    return sha256_jcs(
        {
            "schema": "context-plan-import:v2",
            "registration_origin": origin,
            "source_provenance": provenance,
            "source_manifest": sources.manifest,
            "sealed_hash": sealed_hash(snapshot, detail_key),
        }
    )


def _validate_fork_evidence(
    connection: sqlite3.Connection,
    snapshot: ContextAssemblySnapshot,
    origin: str,
    provenance: Mapping[str, object],
) -> None:
    if origin != "fork_import":
        return
    row = connection.execute(
        "SELECT lineage_json FROM fork_identity_mappings WHERE fork_id = ? "
        "AND entity_type = 'assembly' AND source_session_id = ? AND source_local_id = ? "
        "AND target_session_id = ? AND target_local_id = ?",
        (
            provenance["audit_id"],
            provenance["source_session_id"],
            provenance["source_assembly_id"],
            snapshot.session_id,
            snapshot.assembly_id,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(
            "source-mismatch: fork import 缺少复制 audit 的 source snapshot 证据"
        )
    evidence = json.loads(row[0])
    if row[0] != json_text(evidence) or evidence.get("source_snapshot") != dict(
        provenance
    ):
        raise ValueError(
            "source-mismatch: fork import provenance 与 source snapshot audit 不一致"
        )


def _assembly(
    connection: sqlite3.Connection, session_id: str, plan_id: str, assembly_id: str
) -> tuple[ContextAssemblySnapshot, str | None]:
    rows = connection.execute(
        "SELECT snapshot_json, detail_ref, status, assembly_id, turn_id, execution_id, "
        "plan_hash, request_hash, history_view_revision, source_overlay_epoch "
        "FROM context_assemblies WHERE session_id = ? AND plan_id = ?",
        (session_id, plan_id),
    ).fetchall()
    if len(rows) != 1 or rows[0][2] not in {"sealed", "terminal"}:
        raise ValueError(
            "source-mismatch: imported plan 必须恰好绑定一个 sealed assembly"
        )
    row = rows[0]
    snapshot = _snapshot(row[0])
    if (snapshot.session_id, snapshot.plan_id, snapshot.assembly_id) != (
        session_id,
        plan_id,
        assembly_id,
    ) or row[3:] != (
        snapshot.assembly_id,
        snapshot.turn_id,
        snapshot.execution_id,
        snapshot.plan_hash,
        snapshot.request_hash,
        snapshot.history_view_revision,
        snapshot.source_overlay_epoch,
    ):
        raise ValueError("source-mismatch: imported assembly header 与 snapshot 不一致")
    if row[1] is not None:
        detail_ref_from_key(row[1]).require_owner(session_id, assembly_id)
    return snapshot, row[1]


def write_imported_registration(
    connection: sqlite3.Connection,
    snapshot: ContextAssemblySnapshot,
    *,
    detail_key: str | None,
    origin: str,
    source_provenance: Mapping[str, object],
    source_manifest: Mapping[str, object],
    seal_idempotency_key: str,
) -> None:
    """调用方先写同事务 assembly；本 port 不创建连接、事务或提交。"""
    require_transaction(connection)
    if not isinstance(snapshot, ContextAssemblySnapshot):
        raise TypeError("import 必须提供 typed sealed snapshot")
    if any(item.body is not None for item in snapshot.contributions):
        raise ValueError(
            "source-mismatch: imported snapshot 不得包含 inline contribution body"
        )
    require_owner(connection, snapshot.session_id)
    provenance = _provenance(origin, source_provenance)
    if not isinstance(seal_idempotency_key, str) or not seal_idempotency_key.strip():
        raise ValueError("assembly-idempotency-conflict: import seal key 必须非空")
    # 重新解析 frozen dataclass 内可能被修改的嵌套值，不读取任何 source 正文。
    checked = _snapshot(json_text(snapshot.to_dict()))
    sources = parse_source_manifest(
        source_manifest, session_id=checked.session_id, plan_id=checked.plan_id
    )
    stored, stored_detail = _assembly(
        connection, checked.session_id, checked.plan_id, checked.assembly_id
    )
    if stored_detail != detail_key or sealed_hash(stored, stored_detail) != sealed_hash(
        checked, detail_key
    ):
        raise ValueError("source-mismatch: imported snapshot 必须匹配同事务 assembly")
    fingerprint = imported_registration_hash(
        checked, detail_key=detail_key, origin=origin, source_provenance=provenance,
        source_manifest=sources.manifest,
    )
    _validate_fork_evidence(connection, checked, origin, provenance)
    identity = (checked.session_id, checked.plan_id)
    if connection.execute(
        "SELECT 1 FROM context_plans WHERE session_id = ? AND plan_id = ?", identity
    ).fetchone():
        existing = read_imported_registration(connection, *identity)
        if (
            existing.registration_origin != origin
            or existing.source_provenance != provenance
            or existing.assembly_id != checked.assembly_id
            or existing.seal_idempotency_key != seal_idempotency_key
            or existing.seal_hash != fingerprint
        ):
            raise ValueError(
                "assembly-idempotency-conflict: imported registration 已存在不同输入"
            )
        return
    timestamp = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO context_plans(session_id, plan_id, registration_origin, source_provenance_json, source_manifest_json, "
        "revision, plan_state, assembly_id, seal_idempotency_key, seal_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, 'sealed', ?, ?, ?, ?, ?)",
        (
            *identity,
            origin,
            json_text(provenance),
            json_text(sources.manifest),
            checked.assembly_id,
            seal_idempotency_key,
            fingerprint,
            timestamp,
            timestamp,
        ),
    )
    write_imported_rows(connection, checked, timestamp, sources=sources)
    read_imported_registration(connection, *identity)


def read_imported_registration(
    connection: sqlite3.Connection, session: str, plan: str
) -> ContextPlanRegistration:
    """只读验证已导入 sealed header、原始 snapshot 与三类完整索引。"""
    require_owner(connection, session)
    row = connection.execute(
        "SELECT registration_origin, source_provenance_json, revision, plan_state, "
        "assembly_id, seal_idempotency_key, seal_hash, plan_creation_idempotency_key, "
        "creation_hash, creation_json, draft_json, draft_hash, seal_input_hash, source_manifest_json FROM context_plans "
        "WHERE session_id = ? AND plan_id = ?",
        (session, plan),
    ).fetchone()
    if row is None:
        raise KeyError("imported context plan 不存在")
    if (
        row[0] not in {"schema3_import", "fork_import"}
        or type(row[2]) is not int
        or row[2] != 0
        or row[3] != "sealed"
        or any(not isinstance(value, str) or not value.strip() for value in row[4:7])
        or any(value is not None for value in row[7:13])
    ):
        raise ValueError(
            "source-mismatch: imported header 不得包含虚构 draft 或非法 binding"
        )
    provenance = _provenance(row[0], json.loads(row[1]))
    if row[1] != json_text(provenance):
        raise ValueError("source-mismatch: import provenance 不是规范 JCS")
    snapshot, detail_key = _assembly(connection, session, plan, row[4])
    sources = read_source_manifest(row[13], session_id=session, plan_id=plan)
    if row[6] != imported_registration_hash(
        snapshot, detail_key=detail_key, origin=row[0], source_provenance=provenance,
        source_manifest=sources.manifest,
    ):
        raise ValueError("source-mismatch: imported seal hash 与 assembly 不一致")
    _validate_fork_evidence(connection, snapshot, row[0], provenance)
    validate_imported_rows(connection, snapshot, sources=sources)
    return ContextPlanRegistration(
        draft=None,
        creation_hash=None,
        revision=0,
        plan_state="sealed",
        assembly_id=row[4],
        seal_idempotency_key=row[5],
        seal_hash=row[6],
        registration_origin=row[0],
        source_provenance=provenance,
        source_manifest=sources.manifest,
    )


__all__ = [
    "imported_registration_hash",
    "read_imported_registration",
    "write_imported_registration",
]
