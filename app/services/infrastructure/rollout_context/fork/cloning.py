"""v2 fork completion/clone owner。"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.fork.assembly_copy import (
    validate_source_assemblies,
)
from app.services.infrastructure.rollout_context.fork.full_copy.sqlite_snapshot import (
    SQLITE_EXCLUDED_FILES,
    copy_live_sqlite_snapshot,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    detail_copy_row,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _source_snapshot(source: Path, target_session: Path):
    """在 source 共享锁内检查私有 SQLite/WAL 副本，不修改原件的 SHM。"""
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        for name in (*directory_names, *file_names):
            candidate = Path(current) / name
            if candidate.is_symlink():
                raise RuntimeError(
                    f"full_rollout_copy source 路径不能包含符号链接: {candidate}"
                )
            if name in file_names and not candidate.is_file():
                raise RuntimeError(
                    f"full_rollout_copy source 不是普通文件: {candidate}"
                )
    # 只复制 SQLite 权威字节及其 WAL；SHM 不是事实源，在私有副本中重建。
    # 不能 immutable=1 忽略非空 WAL，也不能对 source 执行 checkpoint/recovery。
    with TemporaryDirectory(prefix=".fork-preflight-", dir=target_session) as temporary:
        index = Path(temporary) / "index.sqlite"
        copy_live_sqlite_snapshot(source, Path(temporary))
        with closing(
            sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            yield connection


class ForkCloneMixin:
    """完整 v2 rollout 物理副本 owner。"""

    def _require_private_detail_staging(self) -> None:
        raise DetailUnavailableError(
            "detail-forbidden: protected clone 只能通过原子 full-copy staging"
        )

    def clone_rollout(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        checkpoint_ns: str = "",
        source_checkpoint_id: str | None,
        detail_capability=None,
    ) -> str | None:
        from app.services.infrastructure.rollout_context.storage import (
            schema as storage_version,
        )
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        source_thread_id = required_text(
            source_thread_id, field="clone.source_thread_id"
        )
        target_thread_id = required_text(
            target_thread_id, field="clone.target_thread_id"
        )
        if source_thread_id == target_thread_id:
            raise ValueError("clone source 与 target session 不能相同")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("clone.checkpoint_ns 必须是字符串")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="clone.source_checkpoint_id"
        )
        source = self.root(source_thread_id, checkpoint_ns)
        target = self.root(target_thread_id, checkpoint_ns)
        self._safe_session_relative_path(source.parent, source.name)
        self._safe_session_relative_path(target.parent, target.name)
        if not source.is_dir():
            raise KeyError(source_thread_id)
        source_lock = _RolloutFileLock(
            source.parent / ".rollout.write.lock",
            exclusive=False,
        )
        target_lock = self._lock(target_thread_id, checkpoint_ns)
        source_lock.acquire()
        source_view_id: str | None = None
        source_detail_rows: tuple[tuple[object, ...], ...] = ()
        try:
            with _source_snapshot(source, target.parent) as source_connection:
                source_format_version = self._rollout_format(source_connection)
                if source_format_version == 1:
                    raise FormatDispatchError(
                        "v1_migration_required: full_rollout_copy 不得隐式迁移 v1；"
                        "请先显式运行 legacy_import_v1_to_v2"
                    )
                if source_format_version != storage_version.ROLLOUT_FORMAT_VERSION:
                    raise ValueError(
                        "v1_migration_required: normal full_rollout_copy 不读取 v1 rollout"
                    )
                schema_version = source_connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton_id=1"
                ).fetchone()
                if schema_version != (storage_version.ROLLOUT_SCHEMA_VERSION,):
                    raise RuntimeError(
                        "schema-upgrade-required: full-copy source 必须先显式升级 v2 schema"
                    )
                self._validate_schema_state(source_connection)
                self._validate_v2_commit_offsets(
                    source_connection, source / "rollout.jsonl"
                )
                state = source_connection.execute(
                    "SELECT database_state FROM database_meta WHERE singleton_id=1"
                ).fetchone()
                if state != ("active",):
                    raise RuntimeError(
                        "recovery-required: full-copy source 不是已发布 active rollout"
                    )
                validate_source_assemblies(self, source_connection, checkpoint_ns)
                source_view = (
                    source_connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_checkpoint_id
                    else source_connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                )
                if source_checkpoint_id is not None and source_view is None:
                    raise KeyError(source_checkpoint_id)
                source_view_id = (
                    required_text(source_view[0], field="clone.source_view_id")
                    if source_view is not None and source_view[0] is not None
                    else None
                )
                source_detail_rows = tuple(
                    detail_copy_row(row)
                    for row in source_connection.execute(
                        "SELECT detail_ref, assembly_id, relative_path, content_hash, required, status, detail_id FROM context_plan_details WHERE session_id = ? AND checkpoint_ns = ? ORDER BY detail_ref",
                        (source_thread_id, checkpoint_ns),
                    ).fetchall()
                )
                if source_connection.execute(
                    "SELECT 1 FROM context_plan_details WHERE protection='protected' LIMIT 1"
                ).fetchone():
                    if detail_capability is None:
                        raise DetailUnavailableError(
                            "detail-forbidden: protected fork 未注入 capability"
                        )
                    detail_capability.require_protected_key()
                    self._require_private_detail_staging()
                for (
                    detail_ref,
                    assembly_id,
                    relative_path,
                    expected_hash,
                    required,
                    status,
                ) in source_detail_rows:
                    detail_path = Path(relative_path)
                    detail_ref.require_owner(source_thread_id, assembly_id)
                    if relative_path != detail_relative_path(detail_ref).as_posix():
                        raise RuntimeError(
                            f"source context detail locator 非法: {detail_ref}"
                        )
                    candidate = self._safe_session_relative_path(
                        source.parent,
                        detail_path,
                    )
                    available = (
                        status == "available"
                        and candidate.is_file()
                        and not candidate.is_symlink()
                    )
                    if available:
                        try:
                            _, record = parse_detail_payload(
                                candidate.read_bytes(), detail_ref=detail_ref
                            )
                            available = record.content_hash == expected_hash
                        except (OSError, ValueError, DetailUnavailableError):
                            available = False
                    if required and not available:
                        raise RuntimeError(
                            "required context detail 无法随 full_rollout_copy 复制: "
                            f"detail_ref={detail_ref}, status={status}"
                        )
            # copytree 默认会跟随目录符号链接；rollout 是 session 数据的
            # canonical 物理副本，任何一个父级或文件 symlink 都会破坏
            # target-local/no-follow 合同，因此在创建目标前逐项拒绝。
            if source.is_symlink():
                raise RuntimeError(
                    f"source rollout session node 不能是符号链接: {source}"
                )
            with target_lock:
                self._safe_session_relative_path(target.parent, target.name)
                if target.exists():
                    raise FileExistsError(target)
                ignored_details = shutil.ignore_patterns(
                    ".rollout.write.lock",
                    ".context-redaction-key",
                    "context-plan-details-protected",
                    *(
                        ("context-plan-details",)
                        if detail_capability is not None
                        else ()
                    ),
                )

                def ignore_live_sqlite(directory, names):
                    ignored = ignored_details(directory, names)
                    if Path(directory) == source:
                        ignored.update(SQLITE_EXCLUDED_FILES.intersection(names))
                    return ignored

                shutil.copytree(
                    source,
                    target,
                    ignore=ignore_live_sqlite,
                )
                copy_live_sqlite_snapshot(source, target)
                with self._connect(target_thread_id, checkpoint_ns) as connection:
                    target_rollout_id = self._rollout_id(target_thread_id)
                    connection.execute(
                        "UPDATE database_meta SET rollout_id = ?, session_id = ?, updated_at = ? WHERE singleton_id = 1",
                        (target_rollout_id, target_thread_id, _now()),
                    )
                    # fork_origins 和 retention_refs 属于原 rollout 的会话关系，
                    # 其中的 child/source/owner ID 不能随完整副本带入新会话。
                    # 新 fork 的唯一来源记录由统一 materialization writer 写入。
                    connection.execute("DELETE FROM fork_origins")
                    connection.execute("DELETE FROM retention_refs")
                    connection.execute("DELETE FROM fork_identity_mappings")
                    # fork journal 只描述这次目标物化，不能把父会话过去的
                    # prepared/committed 记录复制成子会话的新操作历史。
                    connection.execute("DELETE FROM fork_materializations")
                    # 这些表的物理文件随副本落在 target session 节点中，但
                    # session owner 字段仍是 source；若不改写，Saver 的
                    # target-local assembly/detail/overlay 查询会看不到已复制
                    # 的记录。正文、item/view/checkpoint local id 保持不变，
                    # 由 fork identity mapping 记录 source lineage。
                    connection.execute(
                        "UPDATE turn_acceptances SET session_id = ?",
                        (target_thread_id,),
                    )
                    connection.execute(
                        "UPDATE context_assemblies SET session_id = ?",
                        (target_thread_id,),
                    )
                    # 保留原始 snapshot 字节，后续 remap 同时处理所有 typed owner，
                    # 并将精确 source fingerprint 写入显式 fork import provenance。
                    # detail 的 owner、typed key 和 leaf 由 catalog remap 同步改写；
                    # 单独修改 session_id 会破坏 schema3 的 typed identity CHECK。
                    connection.execute(
                        "UPDATE source_overlays SET session_id = ?",
                        (target_thread_id,),
                    )
                    # schema4 工具行与 plan header 有复合 FK；不能在克隆阶段单改
                    # session owner。remap 事务按子表→父表移除私有副本，再完整重写。
                    unavailable_detail_refs: list[str] = []
                    for (
                        detail_ref,
                        _assembly_id,
                        relative_path,
                        expected_hash,
                        required,
                        status,
                    ) in source_detail_rows:
                        if detail_capability is not None:
                            # staging 按 typed registry 重新物化正文，不复制未登记的 orphan。
                            continue
                        detail_path = self._safe_session_relative_path(
                            target.parent,
                            Path(relative_path),
                        )
                        available = (
                            status == "available"
                            and detail_path.is_file()
                            and not detail_path.is_symlink()
                        )
                        if available:
                            try:
                                _, record = parse_detail_payload(
                                    detail_path.read_bytes(), detail_ref=detail_ref
                                )
                                available = record.content_hash == expected_hash
                            except (OSError, ValueError, DetailUnavailableError):
                                available = False
                        if not available and required:
                            raise RuntimeError(
                                "target context detail 复制后不可用: "
                                f"detail_ref={detail_ref}"
                            )
                        if not available:
                            unavailable_detail_refs.append(detail_ref_key(detail_ref))
                    if unavailable_detail_refs:
                        placeholders = ",".join("?" for _ in unavailable_detail_refs)
                        connection.execute(
                            f"UPDATE context_plan_details SET status = 'unavailable' WHERE detail_ref IN ({placeholders})",
                            tuple(unavailable_detail_refs),
                        )
                    connection.execute(
                        "UPDATE legacy_migration_reports SET target_session_id = ? WHERE target_session_id IS NOT NULL",
                        (target_thread_id,),
                    )
        finally:
            source_lock.release()
        return source_view_id
