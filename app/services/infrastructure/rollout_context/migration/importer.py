"""显式一次性 v1 导入 owner；组合现行 storage，验收后原子安装。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.core.sqlite_state import utc_now_text
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
from app.services.infrastructure.rollout_context.migration.quarantine import (
    write_quarantine,
)
from app.services.infrastructure.rollout_context.migration.source import (
    read_source_report,
)
from app.services.infrastructure.rollout_context.migration.staging import (
    ImportStagingStorage,
    validate_staging,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class LegacyRolloutImporter:
    """CLI 显式创建的导入流程；不向正常 runtime 暴露 v1 storage API。"""

    def __init__(self, storage: RolloutStorage) -> None:
        self.storage = storage

    def _main_owner(self, session_id: str, thread_id: str) -> None:
        strict_text(session_id, field="migration.session_id")
        strict_text(thread_id, field="migration.thread_id")
        if self.storage._resolve_thread_identity(
            session_id, thread_id
        ) != self.storage.main_thread_id(session_id):
            raise ValueError("一次性 v1 import 必须显式绑定 catalog main thread")

    def read_report(
        self, *, source_session_id: str, source_thread_id: str, checkpoint_ns: str
    ) -> dict[str, object]:
        self._main_owner(source_session_id, source_thread_id)
        strict_text(checkpoint_ns, field="migration.checkpoint_ns", allow_empty=True)
        source = self.storage.root(
            source_session_id, checkpoint_ns, thread_id=source_thread_id
        )
        require_safe_path(source.parent / ".rollout.write.lock")
        with self.storage._lock(
            source_thread_id, checkpoint_ns, session_id=source_session_id
        ):
            return {
                **read_source_report(source, source_session_id),
                "source_thread_id": source_thread_id,
                "checkpoint_ns": checkpoint_ns,
            }

    def import_v1(
        self,
        *,
        source_session_id: str,
        source_thread_id: str,
        target_session_id: str,
        target_thread_id: str,
        checkpoint_ns: str,
        require_lossless: bool = False,
    ) -> dict[str, object]:
        if type(require_lossless) is not bool:
            raise TypeError("migration require_lossless 必须是 boolean")
        if source_session_id == target_session_id:
            raise ValueError("legacy migration source/target 不得是同一个 session")
        self._main_owner(source_session_id, source_thread_id)
        self._main_owner(target_session_id, target_thread_id)
        strict_text(checkpoint_ns, field="migration.checkpoint_ns", allow_empty=True)
        source = self.storage.root(
            source_session_id, checkpoint_ns, thread_id=source_thread_id
        )
        target = self.storage.root(
            target_session_id, checkpoint_ns, thread_id=target_thread_id
        )
        audit_root = target.parent / "legacy-import"
        for root in (source, target, audit_root):
            require_safe_path(root)
        if source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError("migration source/target 物理路径重叠")
        for root in (source, target):
            require_safe_path(root.parent / ".rollout.write.lock")
        first, second = sorted(
            (
                (source_session_id, source_thread_id),
                (target_session_id, target_thread_id),
            )
        )
        with (
            self.storage._lock(first[1], checkpoint_ns, session_id=first[0]),
            self.storage._lock(second[1], checkpoint_ns, session_id=second[0]),
        ):
            self._recover(
                target_session_id, target_thread_id, checkpoint_ns, audit_root
            )
            original_target = empty_target_identity(target)
            audit_root.mkdir(mode=0o700, exist_ok=True)
            migration_id = uuid4().hex
            audit = audit_root / migration_id
            audit.mkdir(mode=0o700)
            sync_directory(audit_root)
            staging = audit / "staging"
            record: dict[str, object] = {
                "migration_id": migration_id,
                "source_session_id": source_session_id,
                "source_thread_id": source_thread_id,
                "target_session_id": target_session_id,
                "target_thread_id": target_thread_id,
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
                report = read_source_report(
                    audit / "source", source_session_id, include_raw_candidates=True
                )
                report.update(
                    {
                        "raw_artifact_ref": f"legacy-import/{migration_id}/source",
                        "protection": "protected",
                        "checkpoint_ns": checkpoint_ns,
                    }
                )
                staged = ImportStagingStorage(
                    self.storage,
                    session_id=target_session_id,
                    thread_id=target_thread_id,
                    checkpoint_ns=checkpoint_ns,
                    root=staging,
                )
                result = LegacyImportBuilder(staged).build_import(
                    source_session_id,
                    source_thread_id=source_thread_id,
                    target_session_id=target_session_id,
                    target_thread_id=target_thread_id,
                    report=report,
                    migration_id=migration_id,
                    checkpoint_ns=checkpoint_ns,
                )
                if result["rejected"]:
                    result["quarantine"] = write_quarantine(
                        audit,
                        raw_artifact_ref=report["raw_artifact_ref"],
                        candidates=result["rejected"],
                    )
                record["result"] = result
                if require_lossless and not result["lossless"]:
                    raise RuntimeError(
                        "v1_full_copy_not_lossless: 详见 migration report 的 loss/rejected"
                    )
                with staged._connect(
                    target_thread_id, checkpoint_ns, session_id=target_session_id
                ) as connection:
                    now = utc_now_text()
                    connection.execute(
                        "INSERT INTO legacy_migration_reports(migration_id, source_session_id, "
                        "target_session_id, source_format_version, target_format_version, "
                        "status, report_json, created_at, completed_at) "
                        "VALUES (?, ?, ?, 1, 2, ?, ?, ?, ?)",
                        (
                            migration_id,
                            source_session_id,
                            target_session_id,
                            result["status"],
                            canonical_json_text(result),
                            now,
                            now,
                        ),
                    )
                    connection.commit()
                validate_staging(
                    staged, target_session_id, target_thread_id, checkpoint_ns
                )
                write_private(
                    staging / "legacy-import.json",
                    json.dumps(
                        {
                            "migration_id": migration_id,
                            "target_session_id": target_session_id,
                            "target_thread_id": target_thread_id,
                            "checkpoint_ns": checkpoint_ns,
                        }
                    ).encode(),
                )
                record.update(
                    {"status": "ready", "staging_files": artifact_manifest(staging)}
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
                # rename 之后的失败必须保留已发布的 v2 history 和失败审计。
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

    def recover(
        self, *, target_session_id: str, target_thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, object]]:
        self._main_owner(target_session_id, target_thread_id)
        strict_text(checkpoint_ns, field="migration.checkpoint_ns", allow_empty=True)
        target = self.storage.root(
            target_session_id, checkpoint_ns, thread_id=target_thread_id
        )
        require_safe_path(target)
        require_safe_path(target.parent / ".rollout.write.lock")
        with self.storage._lock(
            target_thread_id, checkpoint_ns, session_id=target_session_id
        ):
            return self._recover(
                target_session_id,
                target_thread_id,
                checkpoint_ns,
                target.parent / "legacy-import",
            )

    def _recover(
        self, session_id: str, thread_id: str, namespace: str, audit_root: Path
    ) -> list[dict[str, object]]:
        require_safe_path(audit_root)
        if not audit_root.exists():
            return []
        target = self.storage.root(session_id, namespace, thread_id=thread_id)
        reports: list[dict[str, object]] = []
        for audit in sorted(audit_root.iterdir()):
            require_safe_path(audit)
            report = audit / "report.json"
            pending_report: Path | None = None
            require_safe_path(report)
            if report.exists():
                record = json.loads(read_regular(report))
            else:
                # 首份 report 的 replace 是审计发布点；只恢复发布前的已知残留。
                if not re.fullmatch(r"[0-9a-f]{32}", audit.name) or (
                    self._installed_migration_id(target) == audit.name
                ):
                    raise RuntimeError(
                        f"migration recovery 缺少有效 audit report: {audit}"
                    )
                pending = list(audit.iterdir())
                if not pending:
                    audit.rmdir()
                    sync_directory(audit_root)
                    continue
                if len(pending) != 1 or not re.fullmatch(
                    r"report-[0-9a-f]{32}\.next\.json", pending[0].name
                ):
                    raise RuntimeError(
                        f"migration recovery 缺少初始 audit report: {audit}"
                    )
                pending_report = pending[0]
                pending_bytes = read_regular(pending_report)
                try:
                    record = json.loads(pending_bytes)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # 首份临时报告尚未发布、也未开始复制；只清理本次写入中断的数据。
                    pending_report.unlink()
                    sync_directory(audit)
                    audit.rmdir()
                    sync_directory(audit_root)
                    continue
                if (
                    not isinstance(record, dict)
                    or set(record)
                    != {
                        "migration_id",
                        "source_session_id",
                        "source_thread_id",
                        "target_session_id",
                        "target_thread_id",
                        "source_format_version",
                        "target_format_version",
                        "status",
                        "checkpoint_ns",
                        "original_target",
                    }
                    or (
                        record.get("status"),
                        record.get("source_format_version"),
                        record.get("target_format_version"),
                    )
                    != ("running", 1, 2)
                ):
                    raise RuntimeError(
                        f"migration recovery 初始 audit report 非法: {audit}"
                    )
                strict_text(
                    record.get("source_session_id"), field="migration.source_session_id"
                )
                strict_text(
                    record.get("source_thread_id"), field="migration.source_thread_id"
                )
            if not isinstance(record, dict) or (
                record.get("target_session_id"),
                record.get("target_thread_id"),
                record.get("checkpoint_ns"),
                record.get("migration_id"),
            ) != (session_id, thread_id, namespace, audit.name):
                raise RuntimeError(f"migration recovery audit identity 不一致: {audit}")
            if pending_report is not None:
                os.replace(pending_report, report)
                sync_directory(audit)
            if record.get("status") not in {
                "running",
                "ready",
                "installed_audit_failed",
            }:
                continue
            installed = self._installed_migration_id(target) == audit.name
            if installed:
                # 不初始化、不修改正式 artifact；只验收已安装的不可变提交。
                artifact_manifest(target)
                with closing(
                    sqlite3.connect(
                        (target / "index.sqlite").as_uri() + "?mode=ro&immutable=1",
                        uri=True,
                    )
                ) as connection:
                    owner = connection.execute(
                        "SELECT rollout_format_version, database_state, session_id, thread_id FROM database_meta WHERE singleton_id=1"
                    ).fetchone()
                    imported = connection.execute(
                        "SELECT source_session_id, target_session_id, report_json FROM legacy_migration_reports WHERE migration_id=?",
                        (audit.name,),
                    ).fetchone()
                    if (
                        imported is None
                        or imported[:2] != (record["source_session_id"], session_id)
                        or json.loads(imported[2]) != record.get("result")
                    ):
                        raise RuntimeError(
                            "migration recovery install marker 与已提交 report 不一致"
                        )
                    self.storage._validate_v2_commit_offsets(
                        connection, target / "rollout.jsonl"
                    )
                if owner != (2, "active", session_id, thread_id):
                    raise RuntimeError(
                        "migration recovery 已安装 artifact owner 不完整"
                    )
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
