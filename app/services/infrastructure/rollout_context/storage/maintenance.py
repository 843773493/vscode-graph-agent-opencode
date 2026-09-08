"""rollout storage 的跨文件维护、完整性校验与只读快照 owner。

这些能力不是 storage facade 的依赖组装职责：它们需要同时理解 JSONL
durability barrier、SQLite commit chain、备份文件和读取快照生命周期，统一
放在此处，避免 ``service.py`` 继续成为所有 storage 领域的聚合实现。
"""

from __future__ import annotations

import hashlib
import json

# 保留 storage maintenance 的 fsync monkeypatch seam，事务 owner 与它共享 os 模块对象。
import os  # noqa: F401
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.domain.itemized.enums import CommitKind, CommitMode
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_body,
    validate_projection_membership,
)
from app.services.infrastructure.rollout_context.storage.guards import (
    mark_jsonl_commit_attempted,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutCheckpointIndex,
    RolloutManifest,
    RolloutReadSnapshot,
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    validate_schema_journal,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    V2ItemCommitCoordinator,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
    validate_commit_contract,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{field} 必须是非负整数")
    return value


def _strict_optional_non_negative_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _strict_non_negative_int(value, field=field)


class RolloutStorageMaintenanceMixin:
    """维护 API 与跨文件完整性边界。"""

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        """提交已经完成 JSONL durability barrier 的 SQLite 事务。"""
        connection.commit()
        mark_jsonl_commit_attempted(connection)

    @staticmethod
    def _is_removed_rollout_layout(root: Path) -> bool:
        """判断目录是否仍是已经移除的旧 rollout 布局。"""
        return (root / "manifest.json").exists() or any(root.glob("segment-*.jsonl"))

    @staticmethod
    def _validate_schema_state(
        connection: sqlite3.Connection, *, allow_older_schema: bool = False,
        pending_retry: tuple[int, int, str, str | None] | None = None,
    ) -> None:
        removed_journal = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'compaction_runs'"
        ).fetchone()
        if removed_journal is not None and connection.execute(
            "SELECT 1 FROM compaction_runs LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError(
                "recovery-required: 检测到已移除的物理 compaction journal；"
                "保留原始 JSONL/SQLite/备份，禁止自动替换已提交事实"
            )
        row = connection.execute(
            "SELECT schema_version, message_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        schema_version = _strict_non_negative_int(
            row[0], field="database_meta.schema_version"
        )
        message_format_version = _strict_non_negative_int(
            row[1], field="database_meta.message_format_version"
        )
        if schema_version > storage_version.ROLLOUT_SCHEMA_VERSION:
            raise RuntimeError(
                "rollout SQLite schema 版本高于当前程序支持范围: "
                f"database={row[0]}, supported={storage_version.ROLLOUT_SCHEMA_VERSION}"
            )
        if schema_version < storage_version.ROLLOUT_SCHEMA_VERSION and not allow_older_schema:
            raise RuntimeError(
                "schema-upgrade-required: v2 SQLite 必须显式执行 Saver.upgrade_rollout_schema，"
                f"current={schema_version}, target={storage_version.ROLLOUT_SCHEMA_VERSION}"
            )
        if message_format_version != storage_version.MESSAGE_FORMAT_VERSION:
            raise RuntimeError(
                "rollout JSONL message format 版本不受支持: "
                f"database={row[1]}, supported={storage_version.MESSAGE_FORMAT_VERSION}"
            )
        if pending_retry is not None and not allow_older_schema:
            raise RuntimeError("schema-upgrade-retry-conflict: 普通 runtime 不允许失败迁移重试")
        validate_schema_journal(connection, schema_version, pending_retry=pending_retry)

    @staticmethod
    def _validate_v2_commit_offsets(
        connection: sqlite3.Connection,
        jsonl_path: Path,
        *,
        validate_jsonl_items: bool = True,
    ) -> None:
        """校验 committed offset 的单一权威和 storage commit 链。"""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in columns:
            return
        row = connection.execute(
            "SELECT rollout_format_version, committed_jsonl_offset, last_commit_id "
            "FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None or _strict_non_negative_int(
            row[0], field="database_meta.rollout_format_version"
        ) != storage_version.ROLLOUT_FORMAT_VERSION:
            return
        database_offset = _strict_non_negative_int(
            row[1], field="database_meta.committed_jsonl_offset"
        )
        database_last_commit_id = _strict_optional_non_negative_int(
            row[2], field="database_meta.last_commit_id"
        )
        jsonl_size = jsonl_path.stat().st_size
        commits = connection.execute(
            "SELECT commit_id, jsonl_start_offset, jsonl_end_offset, "
            "jsonl_offset_before, jsonl_offset_after, jsonl_record_count, status, "
            "commit_kind, commit_mode, outcome, metadata_json, jsonl_fsync_at "
            "FROM storage_commits ORDER BY commit_id"
        ).fetchall()
        previous = 0
        commit_kinds = {value.value for value in CommitKind}
        commit_modes = {value.value for value in CommitMode}
        for commit in commits:
            commit_id = _strict_non_negative_int(
                commit[0], field="storage_commits.commit_id"
            )
            start = _strict_non_negative_int(
                commit[1], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end = _strict_non_negative_int(
                commit[2], field=f"storage commit jsonl_end_offset: {commit_id}"
            )
            before = _strict_non_negative_int(
                commit[3], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end_offset = _strict_non_negative_int(
                commit[4], field=f"storage commit jsonl_offset_after: {commit_id}"
            )
            record_count = _strict_non_negative_int(
                commit[5], field=f"storage commit jsonl_record_count: {commit_id}"
            )
            status = strict_text(commit[6], field=f"storage commit status: {commit_id}")
            if status != "committed":
                raise RuntimeError(f"v2 storage commit 未收敛: commit_id={commit_id}")
            commit_kind = strict_text(
                commit[7], field=f"storage commit kind: {commit_id}"
            )
            commit_mode = strict_text(
                commit[8], field=f"storage commit mode: {commit_id}"
            )
            if commit_kind not in commit_kinds:
                raise RuntimeError(
                    f"v2 storage commit kind 非法: commit_id={commit_id}, kind={commit_kind}"
                )
            if commit_mode not in commit_modes:
                raise RuntimeError(
                    f"v2 storage commit mode 非法: commit_id={commit_id}, mode={commit_mode}"
                )
            after = _strict_non_negative_int(
                commit[4], field=f"storage commit jsonl_offset_after: {commit_id}"
            )
            if end_offset != after:
                raise RuntimeError(
                    f"v2 storage commit end offset 字段不一致: commit_id={commit_id}"
                )
            outcome = commit[9]
            if outcome is not None and not isinstance(outcome, str):
                raise RuntimeError(
                    f"v2 storage commit outcome 必须是字符串或 null: commit_id={commit_id}"
                )
            try:
                validate_commit_contract(
                    commit_kind=commit_kind,
                    commit_mode=commit_mode,
                    item_count=record_count,
                    outcome=outcome,
                )
            except (ItemSchemaError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"v2 storage commit contract 非法: commit_id={commit_id}: {error}"
                ) from error
            metadata_json = commit[10]
            if not isinstance(metadata_json, str) or not metadata_json:
                raise RuntimeError(
                    f"v2 storage commit metadata_json 必须是非空字符串: commit_id={commit_id}"
                )
            try:
                metadata_value = json.loads(metadata_json)
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"v2 storage commit metadata_json 非法: commit_id={commit_id}"
                ) from error
            if not isinstance(metadata_value, Mapping) or _json(metadata_value) != metadata_json:
                raise RuntimeError(
                    "v2 storage commit metadata_json 不是 RFC 8785 JCS object: "
                    f"commit_id={commit_id}"
                )
            if "physical_record_count" in metadata_value or "compacted" in metadata_value:
                raise RuntimeError(
                    "immutable JSONL 不允许已提交记录被物理压缩或重写: "
                    f"commit_id={commit_id}"
                )
            if start != before or end != after:
                raise RuntimeError(
                    "v2 storage commit JSONL offset 链断裂（offset 字段不一致）: "
                    f"commit_id={commit_id}, before={before}, start={start}, "
                    f"end={end}, after={after}"
                )
            if before != previous or after < before:
                raise RuntimeError(
                    "v2 storage commit JSONL offset 链断裂: "
                    f"commit_id={commit_id}, previous={previous}, before={before}, "
                    f"start={start}, end={end}, after={after}"
                )
            if (
                commit_mode == CommitMode.ITEM_BEARING.value
                and not isinstance(commit[11], str)
            ):
                raise RuntimeError(
                    f"item-bearing commit 缺少 JSONL fsync barrier: commit_id={commit_id}"
                )
            if commit[11] is not None and (
                not isinstance(commit[11], str) or not commit[11]
            ):
                raise RuntimeError(
                    f"v2 storage commit fsync timestamp 非法: commit_id={commit_id}"
                )
            if commit_mode == CommitMode.METADATA_ONLY.value and (
                start != end or record_count != 0
            ):
                raise RuntimeError(
                    f"metadata-only commit 不得推进 JSONL offset: commit_id={commit_id}"
                )
            if commit_mode == CommitMode.ITEM_BEARING.value and record_count <= 0:
                raise RuntimeError(
                    f"item-bearing commit 必须包含 item: commit_id={commit_id}"
                )
            catalog_row = connection.execute(
                "SELECT COUNT(*) FROM item_catalog WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
            catalog_count = _strict_non_negative_int(
                catalog_row[0], field=f"item catalog count: {commit_id}"
            )
            if catalog_count != record_count:
                raise RuntimeError(
                    "storage commit 的 item catalog 数量不一致: "
                    f"commit_id={commit_id}, catalog={catalog_count}, "
                    f"record_count={record_count}"
                )
            previous = after
        if commits and database_last_commit_id != _strict_non_negative_int(
            commits[-1][0], field="storage_commits.last_commit_id"
        ):
            raise RuntimeError(
                "database_meta.last_commit_id 与 storage_commits 不一致: "
                f"meta={database_last_commit_id}, commits={commits[-1][0]}"
            )
        if not commits and database_last_commit_id is not None:
            raise RuntimeError(
                "database_meta.last_commit_id 指向不存在的 storage commit: "
                f"{database_last_commit_id}"
            )
        # 即使调用方省略逐 item 正文校验，commit chain 仍必须与 meta
        # 等值。尾部尚未收敛的字节可以存在，但不得用于选择另一边界。
        if previous != database_offset or jsonl_size < database_offset:
            raise RuntimeError(
                "database_meta.committed_jsonl_offset 与 storage_commits 不一致: "
                f"meta={database_offset}, commits={previous}, file={jsonl_size}"
            )
        jsonl_bytes = b""
        if validate_jsonl_items:
            with jsonl_path.open("rb") as stream:
                jsonl_bytes = stream.read(database_offset)

        item_rows = connection.execute(
            "SELECT item_sequence, item_id, content_hash, jsonl_offset, jsonl_length, "
            "commit_id, payload_length, source_revision FROM item_catalog ORDER BY jsonl_offset"
        ).fetchall()
        expected_item_offset = 0
        commit_ranges = {
            _strict_non_negative_int(commit[0], field="storage commit id"): (
                _strict_non_negative_int(commit[1], field="storage commit start offset"),
                _strict_non_negative_int(commit[2], field="storage commit end offset"),
                _strict_non_negative_int(commit[5], field="storage commit record count"),
            )
            for commit in commits
        }
        expected_item_sequence = 1
        for (
            item_sequence,
            item_id,
            catalog_hash,
            item_offset,
            item_length,
            item_commit_id,
            logical_length,
            source_revision,
        ) in item_rows:
            item_id = strict_text(item_id, field="item_catalog.item_id")
            logical_length = _strict_non_negative_int(
                logical_length, field=f"item_catalog.payload_length: {item_id}"
            )
            source_revision = strict_text(
                source_revision, field=f"item_catalog.source_revision: {item_id}"
            )
            sequence = _strict_non_negative_int(
                item_sequence, field=f"item_catalog.item_sequence: {item_id}"
            )
            offset = _strict_non_negative_int(
                item_offset, field=f"item_catalog.jsonl_offset: {item_id}"
            )
            length = _strict_non_negative_int(
                item_length, field=f"item_catalog.jsonl_length: {item_id}"
            )
            catalog_hash = strict_text(
                catalog_hash,
                field=f"item_catalog.content_hash: {item_id}",
            )
            if length == 0:
                raise RuntimeError(
                    f"item_catalog.jsonl_length 必须大于 0: {item_id}"
                )
            if sequence != expected_item_sequence:
                raise RuntimeError(
                    "v2 item catalog item_sequence 不连续或顺序非法，拒绝使用派生索引: "
                    f"item_id={item_id}, sequence={sequence}, "
                    f"expected={expected_item_sequence}"
                )
            if offset != expected_item_offset or length <= 0:
                raise RuntimeError(
                    "v2 item catalog JSONL locator 不连续或非法: "
                    f"item_id={item_id}, sequence={sequence}, offset={offset}, "
                    f"length={length}, expected_offset={expected_item_offset}"
                )
            end = offset + length
            if end > database_offset:
                raise RuntimeError(
                    f"v2 item catalog JSONL locator 越界: item_id={item_id}"
                )
            if validate_jsonl_items:
                validate_catalog_body(
                    jsonl_bytes[offset:end],
                    sequence=sequence,
                    item_id=item_id,
                    catalog_hash=catalog_hash,
                    payload_length=logical_length,
                    source_revision=source_revision,
                )
            item_commit_id_value = _strict_non_negative_int(
                item_commit_id, field=f"item_catalog.commit_id: {item_id}"
            )
            commit_range = commit_ranges.get(item_commit_id_value)
            if commit_range is None:
                raise RuntimeError(
                    f"v2 item catalog 指向不存在的 storage commit: item_id={item_id}"
                )
            commit_start, commit_end, commit_record_count = commit_range
            if (
                item_offset < commit_start
                or end > commit_end
                or commit_record_count <= 0
            ):
                raise RuntimeError(
                    "storage commit 的 item catalog offset 不在 commit 边界内: "
                    f"commit_id={item_commit_id_value}, item_id={item_id}, "
                    f"item_start={item_offset}, item_end={end}, "
                    f"start={commit_start}, end={commit_end}"
                )
            expected_item_offset = end
            expected_item_sequence += 1
        if expected_item_offset != database_offset:
            raise RuntimeError(
                "database_meta.committed_jsonl_offset 与 storage_commits 不一致: "
                f"meta={database_offset}, commits={previous}, "
                f"catalog_end={expected_item_offset}, file={jsonl_size}"
            )
        validate_projection_membership(connection)

    def open_read_snapshot(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_integrity: bool = False,
    ) -> RolloutReadSnapshot:
        """打开带文件锁和 SQLite 事务的只读 snapshot。"""
        root = self.root(thread_id, checkpoint_ns)
        jsonl_path = self.jsonl_path(thread_id, checkpoint_ns)
        index_path = self.index_path(thread_id, checkpoint_ns)
        if not root.is_dir() or not jsonl_path.is_file() or not index_path.is_file():
            self.initialize(thread_id, checkpoint_ns)
        file_lock = _RolloutFileLock(root.parent / ".rollout.write.lock", exclusive=False)
        file_lock.acquire()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect(thread_id, checkpoint_ns, read_only=True)
            self._validate_schema_state(connection)
            self._validate_reasoning_projection_connection(connection)
            self._require_v2_runtime(connection)
            self._validate_v2_commit_offsets(
                connection, jsonl_path, validate_jsonl_items=validate_integrity
            )
            database_state = connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if database_state is None or (
                strict_text(database_state[0], field="database_meta.database_state")
                == "migrating"
            ):
                raise RuntimeError(
                    "rollout 正在进行 legacy migration，暂不可建立业务 context snapshot"
                )
            committed_row = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if committed_row is None:
                raise RuntimeError("database_meta 缺少 committed_jsonl_offset")
            committed_offset = _strict_non_negative_int(
                committed_row[0],
                field="database_meta.committed_jsonl_offset",
            )
            if jsonl_path.stat().st_size < committed_offset:
                raise RuntimeError("rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复")
            integrity = (
                connection.execute("PRAGMA integrity_check").fetchone()[0]
                if validate_integrity
                else "ok"
            )
            if integrity != "ok":
                connection.rollback()
                connection.close()
                file_lock.release()
                with (
                    self._lock(thread_id, checkpoint_ns),
                    self._connect(thread_id, checkpoint_ns) as writable,
                ):
                    writable.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                raise RuntimeError(
                    "rollout SQLite integrity_check 失败: "
                    f"{self.index_path(thread_id, checkpoint_ns)}: {integrity}"
                )
            connection.execute("BEGIN")
            manifest = self._manifest_from_connection(connection, checkpoint_ns)
        except Exception:
            if connection is not None:
                connection.close()
            file_lock.release()
            raise
        return RolloutReadSnapshot(
            thread_id,
            checkpoint_ns,
            manifest,
            connection,
            file_lock,
        )

    def validate_index(self, thread_id: str, checkpoint_ns: str = "") -> RolloutReadSnapshot:
        """执行完整 SQLite integrity_check。"""
        return self.open_read_snapshot(thread_id, checkpoint_ns, validate_integrity=True)

    def repair_index(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        manifest: RolloutManifest | None = None,
    ) -> None:
        del manifest
        raise RuntimeError(
            "SQLite 是 rollout 控制状态的权威来源，不能从 rollout.jsonl 重建 index；请恢复 SQLite 备份"
        )

    def _append_v2_records_transaction(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
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
        """通过统一 coordinator 执行 v2 item-bearing 原子提交。"""
        coordinator = V2ItemCommitCoordinator(
            jsonl_path=self.jsonl_path(thread_id, checkpoint_ns),
            canonical_writer=self._insert_canonical_item,
        )
        return coordinator.append(
            connection,
            items,
            commit_kind=commit_kind,
            commit_mode=commit_mode,
            outcome=outcome,
            metadata=metadata,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            begin_transaction=begin_transaction,
        )

    def _checkpoint_index(self, row: Sequence[object]) -> RolloutCheckpointIndex:
        if len(row) != 16:
            raise RuntimeError(
                f"checkpoint index 列数非法: expected=16, got={len(row)}"
            )
        blobs = (row[13], row[15])
        if any(not isinstance(blob, bytes) for blob in blobs):
            raise RuntimeError("checkpoint index blob 字段必须是 bytes")
        return RolloutCheckpointIndex(
            strict_text(row[0], field="checkpoints.checkpoint_id"),
            strict_text(row[1], field="checkpoints.checkpoint_ns", allow_empty=True),
            strict_non_negative_int(row[2], field="checkpoints.commit_id"),
            strict_non_negative_int(
                row[3], field="checkpoints.message_sequence"
            ),
            strict_non_negative_int(row[4], field="checkpoints.message_count"),
            strict_optional_text(
                row[5], field="checkpoints.parent_checkpoint_id"
            ),
            strict_text(row[6], field="checkpoints.view_id"),
            strict_text(row[7], field="checkpoints.branch_id"),
            strict_non_negative_int(
                row[8], field="checkpoints.checkpoint_version"
            ),
            strict_text(row[9], field="checkpoints.checkpoint_timestamp"),
            strict_text(row[10], field="checkpoints.checkpoint_json"),
            strict_text(row[11], field="checkpoints.metadata_json"),
            strict_text(row[12], field="checkpoints.versions_seen_type"),
            blobs[0],
            strict_text(row[14], field="checkpoints.pending_sends_type"),
            blobs[1],
        )

    def rollout_id(self, thread_id: str, checkpoint_ns: str = "") -> str:
        manifest = self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
        return manifest.rollout_id
