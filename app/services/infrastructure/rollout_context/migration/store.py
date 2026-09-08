"""显式 v1 import：在独立 staging 验收，再原子发布整个 rollout。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
    copy_source,
    empty_target_identity,
    install_directory,
    read_regular,
    require_safe_path,
    sync_directory,
    write_audit,
    write_private,
)
from app.services.infrastructure.rollout_context.migration.builder import (
    LegacyImportBuilder,
)
from app.services.infrastructure.rollout_context.migration.source import (
    read_source_report,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class _StagingStorage(RolloutStorage, LegacyImportBuilder):
    """构造时绑定 staging 根，不向 catalog 注册临时会话。"""

    def __init__(self, owner: RolloutStorage, target: str, root: Path) -> None:
        super().__init__(
            owner.sessions_dir, serde=owner._serde, message_codec=owner._message_codec
        )
        self._target = target
        self._root = root

    def root(self, thread_id: str, checkpoint_ns: str = "") -> Path:
        if thread_id != self._target:
            raise ValueError("migration staging 只能访问绑定的 target")
        return self._root


def _validate_staging(storage: _StagingStorage, target: str, namespace: str) -> None:
    """通过真实 v2 reader 验证提交链、正文及关系闭合。"""
    storage.initialize(target, namespace)
    by_id = {
        item.item_id: item
        for item in storage.read_items(target, checkpoint_ns=namespace)
    }
    calls: dict[str, CanonicalItemRecord] = {}
    results: set[str] = set()
    for item in by_id.values():
        if item.semantic_kind == "tool_call":
            call_id = item.payload["tool_call_id"]
            if call_id in calls:
                raise RuntimeError("migration staging tool call identity 重复")
            calls[call_id] = item
        elif item.semantic_kind == "tool_result":
            call_id = item.payload["tool_call_id"]
            call = calls.get(call_id)
            if call is None or call_id in results or call.turn_id != item.turn_id:
                raise RuntimeError("migration staging tool result lineage 不闭合")
            for field in ("tool_invocation_id", "tool_attempt_id"):
                if call.payload[field] != item.payload[field]:
                    raise RuntimeError(f"migration staging {field} 不一致")
            results.add(call_id)
    with storage._connect(target, namespace) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("migration staging SQLite integrity_check 失败")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("migration staging SQLite foreign_key_check 失败")
        for turn, root, final, status in connection.execute(
            "SELECT turn_id, root_input_item_id, final_item_id, status FROM turn_records"
        ):
            item = by_id.get(root)
            if (
                item is None
                or item.turn_id != turn
                or item.semantic_kind != "user_input"
            ):
                raise RuntimeError(f"migration staging Turn root 不闭合: {turn}")
            if status in {"active", "open"}:
                raise RuntimeError(f"migration staging 不得发布运行态 Turn: {turn}")
            if (status == "completed") != (final is not None):
                raise RuntimeError(f"migration staging final/status 不闭合: {turn}")
            if final is not None:
                item = by_id.get(final)
                if (
                    item is None
                    or item.turn_id != turn
                    or item.semantic_kind != "assistant_output"
                    or item.status != "completed"
                ):
                    raise RuntimeError(f"migration staging final item 不闭合: {turn}")
        for (item_id,) in connection.execute("SELECT item_id FROM context_view_items"):
            if item_id not in by_id:
                raise RuntimeError(f"migration staging view item 不存在: {item_id}")
        active = connection.execute(
            "SELECT b.head_view_id FROM branches b JOIN checkpoint_namespace_state n "
            "ON b.branch_id=n.active_branch_id WHERE n.checkpoint_ns=?",
            (namespace,),
        ).fetchone()
        if active is None or active[0] is None:
            raise RuntimeError("migration staging 缺少 target active view")
        visible = {
            row[0]
            for row in connection.execute(
                "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1",
                (active[0],),
            )
        }
        expected = {item.item_id for item in by_id.values() if item.turn_id is not None}
        if visible != expected:
            raise RuntimeError("migration staging active view membership 不闭合")
        for turn, root in connection.execute(
            "SELECT turn_id, root_input_item_id FROM context_view_turns WHERE view_id=?",
            (active[0],),
        ):
            if (
                root not in by_id
                or by_id[root].turn_id != turn
                or by_id[root].turn_scope != "turn_root"
            ):
                raise RuntimeError("migration staging view Turn root 不闭合")
        if connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0]:
            raise RuntimeError("migration 不得伪造无法恢复的 legacy assembly")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise RuntimeError("migration staging SQLite WAL checkpoint 未完成")


class LegacyMigrationStorage(RolloutStorage):
    """一次性命令入口；正常 runtime 不拥有 source reader 或导入 API。"""

    def _paths(
        self, source: str, target: str, namespace: str
    ) -> tuple[Path, Path, Path]:
        strict_text(source, field="migration.source_session_id")
        strict_text(target, field="migration.target_session_id")
        strict_text(namespace, field="migration.checkpoint_ns", allow_empty=True)
        if source == target:
            raise ValueError("legacy migration source/target 不得是同一个 session")
        source_root = self.root(source, namespace)
        target_root = self.root(target, namespace)
        require_safe_path(source_root)
        require_safe_path(target_root)
        if (
            source_root == target_root
            or source_root in target_root.parents
            or target_root in source_root.parents
        ):
            raise ValueError("migration source/target 物理路径重叠")
        audit_root = target_root.parent / "legacy-import"
        require_safe_path(audit_root)
        for root in (source_root, target_root):
            require_safe_path(root.parent / ".rollout.write.lock")
        return source_root, target_root, audit_root

    def legacy_migration_report(
        self, thread_id: str, *, checkpoint_ns: str = ""
    ) -> dict[str, object]:
        strict_text(thread_id, field="migration.source_session_id")
        strict_text(checkpoint_ns, field="migration.checkpoint_ns", allow_empty=True)
        source = self.root(thread_id, checkpoint_ns)
        require_safe_path(source.parent / ".rollout.write.lock")
        with self._lock(thread_id, checkpoint_ns):
            return read_source_report(source, thread_id)

    def migrate_legacy_to_v2(
        self,
        source_thread_id: str,
        *,
        target_thread_id: str,
        checkpoint_ns: str = "",
        require_lossless: bool = False,
    ) -> dict[str, object]:
        if type(require_lossless) is not bool:
            raise TypeError("migration require_lossless 必须是 boolean")
        source, target, audit_root = self._paths(
            source_thread_id, target_thread_id, checkpoint_ns
        )
        first, second = sorted((source_thread_id, target_thread_id))
        with self._lock(first, checkpoint_ns), self._lock(second, checkpoint_ns):
            self._recover_import_audits(target_thread_id, checkpoint_ns, audit_root)
            original_target = empty_target_identity(target)
            audit_root.mkdir(mode=0o700, exist_ok=True)
            migration_id = uuid4().hex
            audit = audit_root / migration_id
            audit.mkdir(mode=0o700)
            sync_directory(audit_root)
            staging = audit / "staging"
            record: dict[str, object] = {
                "migration_id": migration_id,
                "source_session_id": source_thread_id,
                "target_session_id": target_thread_id,
                "source_format_version": 1,
                "target_format_version": 2,
                "status": "running",
                "checkpoint_ns": checkpoint_ns,
                "original_target": list(original_target) if original_target else None,
            }
            write_audit(audit, record)
            try:
                source_identity = copy_source(source, audit / "source")
                record["source_files"] = source_identity
                write_audit(audit, record)
                report = read_source_report(audit / "source", source_thread_id)
                report["raw_artifact_ref"] = f"legacy-import/{migration_id}/source"
                report["protection"] = "protected"
                storage = _StagingStorage(self, target_thread_id, staging)
                result = storage.build_import(
                    source_thread_id,
                    target_thread_id=target_thread_id,
                    report=report,
                    migration_id=migration_id,
                    checkpoint_ns=checkpoint_ns,
                )
                record["result"] = result
                if require_lossless and not result["lossless"]:
                    raise RuntimeError(
                        "v1_full_copy_not_lossless: 详见 migration report 的 loss/rejected"
                    )
                _validate_staging(storage, target_thread_id, checkpoint_ns)
                write_private(
                    staging / "legacy-import.json",
                    json.dumps(
                        {
                            "migration_id": migration_id,
                            "target_session_id": target_thread_id,
                        }
                    ).encode(),
                )
                record.update(
                    {
                        "status": "ready",
                        "result": result,
                        "staging_files": artifact_manifest(staging),
                    }
                )
                write_audit(audit, record)
                if artifact_manifest(source) != source_identity:
                    raise RuntimeError("source-mismatch: migration 安装前原件变化")
                if empty_target_identity(target) != original_target:
                    raise RuntimeError("migration target 安装前路径变化")
                install_directory(staging, target)
                record["status"] = "installed"
                write_audit(audit, record)
                return result
            except BaseException as error:
                # rename 后的失败也绝不删除已发布的 v2 history。
                installed = self._installed_migration_id(target) == migration_id
                record.update(
                    {
                        "status": "installed_audit_failed" if installed else "failed",
                        "rollback": "installed_v2_preserved"
                        if installed
                        else "uninstalled_staging_quarantined",
                        "error": str(error),
                    }
                )
                write_audit(audit, record)
                raise

    @staticmethod
    def _installed_migration_id(target: Path) -> str | None:
        marker = target / "legacy-import.json"
        require_safe_path(marker)
        if not marker.exists():
            return None
        value = json.loads(read_regular(marker))
        if not isinstance(value, Mapping):
            raise TypeError("migration install marker 非法")
        return strict_text(value.get("migration_id"), field="migration.install_id")

    def recover_legacy_imports(
        self, target_thread_id: str, *, checkpoint_ns: str = ""
    ) -> list[dict[str, object]]:
        target = self.root(target_thread_id, checkpoint_ns)
        require_safe_path(target)
        require_safe_path(target.parent / ".rollout.write.lock")
        with self._lock(target_thread_id, checkpoint_ns):
            return self._recover_import_audits(
                target_thread_id, checkpoint_ns, target.parent / "legacy-import"
            )

    def _recover_import_audits(
        self, target_id: str, namespace: str, audit_root: Path
    ) -> list[dict[str, object]]:
        require_safe_path(audit_root)
        if not audit_root.exists():
            return []
        target = self.root(target_id, namespace)
        reports: list[dict[str, object]] = []
        for audit in sorted(audit_root.iterdir()):
            require_safe_path(audit)
            record = json.loads(read_regular(audit / "report.json"))
            if (
                not isinstance(record, dict)
                or record.get("target_session_id") != target_id
                or record.get("migration_id") != audit.name
            ):
                raise RuntimeError(f"migration recovery audit identity 不一致: {audit}")
            if record.get("status") not in {
                "running",
                "ready",
                "installed_audit_failed",
            }:
                continue
            installed = self._installed_migration_id(target) == audit.name
            if installed:
                # 不调用 initialize、不强转版本、不清空任何正式 artifact。
                artifact_manifest(target)
                with closing(
                    sqlite3.connect(
                        (target / "index.sqlite").as_uri() + "?mode=ro&immutable=1",
                        uri=True,
                    )
                ) as connection:
                    row = connection.execute(
                        "SELECT rollout_format_version, database_state FROM database_meta WHERE singleton_id=1"
                    ).fetchone()
                    installed_report = connection.execute(
                        "SELECT source_session_id, target_session_id, report_json FROM legacy_migration_reports WHERE migration_id=?",
                        (audit.name,),
                    ).fetchone()
                    if (
                        installed_report is None
                        or installed_report[:2]
                        != (record["source_session_id"], target_id)
                        or json.loads(installed_report[2]) != record.get("result")
                    ):
                        raise RuntimeError(
                            "migration recovery install marker 与已提交 report 不一致"
                        )
                    self._validate_v2_commit_offsets(
                        connection, target / "rollout.jsonl"
                    )
                if row != (2, "active"):
                    raise RuntimeError("migration recovery 已安装 artifact 不完整")
            record.update(
                {
                    "status": "installed" if installed else "failed",
                    "rollback": "installed_v2_preserved"
                    if installed
                    else "uninstalled_staging_quarantined",
                    "error": "migration process exited before audit completion",
                }
            )
            write_audit(audit, record)
            reports.append(record)
        return reports
