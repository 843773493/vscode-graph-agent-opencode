"""rollout schema 自省与 commit-offset 校验；正文复用 storage 的有界校验回执。"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.domain.itemized.errors import (
    FormatDispatchError,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    read_local_scopes,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.integrity import (
    validate_catalog_body,
    validate_projection_membership,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    validate_block_order,
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
from app.services.infrastructure.rollout_context.storage.transaction import (
    STORED_COMMIT_COLUMNS,
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
    validate_stored_commit,
)

_DEFAULT_NAMESPACE = ""


__all__ = ["RolloutSchemaIntrospectionMixin"]

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class RolloutSchemaIntrospectionMixin:
    _host: RolloutStorage

    def _manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
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
        if database_state != "active":
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
        else:
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

        active_branch = connection.execute(
            "SELECT branch_id, head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?) AND status = 'active'",
            (checkpoint_ns,),
        ).fetchone()
        if active_branch is None:
            raise RuntimeError(
                "active branch 缺失，不能创建 rollout context view"
            )
        active_branch_id = strict_text(
            active_branch[0], field="branches.branch_id"
        )
        active_head_view_id = strict_optional_text(
            active_branch[1], field="branches.head_view_id"
        )
        if active_head_view_id is None:
            # acceptance 可以先于第一个 LangGraph checkpoint 到达；
            # 为这个空但真实存在的 active branch 建立 root view，
            # 使 Turn/root 在 acceptance-time 就有稳定的 view-local
            # 索引，而不是等下一次 checkpoint 偶然补齐。
            initial_view_id = self._host._create_view(
                connection,
                active_branch_id,
                None,
                (),
                timestamp,
                view_kind="root",
            )
            head_result = connection.execute(
                "UPDATE branches SET head_view_id = ?, updated_at = ? WHERE branch_id = ?",
                (initial_view_id, timestamp, active_branch_id),
            )
            if head_result.rowcount != 1:
                raise RuntimeError(
                    f"rollout active branch head view 更新失败: {active_branch_id}"
                )

    @staticmethod
    def _is_removed_rollout_layout(root: Path) -> bool:
        """判断目录是否仍是已经移除的旧 rollout 布局。"""
        return (root / "manifest.json").exists() or any(root.glob("segment-*.jsonl"))

    @staticmethod
    def _validate_schema_state(
        connection: sqlite3.Connection,
        *, expected_version: int | None = None,
    ) -> None:
        expected_version = storage_version.ROLLOUT_SCHEMA_VERSION if expected_version is None else expected_version
        for table in ("item_catalog", "context_contributions", "context_assembly_contributions"):
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "metadata_json" in columns:
                raise RuntimeError(
                    "schema-incompatible: 检测到旧实验 v2 metadata 字段形态；"
                    f"table={table}。原始 JSONL/SQLite 保留，请重建对应 rollout/。"
                )
        owner_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "thread_id" not in owner_columns:
            raise RuntimeError(
                "schema-incompatible: rollout SQLite 缺少真实 Thread owner；原始 JSONL/SQLite 保留"
            )
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
        if schema_version > expected_version:
            raise RuntimeError(
                "rollout SQLite schema 版本高于当前程序支持范围: "
                f"database={row[0]}, supported={expected_version}"
            )
        if schema_version < expected_version:
            raise RuntimeError(
                "schema-incompatible: rollout SQLite 不符合当前 typed item/source binding 合同；"
                "18/19/20/21/22/23/24/25版须通过显式 migrate_schema 顺序升级18→19→20→21→22→23→24→25→26；其余旧实验库不支持转换，原始数据保留；"
                f"current={schema_version}, target={expected_version}"
            )
        if message_format_version != storage_version.MESSAGE_FORMAT_VERSION:
            raise RuntimeError(
                "rollout JSONL message format 版本不受支持: "
                f"database={row[1]}, supported={storage_version.MESSAGE_FORMAT_VERSION}"
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='item_catalog_background_result_identity'"
        ).fetchone() is None:
            raise RuntimeError(
                "schema-incompatible: rollout 缺少必需的 canonical ToolResult identity 索引"
            )
        if schema_version >= 26:
            columns = {column[1] for column in connection.execute("PRAGMA table_info(context_views)")}
            required = {"context_views_creation_sequence", "context_views_assign_creation_sequence", "context_views_preserve_creation_sequence"}
            registered = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='context_views' AND type IN ('index','trigger')",
            )}
            if "creation_sequence" not in columns or not required <= registered:
                raise RuntimeError("schema-incompatible: rollout 缺少 view 创建顺序结构")
            if connection.execute("SELECT 1 FROM context_views WHERE creation_sequence<=0 LIMIT 1").fetchone() is not None:
                raise RuntimeError("source-mismatch: context view 缺少已提交创建顺序")
        validate_schema_journal(connection, schema_version)

    def _validate_v2_commit_offsets(
        self,
        connection: sqlite3.Connection,
        jsonl_path: Path,
        *,
        validate_jsonl_items: bool = True,
    ) -> None:
        """校验 committed offset 的单一权威和 storage commit 链。"""
        # 完整历史审计只用于恢复、迁移与显式完整校验；普通快照只检查当前提交边界。
        # 必须先审计成功再回收未提交尾部，损坏历史不得触发截断。
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in columns:
            return
        row = connection.execute(
            "SELECT rollout_format_version, committed_jsonl_offset, last_commit_id, schema_version, last_item_sequence "
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
            "SELECT " + STORED_COMMIT_COLUMNS
            + "FROM storage_commits commit_record ORDER BY commit_record.commit_id"
        )
        previous = 0
        last_seen_commit_id: int | None = None
        validated_metadata: set[str] = set()
        # item_sequence 是 INTEGER PRIMARY KEY；合法历史的序号与物理 offset 必须同序。
        # 沿主键扫描后逐条核 offset 连续性，避免完整审计按未索引的 offset 全量排序。
        item_rows = connection.execute(
            "SELECT item.item_sequence,item.item_id,item.content_hash,item.jsonl_offset,item.jsonl_length,"
            "item.commit_id,item.payload_length,item.source_revision,"
            "execution.session_id,execution.thread_id,turn.session_id,turn.thread_id "
            "FROM item_catalog item "
            "LEFT JOIN executions execution ON execution.execution_id=item.execution_id "
            "LEFT JOIN turn_records turn ON turn.turn_id=item.turn_id ORDER BY item.item_sequence"
        )
        expected_item_offset = 0
        storage_owner = current_scope(connection)
        # 完整审计核验全部冻结来源；逐 item 只检查归属成员，不随 owner 切换重算 binding hash。
        local_scopes = read_local_scopes(connection)
        expected_item_sequence = 1
        # 合法 locator 连续且 commit 边界不重叠，item 的 commit 归属必与提交顺序一致。
        # 两个主键有序流同步前进：在当前 commit 内核 catalog 数量及 locator，
        # 不再先 GROUP BY 全表，也不保存全部 commit 边界；每条正文仍按原字节复验。
        with (jsonl_path.open("rb") if validate_jsonl_items else nullcontext()) as stream:
            catalog_items = (
                (strict_non_negative_int(row[5], field=f"item_catalog.commit_id: {row[1]}"), row)
                for row in item_rows
            )
            pending_item = next(catalog_items, None)
            for commit in commits:
                commit_id, start, end, record_count = validate_stored_commit(
                    commit, validated_metadata=validated_metadata,
                )
                if start != previous:
                    raise RuntimeError(
                        "v2 storage commit JSONL offset 链断裂: "
                        f"commit_id={commit_id}, previous={previous}, start={start}, end={end}"
                    )
                commit_start, commit_end = start, end
                catalog_count = 0
                while pending_item is not None and pending_item[0] == commit_id:
                    item_commit_id_value, item_row = pending_item
                    catalog_count += 1
                    if catalog_count > record_count:
                        raise RuntimeError(
                            "storage commit 的 item catalog 数量不一致: "
                            f"commit_id={commit_id}, catalog>={catalog_count}, record_count={record_count}"
                        )
                    (
                        item_sequence,
                        item_id,
                        catalog_hash,
                        item_offset,
                        item_length,
                        _item_commit_id,
                        logical_length,
                        source_revision,
                        execution_session_id,
                        execution_thread_id,
                        turn_session_id,
                        turn_thread_id,
                    ) = item_row
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
                    execution_scope = (execution_session_id, execution_thread_id) if execution_session_id is not None else None
                    turn_scope = (turn_session_id, turn_thread_id) if turn_session_id is not None else None
                    if execution_scope is not None and turn_scope is not None and execution_scope != turn_scope:
                        raise ValueError(f"source-mismatch: canonical item 的 Turn/execution 历史 owner 不一致: {item_id}")
                    item_owner = execution_scope or turn_scope or storage_owner
                    if item_owner not in local_scopes:
                        require_historical_scope(connection, *item_owner, current_owner=storage_owner)
                    # 历史 prefix 可跳过未继承的原序号；当前 owner 的新事实仍逐项连续。
                    if sequence < expected_item_sequence or (sequence != expected_item_sequence and item_owner == storage_owner):
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
                    if stream is not None:
                        validate_catalog_body(
                            stream.read(length),
                            sequence=sequence,
                            item_id=item_id,
                            catalog_hash=catalog_hash,
                            payload_length=logical_length,
                            source_revision=source_revision,
                            cache=self._host._catalog_body_validation_cache,
                        )
                    if (
                        item_offset < commit_start
                        or end > commit_end
                    ):
                        raise RuntimeError(
                            "storage commit 的 item catalog offset 不在 commit 边界内: "
                            f"commit_id={item_commit_id_value}, item_id={item_id}, "
                            f"item_start={item_offset}, item_end={end}, "
                            f"start={commit_start}, end={commit_end}"
                        )
                    expected_item_offset = end
                    expected_item_sequence = sequence + 1
                    pending_item = next(catalog_items, None)
                if catalog_count != record_count:
                    raise RuntimeError(
                        "storage commit 的 item catalog 数量不一致: "
                        f"commit_id={commit_id}, catalog={catalog_count}, "
                        f"record_count={record_count}"
                    )
                previous = commit_end
                last_seen_commit_id = commit_id
            if pending_item is not None:
                raise RuntimeError(
                    f"v2 item catalog 指向不存在或顺序非法的 storage commit: item_id={pending_item[1][1]}"
                )
        if last_seen_commit_id is not None and database_last_commit_id != last_seen_commit_id:
            raise RuntimeError(
                "database_meta.last_commit_id 与 storage_commits 不一致: "
                f"meta={database_last_commit_id}, commits={last_seen_commit_id}"
            )
        if last_seen_commit_id is None and database_last_commit_id is not None:
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
        if expected_item_offset != database_offset:
            raise RuntimeError(
                "database_meta.committed_jsonl_offset 与 storage_commits 不一致: "
                f"meta={database_offset}, commits={previous}, "
                f"catalog_end={expected_item_offset}, file={jsonl_size}"
            )
        # 复用本次审计的末尾序号；恢复前必须核对 meta，不能再按损坏边界截尾。
        database_last_item_sequence = strict_non_negative_int(
            row[4], field="database_meta.last_item_sequence"
        )
        if database_last_item_sequence != expected_item_sequence - 1:
            raise RuntimeError(
                "database_meta.last_item_sequence 与 item_catalog 不一致: "
                f"meta={database_last_item_sequence}, catalog={expected_item_sequence - 1}"
            )
        validate_projection_membership(connection)
        # TODO：旧库显式升级链路移除后，取消 schema 23 的分界；旧表尚未使用成员块。
        if strict_non_negative_int(row[3], field="database_meta.schema_version") >= 23:
            validate_block_order(connection)

    def _commit_connection(self, connection: sqlite3.Connection) -> None:
        """提交已经完成 JSONL durability barrier 的 SQLite 事务。"""
        connection.commit()
        mark_jsonl_commit_attempted(connection)
