"""rollout schema 自省与 commit-offset 校验（只依赖连接与参数）。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
)
from app.domain.itemized.errors import (
    FormatDispatchError,
    ItemSchemaError,
)
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
    RolloutManifest,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    validate_schema_journal,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
    validate_commit_contract,
)

_DEFAULT_NAMESPACE = ""


__all__ = ["RolloutSchemaIntrospectionMixin"]


class RolloutSchemaIntrospectionMixin:
    def _manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        *,
        allow_migrating: bool = False,
    ) -> RolloutManifest:
        meta_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in meta_columns:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version"
            )
        row = connection.execute(
            "SELECT rollout_id, active_branch_id, last_message_sequence, projection_epoch, last_commit_id, database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        rollout_id = strict_text(row[0], field="database_meta.rollout_id")
        active_branch_id = strict_optional_text(
            row[1], field="database_meta.active_branch_id"
        )
        last_message_sequence = strict_non_negative_int(
            row[2], field="database_meta.last_message_sequence"
        )
        _projection_epoch = strict_non_negative_int(
            row[3], field="database_meta.projection_epoch"
        )
        _last_commit_id = strict_optional_non_negative_int(
            row[4], field="database_meta.last_commit_id"
        )
        database_state = strict_text(row[5], field="database_meta.database_state")
        if database_state != "active" and not (
            allow_migrating and database_state == "migrating"
        ):
            raise RuntimeError(f"rollout SQLite 状态不可读取: {database_state}")
        if active_branch_id is None:
            raise RuntimeError("database_meta.active_branch_id 缺失")
        namespace_state = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if namespace_state is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        namespace_branch_id = strict_text(
            namespace_state[0], field="checkpoint_namespace_state.active_branch_id"
        )
        namespace_projection_epoch = strict_non_negative_int(
            namespace_state[1], field="checkpoint_namespace_state.projection_epoch"
        )
        latest = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns,),
        ).fetchone()
        latest_checkpoint_id = (
            strict_text(latest[0], field="checkpoints.checkpoint_id")
            if latest is not None
            else None
        )
        format_row = connection.execute(
            "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if format_row is None:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version row"
            )
        rollout_format = strict_non_negative_int(
            format_row[0], field="database_meta.rollout_format_version"
        )
        for required_meta_column in ("history_view_revision", "source_overlay_epoch"):
            if required_meta_column not in meta_columns:
                raise RuntimeError(
                    f"v2 rollout database_meta 缺少必需字段: {required_meta_column}"
                )
        revision_row = connection.execute(
            "SELECT history_view_revision, source_overlay_epoch "
            "FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("v2 rollout database_meta revision row 缺失")
        history_view_revision = strict_non_negative_int(
            revision_row[0], field="database_meta.history_view_revision"
        )
        source_overlay_epoch = strict_non_negative_int(
            revision_row[1], field="database_meta.source_overlay_epoch"
        )
        from app.services.infrastructure.rollout_context.storage.primitives import (
            RolloutManifest,
        )

        return RolloutManifest(
            rollout_id,
            checkpoint_ns,
            namespace_branch_id,
            last_message_sequence,
            latest_checkpoint_id,
            namespace_projection_epoch,
            rollout_format,
            history_view_revision,
            source_overlay_epoch,
        )

    @staticmethod
    def _namespace_state(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
    ) -> tuple[str, int]:
        row = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        return (
            strict_text(row[0], field="checkpoint_namespace_state.active_branch_id"),
            strict_non_negative_int(
                row[1], field="checkpoint_namespace_state.projection_epoch"
            ),
        )

    def _ensure_namespace_state(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        existing = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if existing is not None:
            strict_text(
                existing[0], field="checkpoint_namespace_state.active_branch_id"
            )
            return
        if checkpoint_ns == _DEFAULT_NAMESPACE:
            meta = connection.execute(
                "SELECT active_branch_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None or meta[0] is None:
                raise RuntimeError(
                    "rollout 默认 checkpoint namespace 缺少 active branch"
                )
            branch_id = strict_text(meta[0], field="database_meta.active_branch_id")
        else:
            branch_id = "branch-" + uuid4().hex[:12]
            branch_result = connection.execute(
                "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                (branch_id, timestamp, timestamp),
            )
            if branch_result.rowcount != 1:
                raise RuntimeError(
                    f"rollout namespace branch 创建失败: {checkpoint_ns!r}"
                )
        namespace_result = connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (checkpoint_ns, branch_id, timestamp, timestamp),
        )
        if namespace_result.rowcount != 1:
            raise RuntimeError(f"rollout namespace state 创建失败: {checkpoint_ns!r}")

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
        schema_version = strict_non_negative_int(
            row[0], field="database_meta.schema_version"
        )
        message_format_version = strict_non_negative_int(
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
        if row is None or strict_non_negative_int(
            row[0], field="database_meta.rollout_format_version"
        ) != storage_version.ROLLOUT_FORMAT_VERSION:
            return
        database_offset = strict_non_negative_int(
            row[1], field="database_meta.committed_jsonl_offset"
        )
        database_last_commit_id = strict_optional_non_negative_int(
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
            commit_id = strict_non_negative_int(
                commit[0], field="storage_commits.commit_id"
            )
            start = strict_non_negative_int(
                commit[1], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end = strict_non_negative_int(
                commit[2], field=f"storage commit jsonl_end_offset: {commit_id}"
            )
            before = strict_non_negative_int(
                commit[3], field=f"storage commit jsonl_start_offset: {commit_id}"
            )
            end_offset = strict_non_negative_int(
                commit[4], field=f"storage commit jsonl_offset_after: {commit_id}"
            )
            record_count = strict_non_negative_int(
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
            after = strict_non_negative_int(
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
            catalog_count = strict_non_negative_int(
                catalog_row[0], field=f"item catalog count: {commit_id}"
            )
            if catalog_count != record_count:
                raise RuntimeError(
                    "storage commit 的 item catalog 数量不一致: "
                    f"commit_id={commit_id}, catalog={catalog_count}, "
                    f"record_count={record_count}"
                )
            previous = after
        if commits and database_last_commit_id != strict_non_negative_int(
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
            strict_non_negative_int(commit[0], field="storage commit id"): (
                strict_non_negative_int(commit[1], field="storage commit start offset"),
                strict_non_negative_int(commit[2], field="storage commit end offset"),
                strict_non_negative_int(commit[5], field="storage commit record count"),
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
            logical_length = strict_non_negative_int(
                logical_length, field=f"item_catalog.payload_length: {item_id}"
            )
            source_revision = strict_text(
                source_revision, field=f"item_catalog.source_revision: {item_id}"
            )
            sequence = strict_non_negative_int(
                item_sequence, field=f"item_catalog.item_sequence: {item_id}"
            )
            offset = strict_non_negative_int(
                item_offset, field=f"item_catalog.jsonl_offset: {item_id}"
            )
            length = strict_non_negative_int(
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
            item_commit_id_value = strict_non_negative_int(
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

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        """提交已经完成 JSONL durability barrier 的 SQLite 事务。"""
        connection.commit()
        mark_jsonl_commit_attempted(connection)
