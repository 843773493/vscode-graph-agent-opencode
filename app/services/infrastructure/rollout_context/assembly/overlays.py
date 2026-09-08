"""v2 source overlay registry persistence owner。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _required_text(values: dict[str, object], name: str) -> str:
    value = values[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"source overlay {name} 必须是非空字符串")
    return value


def _optional_text(values: dict[str, object], name: str) -> str | None:
    value = values[name]
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"source overlay {name} 必须是非空字符串或 null")
    return value


def _optional_non_negative_int(values: dict[str, object], name: str) -> int | None:
    value = values[name]
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise ValueError(f"source overlay {name} 必须是非负整数或 null")
    return value


def _assert_one_row(cursor, *, context: str) -> None:
    if cursor.rowcount != 1:
        raise RuntimeError(f"{context} 影响行数异常: {cursor.rowcount}")


def _validate_source_overlay_row(
    row: dict[str, object],
    *,
    session_id: str,
    checkpoint_ns: str,
) -> None:
    if row.get("session_id") != session_id:
        raise RuntimeError("source overlay session identity 不一致")
    if row.get("checkpoint_ns") != checkpoint_ns:
        raise RuntimeError("source overlay checkpoint namespace identity 不一致")
    for name in ("overlay_id", "source_kind", "source_revision", "status"):
        _required_text(row, name)
    status = row["status"]
    assert isinstance(status, str)
    if status not in {"active", "superseded", "materialized"}:
        raise RuntimeError(f"source overlay status 非法: {status}")
    epoch = row["source_overlay_epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise RuntimeError("source overlay source_overlay_epoch 不是非负整数")
    if not isinstance(row["checkpoint_ns"], str):
        raise TypeError("source overlay checkpoint_ns 不是字符串")
    for name in (
        "base_ref",
        "delta_ref",
        "supersedes_overlay_id",
        "materializes_overlay_id",
        "idempotency_key",
        "base_source_revision",
        "delta_source_revision",
        "base_content_hash",
        "base_redacted_stable_digest",
        "delta_content_hash",
        "delta_redacted_stable_digest",
        "delta_from_revision",
        "delta_to_revision",
        "delta_diff_hash",
    ):
        _optional_text(row, name)
    for name in ("base_content_length", "delta_content_length"):
        _optional_non_negative_int(row, name)
    if row["status"] == "active" and not (row["base_ref"] or row["delta_ref"]):
        raise RuntimeError("active source overlay 缺少 base_ref/delta_ref")
    if row["base_ref"] is not None and row["base_ref"] == row["delta_ref"]:
        raise RuntimeError("source overlay base_ref 与 delta_ref 不能复用同一 ref")
    if row["delta_ref"] is not None and not all(
        row[name] is not None
        for name in ("delta_from_revision", "delta_to_revision", "delta_diff_hash")
    ):
        raise RuntimeError("source overlay delta manifest 不完整")
    for role in ("base", "delta"):
        ref = row[f"{role}_ref"]
        if ref is None and any(
            row[name] is not None
            for name in (
                f"{role}_source_revision",
                f"{role}_content_length",
                f"{role}_content_hash",
                f"{role}_redacted_stable_digest",
            )
        ):
            raise RuntimeError(f"source overlay {role} manifest 没有对应 ref")
    if not isinstance(row["created_at"], str) or not row["created_at"]:
        raise RuntimeError("source overlay created_at 不是非空字符串")


class ContextOverlayStorageMixin:
    def register_source_overlay(
        self,
        overlay: object,
        *,
        base_content: object | None = None,
        delta_content: object | None = None,
    ) -> None:
        """持久化 source base/delta lineage，不把变更写回 canonical history。"""
        values = {
            name: getattr(overlay, name, None)
            for name in (
                "overlay_id",
                "session_id",
                "checkpoint_ns",
                "source_kind",
                "source_revision",
                "source_overlay_epoch",
                "base_ref",
                "delta_ref",
                "supersedes_overlay_id",
                "materializes_overlay_id",
                "status",
                "idempotency_key",
                "base_source_revision",
                "base_content_length",
                "base_content_hash",
                "base_redacted_stable_digest",
                "delta_source_revision",
                "delta_content_length",
                "delta_content_hash",
                "delta_redacted_stable_digest",
                "delta_from_revision",
                "delta_to_revision",
                "delta_diff_hash",
            )
        }
        overlay_id = _required_text(values, "overlay_id")
        session_id = _required_text(values, "session_id")
        source_kind = _required_text(values, "source_kind")
        source_revision = _required_text(values, "source_revision")
        status = _required_text(values, "status")
        if status not in {"active", "superseded", "materialized"}:
            raise ValueError(f"source overlay status 非法: {status}")
        source_overlay_epoch = values["source_overlay_epoch"]
        if (
            not isinstance(source_overlay_epoch, int)
            or isinstance(source_overlay_epoch, bool)
            or source_overlay_epoch < 0
        ):
            raise ValueError("source overlay source_overlay_epoch 必须是非负整数")
        checkpoint_ns_value = values["checkpoint_ns"]
        if checkpoint_ns_value is None:
            checkpoint_ns = ""
        elif isinstance(checkpoint_ns_value, str):
            checkpoint_ns = checkpoint_ns_value
        else:
            raise ValueError("source overlay checkpoint_ns 必须是字符串或 null")
        for name in (
            "base_ref",
            "delta_ref",
            "supersedes_overlay_id",
            "materializes_overlay_id",
            "idempotency_key",
            "base_source_revision",
            "delta_source_revision",
            "base_content_hash",
            "base_redacted_stable_digest",
            "delta_content_hash",
            "delta_redacted_stable_digest",
            "delta_from_revision",
            "delta_to_revision",
            "delta_diff_hash",
        ):
            _optional_text(values, name)
        for name in ("base_content_length", "delta_content_length"):
            _optional_non_negative_int(values, name)
        if values["base_ref"] is not None and values["base_ref"] == values["delta_ref"]:
            raise ValueError("source overlay base_ref 与 delta_ref 不能复用同一 ref")
        for role, content in (("base", base_content), ("delta", delta_content)):
            ref = values[f"{role}_ref"]
            if ref is None:
                continue
            assert isinstance(ref, str)
            source_revision_key = f"{role}_source_revision"
            length_key = f"{role}_content_length"
            hash_key = f"{role}_content_hash"
            digest_key = f"{role}_redacted_stable_digest"
            values[source_revision_key] = (
                values[source_revision_key]
                if values[source_revision_key] is not None
                else source_revision
            )
            if content is not None:
                actual_length = len(canonical_json_bytes(content))
                actual_hash = contribution_content_hash(
                    f"overlay_{role}",
                    content,
                )
                if (
                    values[length_key] is not None
                    and values[length_key] != actual_length
                ):
                    raise ValueError(
                        f"source overlay {role} content_length 与正文不一致: {ref}"
                    )
                if values[hash_key] is not None and values[hash_key] != actual_hash:
                    raise ValueError(
                        f"source overlay {role} content_hash 与正文不一致: {ref}"
                    )
                if values[digest_key] is not None:
                    raise ValueError(
                        f"source overlay {role} 正文不得同时使用 redacted digest: {ref}"
                    )
                values[length_key] = actual_length
                values[hash_key] = actual_hash
                values[digest_key] = None
            else:
                length = values[length_key]
                token = values[hash_key]
                redacted_digest = values[digest_key]
                if length is None or (token is None) == (redacted_digest is None):
                    raise ValueError(
                        f"source overlay {role} 缺少完整 manifest，无法安全恢复: {ref}"
                    )
                values[length_key] = length
                values[hash_key] = token
                values[digest_key] = redacted_digest
        for name in (
            "base_source_revision",
            "delta_source_revision",
            "delta_from_revision",
            "delta_to_revision",
            "delta_diff_hash",
        ):
            value = values[name]
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"source overlay {name} 不能为空字符串")
        if values["delta_ref"] is not None and not all(
            values[name] is not None
            for name in (
                "delta_from_revision",
                "delta_to_revision",
                "delta_diff_hash",
            )
        ):
            raise ValueError(
                "source overlay delta_ref 必须带 from_revision/to_revision/diff_hash"
            )
        idempotency_value = values["idempotency_key"]
        if idempotency_value is not None:
            assert isinstance(idempotency_value, str)
        idempotency_key = idempotency_value or (
            "overlay:"
            + sha256_jcs(
                {
                    "session_id": values["session_id"],
                    "checkpoint_ns": checkpoint_ns,
                    "source_kind": values["source_kind"],
                    "source_revision": values["source_revision"],
                    "base_ref": values["base_ref"],
                    "delta_ref": values["delta_ref"],
                    "base_source_revision": values["base_source_revision"],
                    "base_content_length": values["base_content_length"],
                    "base_content_hash": values["base_content_hash"],
                    "base_redacted_stable_digest": values[
                        "base_redacted_stable_digest"
                    ],
                    "delta_source_revision": values["delta_source_revision"],
                    "delta_content_length": values["delta_content_length"],
                    "delta_content_hash": values["delta_content_hash"],
                    "delta_redacted_stable_digest": values[
                        "delta_redacted_stable_digest"
                    ],
                    "delta_from_revision": values["delta_from_revision"],
                    "delta_to_revision": values["delta_to_revision"],
                    "delta_diff_hash": values["delta_diff_hash"],
                }
            )
        )
        if status == "active" and not values["delta_ref"] and not values["base_ref"]:
            raise ValueError("active source overlay 必须声明 base_ref 或 delta_ref")
        with self._lock(session_id, checkpoint_ns):
            self.initialize(session_id, checkpoint_ns)
            with self._connect(session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT session_id, checkpoint_ns, source_kind, source_revision, source_overlay_epoch, base_ref, delta_ref, base_source_revision, base_content_length, base_content_hash, base_redacted_stable_digest, delta_source_revision, delta_content_length, delta_content_hash, delta_redacted_stable_digest, delta_from_revision, delta_to_revision, delta_diff_hash, supersedes_overlay_id, materializes_overlay_id, status, idempotency_key FROM source_overlays WHERE overlay_id = ?",
                    (overlay_id,),
                ).fetchone()
                expected = (
                    session_id,
                    checkpoint_ns,
                    source_kind,
                    source_revision,
                    source_overlay_epoch,
                    values["base_ref"],
                    values["delta_ref"],
                    values["base_source_revision"],
                    values["base_content_length"],
                    values["base_content_hash"],
                    values["base_redacted_stable_digest"],
                    values["delta_source_revision"],
                    values["delta_content_length"],
                    values["delta_content_hash"],
                    values["delta_redacted_stable_digest"],
                    values["delta_from_revision"],
                    values["delta_to_revision"],
                    values["delta_diff_hash"],
                    values["supersedes_overlay_id"],
                    values["materializes_overlay_id"],
                    status,
                    idempotency_key,
                )
                if existing is not None:
                    if tuple(existing) == expected:
                        return
                    raise ValueError("source overlay identity 冲突")
                duplicate = connection.execute(
                    "SELECT overlay_id FROM source_overlays WHERE session_id = ? AND checkpoint_ns = ? AND idempotency_key = ?",
                    (session_id, checkpoint_ns, idempotency_key),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(
                        "source overlay idempotency_key 已绑定其它 overlay_id: "
                        f"{idempotency_key}"
                    )
                latest_epoch = connection.execute(
                    "SELECT MAX(source_overlay_epoch) FROM source_overlays WHERE session_id = ? AND checkpoint_ns = ?",
                    (session_id, checkpoint_ns),
                ).fetchone()
                if latest_epoch is None:
                    raise RuntimeError("source overlay latest epoch 查询失败")
                latest_epoch_value = _optional_non_negative_int(
                    {"value": latest_epoch[0]}, "value"
                )
                if (
                    latest_epoch_value is not None
                    and source_overlay_epoch < latest_epoch_value
                ):
                    raise ValueError("source_overlay_epoch 不能回退")
                relation_targets: list[tuple[str, str]] = []
                for relation_name in (
                    "supersedes_overlay_id",
                    "materializes_overlay_id",
                ):
                    relation_id = values[relation_name]
                    if relation_id is None:
                        continue
                    if not isinstance(relation_id, str) or not relation_id:
                        raise ValueError(
                            f"source overlay {relation_name} 不能为空字符串"
                        )
                    if relation_id == overlay_id:
                        raise ValueError("source overlay lineage 不得自引用")
                    target = connection.execute(
                        "SELECT session_id, checkpoint_ns, status FROM source_overlays WHERE overlay_id = ?",
                        (relation_id,),
                    ).fetchone()
                    if target is None:
                        raise KeyError(
                            f"source overlay lineage target 不存在: {relation_name}={relation_id}"
                        )
                    target_session_id = _required_text(
                        {"session_id": target[0]}, "session_id"
                    )
                    target_checkpoint_ns = target[1]
                    if not isinstance(target_checkpoint_ns, str):
                        raise TypeError(
                            "source overlay lineage target checkpoint_ns 必须是字符串"
                        )
                    target_status = _required_text({"status": target[2]}, "status")
                    if (
                        target_session_id != session_id
                        or target_checkpoint_ns != checkpoint_ns
                    ):
                        raise ValueError(
                            "source overlay lineage 不得跨 session/checkpoint namespace"
                        )
                    if target_status != "active":
                        raise ValueError(
                            f"source overlay lineage target 不是 active: {relation_id}"
                        )
                    relation_targets.append((relation_name, relation_id))
                if values["materializes_overlay_id"] is not None and (
                    latest_epoch_value is not None
                    and source_overlay_epoch <= latest_epoch_value
                ):
                    raise ValueError(
                        "materializes source overlay 必须推进 source_overlay_epoch"
                    )
                for relation_name, relation_id in relation_targets:
                    next_status = (
                        "materialized"
                        if relation_name == "materializes_overlay_id"
                        else "superseded"
                    )
                    cursor = connection.execute(
                        "UPDATE source_overlays SET status = ? WHERE overlay_id = ? AND status = 'active'",
                        (next_status, relation_id),
                    )
                    _assert_one_row(cursor, context="source overlay lineage 状态更新")
                cursor = connection.execute(
                    "INSERT INTO source_overlays(overlay_id, session_id, checkpoint_ns, source_kind, source_revision, source_overlay_epoch, base_ref, delta_ref, base_source_revision, base_content_length, base_content_hash, base_redacted_stable_digest, delta_source_revision, delta_content_length, delta_content_hash, delta_redacted_stable_digest, delta_from_revision, delta_to_revision, delta_diff_hash, supersedes_overlay_id, materializes_overlay_id, status, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        overlay_id,
                        session_id,
                        checkpoint_ns,
                        source_kind,
                        source_revision,
                        source_overlay_epoch,
                        values["base_ref"],
                        values["delta_ref"],
                        values["base_source_revision"],
                        values["base_content_length"],
                        values["base_content_hash"],
                        values["base_redacted_stable_digest"],
                        values["delta_source_revision"],
                        values["delta_content_length"],
                        values["delta_content_hash"],
                        values["delta_redacted_stable_digest"],
                        values["delta_from_revision"],
                        values["delta_to_revision"],
                        values["delta_diff_hash"],
                        values["supersedes_overlay_id"],
                        values["materializes_overlay_id"],
                        status,
                        idempotency_key,
                        _now(),
                    ),
                )
                _assert_one_row(cursor, context="source overlay 写入")
                cursor = connection.execute(
                    "UPDATE database_meta SET source_overlay_epoch = MAX(source_overlay_epoch, ?), updated_at = ? WHERE singleton_id = 1",
                    (source_overlay_epoch, _now()),
                )
                _assert_one_row(cursor, context="source overlay database_meta 更新")
                connection.commit()

    def list_source_overlays(
        self,
        thread_id: str,
        *,
        source_overlay_epoch: int | None = None,
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("list source overlays thread_id 必须是非空字符串")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("list source overlays checkpoint_ns 必须是字符串")
        if source_overlay_epoch is not None and (
            not isinstance(source_overlay_epoch, int)
            or isinstance(source_overlay_epoch, bool)
            or source_overlay_epoch < 0
        ):
            raise ValueError("list source overlays epoch 必须是非负整数或 null")
        if snapshot is not None and (
            snapshot.thread_id != thread_id or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("source overlay snapshot 与目标 rollout 不一致")
        owned_connection = None
        if snapshot is None:
            self.initialize(thread_id, checkpoint_ns)
            owned_connection = self._connect(thread_id, checkpoint_ns, read_only=True)
        connection = (
            self._snapshot_connection(snapshot)
            if snapshot is not None
            else owned_connection
        )
        assert connection is not None
        try:
            self._require_v2_runtime(connection)
            query = "SELECT overlay_id, session_id, checkpoint_ns, source_kind, source_revision, source_overlay_epoch, base_ref, delta_ref, base_source_revision, base_content_length, base_content_hash, base_redacted_stable_digest, delta_source_revision, delta_content_length, delta_content_hash, delta_redacted_stable_digest, delta_from_revision, delta_to_revision, delta_diff_hash, supersedes_overlay_id, materializes_overlay_id, status, idempotency_key, created_at FROM source_overlays WHERE session_id = ? AND checkpoint_ns = ?"
            params: list[object] = [thread_id, checkpoint_ns]
            if source_overlay_epoch is not None:
                query += " AND source_overlay_epoch = ?"
                params.append(source_overlay_epoch)
            query += " ORDER BY source_overlay_epoch, created_at, overlay_id"
            rows = connection.execute(query, params).fetchall()
        finally:
            if owned_connection is not None:
                owned_connection.close()
        keys = (
            "overlay_id",
            "session_id",
            "checkpoint_ns",
            "source_kind",
            "source_revision",
            "source_overlay_epoch",
            "base_ref",
            "delta_ref",
            "base_source_revision",
            "base_content_length",
            "base_content_hash",
            "base_redacted_stable_digest",
            "delta_source_revision",
            "delta_content_length",
            "delta_content_hash",
            "delta_redacted_stable_digest",
            "delta_from_revision",
            "delta_to_revision",
            "delta_diff_hash",
            "supersedes_overlay_id",
            "materializes_overlay_id",
            "status",
            "idempotency_key",
            "created_at",
        )
        result = [dict(zip(keys, row, strict=True)) for row in rows]
        for row in result:
            _validate_source_overlay_row(
                row,
                session_id=thread_id,
                checkpoint_ns=checkpoint_ns,
            )
        return result
