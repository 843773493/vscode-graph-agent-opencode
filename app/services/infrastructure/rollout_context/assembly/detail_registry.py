"""受 owner 约束的详情 manifest registry；正文仅由 runtime detail store 持有。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_retention import (
    DetailRetentionMixin,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    detail_record_from_mapping,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text

_COLUMNS = (
    "detail_ref", "session_id", "checkpoint_ns", "assembly_id", "detail_id",
    "detail_kind", "retention_class", "visibility", "relative_path", "content_hash",
    "source_revision", "content_length", "redacted_stable_digest", "protection",
    "availability", "required", "sensitive", "status", "created_at", "expires_at",
)


def _detail_mapping(row: tuple[object, ...]) -> dict[str, object]:
    value = dict(zip(_COLUMNS, row, strict=True))
    ref = detail_ref_from_key(value["detail_ref"])
    value["detail_ref"] = ref
    value["length"] = value.pop("content_length")
    record = detail_record_from_mapping(value)
    if record.detail_ref != ref:
        raise ValueError("source-mismatch: detail owner 字段与 typed reference 不一致")
    if record.relative_path != detail_relative_path(ref).as_posix():
        raise ValueError("source-mismatch: detail locator 不属于 typed owner")
    return value


def validate_assembly_details(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
    checkpoint_ns: str, header_detail_key: str | None,
) -> None:
    """seal preflight 与恢复共用同一份详情完整性合同，不在提交后补查。"""
    selected: dict[str, ContextSelectionEntry | None] = {
        detail_ref_key(entry.detail_ref): entry
        for entry in snapshot.selection
        if entry.included and entry.detail_ref is not None
    }
    if header_detail_key is not None:
        detail_ref_from_key(header_detail_key).require_owner(snapshot.session_id, snapshot.assembly_id)
        selected.setdefault(header_detail_key, None)
    rows = connection.execute(
        f"SELECT {','.join(_COLUMNS)} FROM context_plan_details WHERE assembly_id = ?",
        (snapshot.assembly_id,),
    ).fetchall()
    records = {row[0]: _detail_mapping(tuple(row)) for row in rows}
    if set(records) != set(selected):
        raise RuntimeError("detail-unavailable: sealed assembly detail manifest 集合不一致")
    for key, record in records.items():
        ref = detail_ref_from_key(key)
        ref.require_owner(snapshot.session_id, snapshot.assembly_id)
        entry = selected[key]
        # optional header 是审计附件，不是进入请求的正文。fork/GC 可以保留
        # 它的 unavailable manifest；included source 和 required header 仍必须可读。
        needs_body = entry is not None or bool(record["required"])
        if record["checkpoint_ns"] != checkpoint_ns or (
            needs_body and record["availability"] != "available"
        ):
            raise RuntimeError("detail-unavailable: sealed assembly detail 状态或 namespace 不一致")
        if record["required"] == 1 and record["protection"] == "redacted":
            raise RuntimeError("detail-unavailable: required detail 不能仅有 redacted marker")
        if entry is not None and (
            entry.source_revision != record["source_revision"]
            or entry.content_length != record["length"]
            or entry.protection != record["protection"]
            or entry.visibility != record["visibility"]
            or entry.availability != record["availability"]
            or record["detail_kind"] != "request_source"
            or record["retention_class"] != "request_replay"
            or (entry.redacted_stable_digest is not None
                and entry.redacted_stable_digest != record["redacted_stable_digest"])
        ):
            raise RuntimeError("source-mismatch: sealed assembly detail source manifest 不一致")
        if entry is None and (
            record["detail_kind"] != "assembly_snapshot"
            or record["retention_class"] != "assembly_audit"
        ):
            raise RuntimeError("source-mismatch: assembly header detail 用途不一致")


class DetailRegistryMixin(DetailRetentionMixin):
    def register_context_plan_detail(self, detail: DetailRecord) -> None:
        if not isinstance(detail, DetailRecord):
            raise TypeError("detail registry 只接受完整 DetailRecord")
        ref = detail.detail_ref
        key = detail_ref_key(ref)
        expected_path = detail_relative_path(ref)
        if detail.relative_path != expected_path.as_posix():
            raise ValueError("detail locator 必须严格等于 typed owner 的 assembly/detail_id 路径")
        self._safe_session_relative_path(
            self.root(ref.session_id, detail.checkpoint_ns).parent, expected_path,
        )
        values = (
            key, ref.session_id, detail.checkpoint_ns, ref.assembly_id, ref.detail_id,
            detail.detail_kind, detail.retention_class, detail.visibility,
            detail.relative_path, detail.content_hash, detail.source_revision,
            detail.length, detail.redacted_stable_digest, detail.protection,
            detail.availability, int(detail.required), int(detail.sensitive),
            detail.status, datetime.now(UTC).isoformat(), detail.expires_at,
        )
        _detail_mapping(values)
        with self._lock(ref.session_id, detail.checkpoint_ns):
            self.initialize(ref.session_id, detail.checkpoint_ns)
            with self._connect(ref.session_id, detail.checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    f"SELECT {','.join(_COLUMNS)} FROM context_plan_details WHERE detail_ref = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    _detail_mapping(tuple(existing))
                    # created_at 属于首次 registry 提交；其余 manifest 均不可覆盖。
                    if tuple(existing[:18]) != values[:18] or existing[19] != values[19]:
                        raise ValueError("source-mismatch: detail_ref 已存在但 manifest 冲突")
                    return
                result = connection.execute(
                    f"INSERT INTO context_plan_details ({','.join(_COLUMNS)}) "
                    f"VALUES ({','.join('?' for _ in _COLUMNS)})", values,
                )
                if result.rowcount != 1:
                    raise RuntimeError("context detail manifest 未写入")
                connection.commit()

    def get_context_plan_detail(
        self, thread_id: str, *, detail_ref: DetailRef, checkpoint_ns: str = "",
    ) -> dict[str, object]:
        thread_id = strict_text(thread_id, field="get_context_plan_detail.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        key = detail_ref_key(detail_ref)
        detail_ref.require_owner(thread_id)
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM context_plan_details "
                "WHERE detail_ref = ? AND session_id = ? AND checkpoint_ns = ?",
                (key, thread_id, checkpoint_ns),
            ).fetchone()
        if row is None:
            raise KeyError(f"detail-unavailable: context detail 不存在: {detail_ref}")
        value = _detail_mapping(tuple(row))
        if value["detail_ref"] != detail_ref:
            raise ValueError("source-mismatch: registry 返回了错误的 detail identity")
        return value
