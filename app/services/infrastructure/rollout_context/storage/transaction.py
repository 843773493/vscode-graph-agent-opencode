"""v2 canonical item 的 JSONL/SQLite 原子提交协调器。

该模块只负责 item-bearing commit 的跨文件 durability barrier。SQLite schema、
projection 生成和 session 业务状态仍由上层 storage owner 注入，避免把事务
细节复制到 checkpoint、migration 或 projector 中。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.domain.itemized.enums import CommitKind, CommitMode, ControlOutcome
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.guards import (
    register_jsonl_guard,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)

CanonicalItemWriter = Callable[
    [sqlite3.Connection, CanonicalItemRecord, int, int, int, str], None
]


def validate_commit_contract(
    *,
    commit_kind: str,
    commit_mode: str,
    item_count: int,
    outcome: str | None,
) -> None:
    """冻结 commit kind/mode 与 item/outcome 的组合。"""
    if not isinstance(commit_kind, str) or not commit_kind:
        raise ItemSchemaError("storage commit kind 必须是非空字符串")
    if not isinstance(commit_mode, str) or not commit_mode:
        raise ItemSchemaError("storage commit mode 必须是非空字符串")
    if (
        not isinstance(item_count, int)
        or isinstance(item_count, bool)
        or item_count < 0
    ):
        raise ItemSchemaError("storage commit item_count 必须是非负整数")
    if outcome is not None and not isinstance(outcome, str):
        raise ItemSchemaError("storage commit outcome 必须是字符串或 null")
    if commit_kind not in {value.value for value in CommitKind}:
        raise ItemSchemaError(f"未知 storage commit kind: {commit_kind}")
    if commit_mode not in {value.value for value in CommitMode}:
        raise ItemSchemaError(f"未知 storage commit mode: {commit_mode}")
    if commit_mode == CommitMode.ITEM_BEARING.value and item_count <= 0:
        raise ItemSchemaError("item-bearing commit 必须包含 canonical item")
    if commit_mode == CommitMode.METADATA_ONLY.value and item_count != 0:
        raise ItemSchemaError("metadata-only commit 不得包含 canonical item")
    if outcome is not None and outcome not in {value.value for value in ControlOutcome}:
        raise ItemSchemaError(f"未知 storage commit outcome: {outcome}")
    if commit_kind == CommitKind.ACCEPTANCE.value and commit_mode != CommitMode.ITEM_BEARING.value:
        raise ItemSchemaError("acceptance commit 必须是 item-bearing")
    if commit_kind == CommitKind.ASSEMBLY_SEALED.value and commit_mode != CommitMode.METADATA_ONLY.value:
        raise ItemSchemaError("assembly_sealed commit 必须是 metadata-only")
    if commit_kind == CommitKind.ITEM_CONVERGENCE.value and commit_mode != CommitMode.ITEM_BEARING.value:
        raise ItemSchemaError("item_convergence commit 必须是 item-bearing")
    if commit_kind == CommitKind.TERMINAL_CONVERGENCE.value and not outcome:
        raise ItemSchemaError("terminal_convergence commit 必须包含 outcome")
    if (
        commit_kind == CommitKind.TERMINAL_CONVERGENCE.value
        and outcome == ControlOutcome.COMPLETED_EMPTY.value
        and commit_mode != CommitMode.METADATA_ONLY.value
    ):
        raise ItemSchemaError("completed_empty terminal convergence 必须是 metadata-only")


def default_idempotency_key(
    *,
    commit_kind: str,
    subject_id: str | None,
    outcome: str | None,
    metadata: Mapping[str, object],
) -> str:
    """在 transaction owner 内按 JCS metadata 生成稳定幂等键。"""
    from app.domain.itemized.hashing import sha256_jcs

    return (
        f"{commit_kind}:{subject_id or ''}:{outcome or ''}:"
        f"{sha256_jcs(dict(metadata))}"
    )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _required_text(value: object, *, field: str) -> str:
    """读取提交账本中的必填文本，不把 SQLite 值强转成字符串。"""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} 必须是非空字符串")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _non_negative_int(value: object, *, field: str) -> int:
    """读取提交账本中的整数，明确拒绝 bool 和负数。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{field} 必须是非负整数")
    return value


def _canonical_metadata_text(value: object, *, field: str) -> str:
    """验证持久化 metadata 是 canonical JSON object，而不是可被兜底的垃圾值。"""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} 必须是非空 JSON object 字符串")
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{field} 不是合法 JSON object") from error
    if not isinstance(decoded, dict) or _json(decoded) != value:
        raise RuntimeError(f"{field} 不是 RFC 8785 JCS object")
    return value




def _validate_item_storage_metadata(item: CanonicalItemRecord) -> tuple[str | None, int]:
    """校验会影响 catalog/part locator 的 item metadata。"""
    metadata = item.metadata
    raw_revision = metadata.get("source_revision")
    if raw_revision is None:
        source_revision = None
    elif isinstance(raw_revision, str) and raw_revision:
        source_revision = raw_revision
    else:
        raise ItemSchemaError(
            f"canonical item source_revision 必须是非空字符串: {item.item_id}"
        )

    has_block_id = "block_id" in metadata
    has_block_index = "block_index" in metadata
    if has_block_index and not has_block_id:
        raise ItemSchemaError(
            f"canonical item block_index 不得脱离 block_id: {item.item_id}"
        )
    if not has_block_id:
        return source_revision, 0
    block_id = metadata.get("block_id")
    if not isinstance(block_id, str) or not block_id:
        raise ItemSchemaError(
            f"canonical item block_id 必须是非空字符串: {item.item_id}"
        )
    raw_index = metadata.get("block_index", 0)
    if (
        not isinstance(raw_index, int)
        or isinstance(raw_index, bool)
        or raw_index < 0
    ):
        raise ItemSchemaError(
            f"canonical item block_index 必须是非负整数: {item.item_id}"
        )
    return source_revision, raw_index


def strict_non_negative_int(value: object, *, field: str) -> int:
    """向其它 durable owner 暴露统一的 SQLite 非负整数读取合同。"""
    return _non_negative_int(value, field=field)


def strict_optional_non_negative_int(value: object, *, field: str) -> int | None:
    """读取允许 NULL 的 SQLite 非负整数列。"""
    if value is None:
        return None
    return _non_negative_int(value, field=field)


def strict_text(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "字符串" if allow_empty else "非空字符串"
        raise RuntimeError(f"{field} 必须是{suffix}")
    return value


def strict_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return strict_text(value, field=field)


def validate_item_storage_metadata(
    item: CanonicalItemRecord,
) -> tuple[str | None, int]:
    """向 checkpoint item writer 暴露统一的 catalog metadata 校验。"""
    return _validate_item_storage_metadata(item)


@dataclass(frozen=True, slots=True)
class CommittedStorageCommit:
    """已提交幂等记录的严格投影，供所有 terminal caller 复用。"""

    commit_id: int
    commit_kind: str
    commit_mode: str
    outcome: str | None
    metadata_json: str
    record_count: int
    offset_before: int
    offset_after: int


def _decode_committed_storage_commit(
    row: Sequence[object],
) -> CommittedStorageCommit:
    """把 storage_commits 行解析成不可变的已提交 commit 投影。"""
    existing_id = _non_negative_int(row[0], field="storage_commits.commit_id")
    stored_kind = _required_text(row[1], field="storage_commits.commit_kind")
    stored_mode = _required_text(row[2], field="storage_commits.commit_mode")
    stored_outcome = _optional_text(row[3], field="storage_commits.outcome")
    stored_metadata = _canonical_metadata_text(
        row[4], field="storage_commits.metadata_json"
    )
    stored_record_count = _non_negative_int(
        row[5], field="storage_commits.jsonl_record_count"
    )
    stored_offset_before = _non_negative_int(
        row[6], field="storage_commits.jsonl_offset_before"
    )
    stored_offset_after = _non_negative_int(
        row[7], field="storage_commits.jsonl_offset_after"
    )
    stored_start = _non_negative_int(
        row[8], field="storage_commits.jsonl_start_offset"
    )
    stored_end = _non_negative_int(row[9], field="storage_commits.jsonl_end_offset")
    stored_status = _required_text(row[10], field="storage_commits.status")
    if stored_status != "committed":
        raise RuntimeError(
            "storage commit 尚未 committed，禁止把 prepared 状态当作成功: "
            f"commit_id={existing_id}, status={stored_status}"
        )
    if (
        stored_start != stored_offset_before
        or stored_end != stored_offset_after
        or stored_offset_after < stored_offset_before
    ):
        raise RuntimeError(
            "storage commit offset 字段不一致: "
            f"commit_id={existing_id}"
        )
    if stored_record_count == 0 and stored_offset_after != stored_offset_before:
        raise RuntimeError(
            "metadata-only storage commit 不得推进 JSONL offset: "
            f"commit_id={existing_id}"
        )
    validate_commit_contract(
        commit_kind=stored_kind,
        commit_mode=stored_mode,
        item_count=stored_record_count,
        outcome=stored_outcome,
    )
    return CommittedStorageCommit(
        commit_id=existing_id,
        commit_kind=stored_kind,
        commit_mode=stored_mode,
        outcome=stored_outcome,
        metadata_json=stored_metadata,
        record_count=stored_record_count,
        offset_before=stored_offset_before,
        offset_after=stored_offset_after,
    )


def load_committed_storage_commit(
    connection: sqlite3.Connection,
    *,
    commit_id: int,
) -> CommittedStorageCommit:
    """严格读取任意已提交 commit，供 checkpoint 指针做引用校验。"""
    if not isinstance(commit_id, int) or isinstance(commit_id, bool) or commit_id < 0:
        raise RuntimeError("storage_commits.commit_id 必须是非负整数")
    row = connection.execute(
        "SELECT commit_id, commit_kind, commit_mode, outcome, metadata_json, "
        "jsonl_record_count, jsonl_offset_before, jsonl_offset_after, "
        "jsonl_start_offset, jsonl_end_offset, status "
        "FROM storage_commits WHERE commit_id = ?",
        (commit_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"checkpoint 引用了不存在的 storage commit: {commit_id}")
    return _decode_committed_storage_commit(row)


def load_committed_idempotency_commit(
    connection: sqlite3.Connection,
    *,
    commit_kind: str,
    subject_id: str | None,
    idempotency_key: str,
    commit_mode: str,
    outcome: str | None,
    metadata: Mapping[str, object],
    item_count: int,
) -> CommittedStorageCommit | None:
    """严格读取一个幂等 commit，拒绝把损坏账本当作成功重放。"""
    row = connection.execute(
        "SELECT commit_id, commit_kind, commit_mode, outcome, metadata_json, "
        "jsonl_record_count, jsonl_offset_before, jsonl_offset_after, "
        "jsonl_start_offset, jsonl_end_offset, status "
        "FROM storage_commits WHERE commit_kind = ? "
        "AND ((subject_id = ?) OR (subject_id IS NULL AND ? IS NULL)) "
        "AND idempotency_key = ? ORDER BY commit_id LIMIT 1",
        (commit_kind, subject_id, subject_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None

    existing_commit = _decode_committed_storage_commit(row)
    if existing_commit.commit_kind != commit_kind:
        raise RuntimeError(
            "storage commit 幂等记录 commit_kind 与查询条件不一致: "
            f"commit_id={existing_commit.commit_id}"
        )
    requested_metadata = _json(dict(metadata))
    if (
        existing_commit.commit_mode != commit_mode
        or existing_commit.outcome != outcome
        or existing_commit.metadata_json != requested_metadata
    ):
        raise ValueError("storage commit 幂等键冲突：mode/outcome/metadata 不一致")
    if existing_commit.record_count != item_count:
        raise ValueError("storage commit 幂等键冲突：item 数量不一致")
    return existing_commit


def read_committed_item_identities(
    connection: sqlite3.Connection,
    *,
    commit_id: int,
) -> tuple[tuple[str, str, int], ...]:
    """严格读取 commit 绑定的 canonical item identity。"""
    return tuple(
        (
            _required_text(row[0], field="item_catalog.item_id"),
            _required_text(row[1], field="item_catalog.content_hash"),
            _non_negative_int(row[2], field="item_catalog.item_sequence"),
        )
        for row in connection.execute(
            "SELECT item_id, content_hash, item_sequence FROM item_catalog "
            "WHERE commit_id = ? ORDER BY item_sequence",
            (commit_id,),
        ).fetchall()
    )


class V2ItemCommitCoordinator:
    """执行一批已经校验的 immutable item commit。"""

    def __init__(
        self,
        *,
        jsonl_path: Path,
        canonical_writer: CanonicalItemWriter,
    ) -> None:
        self._jsonl_path = jsonl_path
        self._canonical_writer = canonical_writer

    def append(
        self,
        connection: sqlite3.Connection,
        items: Sequence[CanonicalItemRecord],
        *,
        commit_kind: str,
        commit_mode: str | None = None,
        outcome: str | None = None,
        metadata: Mapping[str, object] | None = None,
        subject_id: str | None = None,
        idempotency_key: str | None = None,
        begin_transaction: bool = True,
    ) -> tuple[int, int]:
        if commit_mode is None:
            commit_mode = (
                CommitMode.ITEM_BEARING.value
                if items
                else CommitMode.METADATA_ONLY.value
            )
        validate_commit_contract(
            commit_kind=commit_kind,
            commit_mode=commit_mode,
            item_count=len(items),
            outcome=outcome,
        )
        if metadata is None:
            commit_metadata: dict[str, object] = {}
        elif isinstance(metadata, Mapping):
            commit_metadata = dict(metadata)
        else:
            raise TypeError("storage commit metadata 必须是 object")
        canonical_json_bytes(commit_metadata)
        if subject_id is not None and (
            not isinstance(subject_id, str) or not subject_id
        ):
            raise TypeError("storage commit subject_id 必须是非空字符串或 null")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key
        ):
            raise TypeError(
                "storage commit idempotency_key 必须是非空字符串或 null"
            )
        if idempotency_key is None:
            idempotency_key = default_idempotency_key(
                commit_kind=commit_kind,
                subject_id=subject_id,
                outcome=outcome,
                metadata=commit_metadata,
            )

        existing_commit = load_committed_idempotency_commit(
            connection,
            commit_kind=commit_kind,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            commit_mode=commit_mode,
            outcome=outcome,
            metadata=commit_metadata,
            item_count=len(items),
        )
        if existing_commit is not None:
            existing_items = read_committed_item_identities(
                connection,
                commit_id=existing_commit.commit_id,
            )
            requested_items = tuple(
                (item.item_id, item.content_hash, item.item_sequence) for item in items
            )
            if existing_items != requested_items:
                raise ValueError(
                    "storage commit 幂等键冲突：item identity/content/sequence 不一致"
                )
            return existing_commit.commit_id, existing_commit.offset_after

        meta = connection.execute(
            "SELECT last_item_sequence, committed_jsonl_offset FROM database_meta "
            "WHERE singleton_id = 1"
        ).fetchone()
        if meta is None:
            raise RuntimeError("rollout database_meta 缺失")
        last_item_sequence = _non_negative_int(
            meta[0], field="database_meta.last_item_sequence"
        )
        current_offset = _non_negative_int(
            meta[1], field="database_meta.committed_jsonl_offset"
        )
        if current_offset != self._jsonl_path.stat().st_size:
            raise RuntimeError("rollout JSONL 与 database_meta committed offset 不一致")
        original_file_size = self._jsonl_path.stat().st_size
        prepared: list[tuple[CanonicalItemRecord, bytes, int]] = []
        offset = current_offset
        for item in items:
            item.validate()
            _validate_item_storage_metadata(item)
            expected_sequence = last_item_sequence + len(prepared) + 1
            if item.item_sequence != expected_sequence:
                raise ValueError(
                    "item_sequence 必须从 last_item_sequence 连续递增: "
                    f"expected={expected_sequence}, got={item.item_sequence}"
                )
            existing = connection.execute(
                "SELECT content_hash, item_sequence FROM item_catalog WHERE item_id = ?",
                (item.item_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != item.content_hash:
                    raise ItemSchemaError(f"canonical item_id 内容不可变: {item.item_id}")
                raise ValueError(f"canonical item_id 已存在: {item.item_id}")
            # rollout.jsonl 只保存 CanonicalItemRecord；LangChain message 是
            # 可重建 projection，不能作为第二份 payload/identity 事实随 item
            # 写入。旧 envelope 不在 v2 runtime 中降级读取，必须经过显式迁移。
            raw = canonical_json_line(item.to_dict())
            prepared.append((item, raw, offset))
            offset += len(raw)

        transaction_started = False
        try:
            if begin_transaction:
                connection.execute("BEGIN IMMEDIATE")
                transaction_started = True
            else:
                if not connection.in_transaction:
                    raise RuntimeError(
                        "begin_transaction=False 要求调用方已开启同一 SQLite 事务"
                    )
            register_jsonl_guard(
                connection,
                self._jsonl_path,
                original_size=original_file_size,
            )
            if prepared:
                with self._jsonl_path.open("ab") as stream:
                    for _item, raw, _item_offset in prepared:
                        stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            timestamp = _now()
            commit_cursor = connection.execute(
                "INSERT INTO storage_commits(transaction_id, first_message_sequence, "
                "last_message_sequence, jsonl_start_offset, jsonl_end_offset, "
                "jsonl_fsync_at, status, created_at, commit_kind, commit_mode, "
                "jsonl_offset_before, jsonl_offset_after, jsonl_record_count, "
                "subject_id, idempotency_key, outcome, metadata_json) VALUES "
                "(?, NULL, NULL, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    current_offset,
                    offset,
                    timestamp if prepared else None,
                    timestamp,
                    commit_kind,
                    commit_mode,
                    current_offset,
                    offset,
                    len(prepared),
                    subject_id,
                    idempotency_key,
                    outcome,
                    _json(commit_metadata),
                ),
            )
            commit_id = _non_negative_int(
                commit_cursor.lastrowid, field="storage_commits.commit_id"
            )
            for item, raw, item_offset in prepared:
                self._canonical_writer(
                    connection,
                    item,
                    commit_id=commit_id,
                    jsonl_offset=item_offset,
                    jsonl_length=len(raw),
                    created_at=timestamp,
                )
            committed_cursor = connection.execute(
                "UPDATE storage_commits SET status = 'committed', committed_at = ? "
                "WHERE commit_id = ?",
                (timestamp, commit_id),
            )
            if committed_cursor.rowcount != 1:
                raise RuntimeError(
                    "storage commit 收敛更新行数异常: "
                    f"commit_id={commit_id}, rowcount={committed_cursor.rowcount}"
                )
            meta_cursor = connection.execute(
                "UPDATE database_meta SET last_commit_id = ?, committed_jsonl_offset = ?, "
                "last_item_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                (commit_id, offset, last_item_sequence + len(prepared), timestamp),
            )
            if meta_cursor.rowcount != 1:
                raise RuntimeError(
                    "database_meta 收敛更新行数异常: "
                    f"commit_id={commit_id}, rowcount={meta_cursor.rowcount}"
                )
            return commit_id, offset
        except BaseException:
            if transaction_started:
                connection.rollback()
            if transaction_started and self._jsonl_path.stat().st_size > original_file_size:
                with self._jsonl_path.open("r+b") as stream:
                    stream.truncate(original_file_size)
                    stream.flush()
                    os.fsync(stream.fileno())
            raise


__all__ = [
    "CommittedStorageCommit",
    "V2ItemCommitCoordinator",
    "load_committed_idempotency_commit",
    "load_committed_storage_commit",
    "read_committed_item_identities",
    "strict_non_negative_int",
    "strict_optional_non_negative_int",
    "strict_optional_text",
    "strict_text",
    "validate_commit_contract",
    "validate_item_storage_metadata",
]
