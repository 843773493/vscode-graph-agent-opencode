"""物理树迁移 + session-control 初始化 + 终验 + 通用工具。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.core.atomic_fs import atomic_write_bytes as _atomic_write_bytes
from app.core.atomic_fs import fsync_directory as _fsync_directory
from app.core.session_catalog_legacy_layout import (
    FOLDER_MANIFEST_NAME,
    SESSION_MANIFEST_NAME,
)
from app.core.session_catalog_store import SessionCatalogNode, SessionCatalogStore
from app.core.session_control_store import SessionControlStore

from ._constants import (
    _CONTENT_MANIFEST_EXCLUDED_NAMES,
    _RECONCILE_PAGE_LIMIT,
    _STRIP_MANIFEST_KEYS,
)
from ._contracts import _FrozenNode, _MigrationContext


class SessionCatalogMigratorPhysicalMixin:
    """物理树迁移 + session-control 初始化 + 终验 + 通用工具。"""

    # ------------------------------------------------------------------
    # 物理树迁移 + session-control 初始化(任务书 §2.2-B/C)
    # ------------------------------------------------------------------

    def _physical_started(self, physical: dict[str, object]) -> bool:
        """物理迁移是否已开始(任一 session/folder 离开 pending)。"""
        sessions, folders = self._typed_physical(physical)
        return any(
            record["state"] != "pending" for record in sessions.values()
        ) or any(record["state"] != "pending" for record in folders.values())

    def _run_physical_stage(self, context: _MigrationContext) -> None:
        """执行(或继续)物理树迁移:staging→日期桶 / quarantine 隔离 /
        folder 删除 / session-control 初始化,逐动作更新 journal。

        处理顺序:session 按旧位置深度**降序**(子先于父,保证嵌套子会话
        先搬出、父目录残余即最终内容);folder 同样深先删除;
        quarantine 与 migrate 在同一深序内处理(嵌套在合法会话内的
        quarantine 子会话先于父会话 staging 被隔离出去)。
        """
        sessions, folders = self._typed_physical(context.physical)
        frozen_by_id = {item.node_id: item for item in context.frozen}
        self._check_staging_residues(context, sessions)
        session_order = sorted(
            sessions,
            key=lambda node_id: (
                -len(PurePosixPath(str(sessions[node_id]["old_relative_path"])).parts),
                node_id,
            ),
        )
        for session_id in session_order:
            record = sessions[session_id]
            classification = record["classification"]
            if classification == "quarantine":
                self._process_quarantine_node(
                    session_id, record, context, kind="session"
                )
                continue
            frozen = frozen_by_id[session_id]
            if frozen.created_at is None or frozen.storage_relative_locator is None:
                raise self._fail(
                    "物理迁移",
                    f"冻结映射缺少 session 字段: session_id={session_id}",
                )
            if record["state"] == "pending":
                self._stage_session(session_id, record, context)
            if record["state"] == "staged":
                self._place_session(session_id, record, context, frozen)
            if record["state"] == "placed":
                # placed 复验(staged→placed 落地后与本重入路径共用)。
                target = self._date_bucket_dir(frozen)
                self._verify_placed_session(session_id, record, target, stage="物理迁移")
            if record["control_state"] == "pending":
                self._initialize_session_control(
                    session_id, record, context, frozen
                )
            else:
                self._verify_session_control_rows(
                    self._date_bucket_dir(frozen), frozen, stage="物理迁移"
                )
        folder_order = sorted(
            folders,
            key=lambda node_id: (
                -len(PurePosixPath(str(folders[node_id]["old_relative_path"])).parts),
                node_id,
            ),
        )
        for folder_id in folder_order:
            record = folders[folder_id]
            if record["classification"] == "quarantine":
                # B.4:quarantine 节点(含 folder)物理目录隔离到 orphaned 保留审计。
                self._process_quarantine_node(
                    folder_id, record, context, kind="folder"
                )
                continue
            if record["state"] == "pending":
                self._delete_folder(folder_id, record, context)
            else:
                old_path = self._old_path_for(record, stage="物理迁移")
                if old_path.exists() or old_path.is_symlink():
                    raise self._fail(
                        "物理迁移",
                        "folder 目录在 journal 记 deleted 后重现(外部改动,拒绝继续): "
                        f"folder_id={folder_id}, path={old_path}",
                    )
        self._sweep_staging(context)

    def _check_staging_residues(
        self,
        context: _MigrationContext,
        sessions: dict[str, dict[str, object]],
    ) -> None:
        """staging 残留检查:迁移 staging 目录下的条目必须全部对应 staged 记录。"""
        staging_dir = self._resolved_staging_root / context.migration_id
        if not staging_dir.is_dir():
            return
        for entry in sorted(staging_dir.iterdir()):
            record = sessions.get(entry.name)
            if record is None or record["state"] != "staged":
                raise self._fail(
                    "物理迁移",
                    "staging 残留目录(journal 无对应 staged 记录,拒绝吸收): "
                    f"path={entry}, migration_id={context.migration_id}",
                )

    def _old_path_for(
        self, record: dict[str, object], *, stage: str
    ) -> Path:
        """由 journal 记录还原旧位置绝对路径(已在解析期校验形态)。"""
        relative = record.get("old_relative_path")
        if not isinstance(relative, str) or not relative:
            raise self._fail(
                stage, f"physical 记录缺少 old_relative_path: {relative!r}"
            )
        return self._resolved_sessions_root / relative

    def _date_bucket_dir(self, frozen: _FrozenNode) -> Path:
        """由冻结 locator 解析日期桶目标目录。"""
        assert frozen.storage_relative_locator is not None
        relative = frozen.storage_relative_locator[len("sessions/"):]
        return self._resolved_sessions_root / relative

    def _stage_session(
        self,
        session_id: str,
        record: dict[str, object],
        context: _MigrationContext,
    ) -> None:
        """pending → staged:采集内容清单 → 旧位置 rename 到 staging → 剥离
        session.json → journal 记录清单与 sha256。

        先动作后记账(act-then-journal):rename 与 journal 写之间的崩溃
        窗口由恢复语义 fail closed(见模块 docstring),数据完整保留。
        """
        stage = "物理迁移(staging)"
        old_path = self._old_path_for(record, stage=stage)
        if not old_path.is_dir() or old_path.is_symlink():
            raise self._fail(
                stage,
                "旧位置目录已不存在且 journal 记 pending,无法证明(外部改动,拒绝继续): "
                f"session_id={session_id}, path={old_path}",
            )
        content_manifest = self._collect_content_manifest(old_path, stage=stage)
        session_json_path = old_path / SESSION_MANIFEST_NAME
        try:
            original_sha = hashlib.sha256(session_json_path.read_bytes()).hexdigest()
        except OSError as error:
            raise self._fail(
                stage, f"session.json 无法读取: {session_json_path}: {error}"
            ) from error
        staging_slot = self._resolved_staging_root / context.migration_id / session_id
        staging_slot.parent.mkdir(parents=True, exist_ok=True)
        if staging_slot.exists() or staging_slot.is_symlink():
            raise self._fail(
                stage,
                f"staging 槽位已存在,拒绝覆盖: {staging_slot}",
            )
        try:
            os.rename(old_path, staging_slot)
        except OSError as error:
            raise self._fail(
                stage,
                f"旧位置 rename 到 staging 失败: {old_path} -> {staging_slot}: {error}",
            ) from error
        _fsync_directory(staging_slot.parent)
        _fsync_directory(old_path.parent)
        stripped_sha = self._strip_session_json(
            staging_slot / SESSION_MANIFEST_NAME, stage=stage
        )
        record["state"] = "staged"
        record["original_session_json_sha256"] = original_sha
        record["stripped_session_json_sha256"] = stripped_sha
        record["content_manifest"] = content_manifest
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

    def _place_session(
        self,
        session_id: str,
        record: dict[str, object],
        context: _MigrationContext,
        frozen: _FrozenNode,
    ) -> None:
        """staged → placed:staging rename 到日期桶(locator 来自冻结映射)。"""
        stage = "物理迁移(placed)"
        staging_slot = self._resolved_staging_root / context.migration_id / session_id
        if not staging_slot.is_dir() or staging_slot.is_symlink():
            raise self._fail(
                stage,
                "journal 记 staged 但 staging 槽位缺失,无法证明(外部改动,拒绝继续): "
                f"session_id={session_id}, path={staging_slot}",
            )
        staged_sha = hashlib.sha256(
            (staging_slot / SESSION_MANIFEST_NAME).read_bytes()
        ).hexdigest()
        expected_sha = record.get("stripped_session_json_sha256")
        if staged_sha != expected_sha:
            raise self._fail(
                stage,
                "staging 内 session.json sha256 与 journal 记录不一致: "
                f"session_id={session_id}, expected={expected_sha!r}, "
                f"actual={staged_sha!r}",
            )
        target = self._date_bucket_dir(frozen)
        if target.exists() or target.is_symlink():
            raise self._fail(
                stage,
                "日期桶目标已存在且 journal 记 staged,拒绝覆盖(fail closed): "
                f"session_id={session_id}, target={target}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging_slot, target)
        except OSError as error:
            raise self._fail(
                stage,
                f"staging rename 到日期桶失败: {staging_slot} -> {target}: {error}",
            ) from error
        _fsync_directory(target.parent)
        _fsync_directory(staging_slot.parent)
        record["state"] = "placed"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)
        self._verify_placed_session(session_id, record, target, stage=stage)

    def _delete_folder(
        self,
        folder_id: str,
        record: dict[str, object],
        context: _MigrationContext,
    ) -> None:
        """folder 删除:目录内除 .boxteam-folder.json 外必须无其它条目。"""
        stage = "物理迁移(folder 删除)"
        old_path = self._old_path_for(record, stage=stage)
        if not old_path.is_dir() or old_path.is_symlink():
            raise self._fail(
                stage,
                "folder 目录已不存在且 journal 记 pending,无法证明(外部改动,拒绝继续): "
                f"folder_id={folder_id}, path={old_path}",
            )
        entries = {entry.name for entry in old_path.iterdir()}
        unexpected = entries - {FOLDER_MANIFEST_NAME}
        if unexpected:
            raise self._fail(
                stage,
                "folder 目录含未预期条目,保留审计并拒绝删除: "
                f"folder_id={folder_id}, path={old_path}, "
                f"unexpected={sorted(unexpected)}",
            )
        try:
            shutil.rmtree(old_path)
        except OSError as error:
            raise self._fail(
                stage, f"folder 目录删除失败: {old_path}: {error}"
            ) from error
        _fsync_directory(old_path.parent)
        record["state"] = "deleted"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

    def _initialize_session_control(
        self,
        session_id: str,
        record: dict[str, object],
        context: _MigrationContext,
        frozen: _FrozenNode,
    ) -> None:
        """placed 后初始化 per-session session-control.sqlite(main row + fence)。"""
        stage = "物理迁移(session-control 初始化)"
        target = self._date_bucket_dir(frozen)
        control_path = target / "session-control.sqlite"
        try:
            store = SessionControlStore(control_path)
        except (RuntimeError, sqlite3.Error, OSError, TypeError, ValueError) as error:
            raise self._fail(
                stage, f"session control store 构造失败: {control_path}: {error}"
            ) from error
        try:
            store.initialize_main_thread(
                frozen.main_thread_id or "", frozen.created_at
            )
            store.initialize_fence("active", 1)
            store.verify_matches_catalog_main_thread(frozen.main_thread_id or "")
        except (RuntimeError, KeyError, sqlite3.Error, TypeError, ValueError) as error:
            raise self._fail(
                stage,
                f"session control 初始化失败: session_id={session_id}, "
                f"path={control_path}: {error}",
            ) from error
        finally:
            store.close()
        record["control_state"] = "initialized"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

    def _sweep_staging(self, context: _MigrationContext) -> None:
        """清理迁移 staging 目录(全部 placed/isolated 后必须为空)。"""
        stage = "物理迁移(staging 清理)"
        staging_dir = self._resolved_staging_root / context.migration_id
        if not staging_dir.is_dir():
            return
        entries = list(staging_dir.iterdir())
        if entries:
            raise self._fail(
                stage,
                "staging 清理时发现残留条目(拒绝吸收): "
                f"paths={sorted(str(entry) for entry in entries)}",
            )
        try:
            staging_dir.rmdir()
        except OSError as error:
            raise self._fail(
                stage, f"staging 目录清理失败: {staging_dir}: {error}"
            ) from error
        _fsync_directory(staging_dir.parent)

    def _assert_staging_clean(
        self, context: _MigrationContext, *, stage: str
    ) -> None:
        """终验口径:迁移 staging 目录必须不存在。"""
        staging_dir = self._resolved_staging_root / context.migration_id
        if staging_dir.exists() or staging_dir.is_symlink():
            raise self._fail(
                stage,
                f"迁移 staging 目录未清理: {staging_dir}",
            )

    def _collect_content_manifest(
        self, directory: Path, *, stage: str
    ) -> list[dict[str, object]]:
        """采集目录内容清单(排除 session.json):相对路径+size+sha256,按路径排序。

        发现符号链接即 fail closed:无法在不读取目标的前提下证明其 bytes
        稳定,绝不静默跳过。
        """
        entries: list[dict[str, object]] = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise self._fail(
                    stage, f"会话目录内发现符号链接,拒绝迁移: {path}"
                )
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            if relative in _CONTENT_MANIFEST_EXCLUDED_NAMES:
                continue
            stat_result = path.stat()
            entries.append(
                {
                    "path": relative,
                    "size": stat_result.st_size,
                    "sha256": self._sha256_file(path, stage=stage),
                }
            )
        return entries

    def _strip_session_json(self, session_json_path: Path, *, stage: str) -> str:
        """剥离 session.json 的可变导航字段(原子写),返回剥离后 sha256。"""
        try:
            raw = session_json_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise self._fail(
                stage, f"session.json 无法读取: {session_json_path}: {error}"
            ) from error
        try:
            manifest = json.loads(raw)
        except ValueError as error:
            raise self._fail(
                stage, f"session.json 无法解析: {session_json_path}: {error}"
            ) from error
        if not isinstance(manifest, dict):
            raise self._fail(
                stage, f"session.json 必须是 JSON object: {session_json_path}"
            )
        for key in _STRIP_MANIFEST_KEYS:
            manifest.pop(key, None)
        encoded = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(session_json_path, encoded)
        return hashlib.sha256(encoded).hexdigest()

    def _verify_placed_session(
        self,
        session_id: str,
        record: dict[str, object],
        target: Path,
        *,
        stage: str,
    ) -> None:
        """placed 校验:session.json sha256 与内容清单逐文件一致(排除 session.json)。"""
        session_json_path = target / SESSION_MANIFEST_NAME
        try:
            actual_sha = hashlib.sha256(session_json_path.read_bytes()).hexdigest()
        except OSError as error:
            raise self._fail(
                stage,
                "placed session 的 session.json 无法读取: "
                f"session_id={session_id}, path={session_json_path}: {error}",
            ) from error
        expected_sha = record.get("stripped_session_json_sha256")
        if actual_sha != expected_sha:
            raise self._fail(
                stage,
                "placed session.json sha256 与 journal 记录不一致(内容被外部改动): "
                f"session_id={session_id}, expected={expected_sha!r}, "
                f"actual={actual_sha!r}",
            )
        actual_manifest = self._collect_content_manifest(target, stage=stage)
        expected_manifest = record.get("content_manifest")
        if actual_manifest != expected_manifest:
            raise self._fail(
                stage,
                "placed session 内容清单与 journal 记录不一致(内容被外部改动): "
                f"session_id={session_id}, "
                f"expected={expected_manifest!r}, actual={actual_manifest!r}",
            )

    def _verify_session_control_rows(
        self, session_dir: Path, frozen: _FrozenNode, *, stage: str
    ) -> None:
        """只读复验 session-control:main row thread_id 与 fence (active, 1)。"""
        control_path = session_dir / "session-control.sqlite"
        if not control_path.is_file():
            raise self._fail(
                stage, f"session-control.sqlite 缺失: {control_path}"
            )
        try:
            connection = sqlite3.connect(control_path)
        except sqlite3.Error as error:
            raise self._fail(
                stage, f"session-control.sqlite 无法打开: {control_path}: {error}"
            ) from error
        try:
            rows = connection.execute(
                "SELECT thread_id FROM thread_catalog WHERE kind = 'main'"
            ).fetchall()
            if len(rows) != 1:
                raise self._fail(
                    stage,
                    "session-control main row 数量非法: "
                    f"path={control_path}, rows={[str(row[0]) for row in rows]}",
                )
            if str(rows[0][0]) != frozen.main_thread_id:
                raise self._fail(
                    stage,
                    "session-control main row 与冻结映射 main_thread_id 不一致: "
                    f"path={control_path}, control={rows[0][0]!r}, "
                    f"frozen={frozen.main_thread_id!r}",
                )
            fence_rows = connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = 1"
            ).fetchall()
            if len(fence_rows) != 1 or str(fence_rows[0][0]) != "active" or int(
                fence_rows[0][1]
            ) != 1:
                raise self._fail(
                    stage,
                    "session-control fence 与期望 (active, 1) 不一致: "
                    f"path={control_path}, rows={fence_rows!r}",
                )
        except sqlite3.Error as error:
            raise self._fail(
                stage,
                f"session-control 校验查询失败(库损坏或未初始化): "
                f"{control_path}: {error}",
            ) from error
        finally:
            connection.close()

    def _create_or_verify_node(
        self, store: SessionCatalogStore, item: _FrozenNode
    ) -> None:
        """gate 内幂等重建:缺失即按冻结字段创建;已存在则逐字段对账。"""
        try:
            existing = store.get_node(item.node_id)
        except KeyError:
            if item.kind == "folder":
                store.create_folder(
                    item.node_id,
                    self._workspace_id,
                    item.parent_node_id,
                    item.display_name,
                )
            else:
                store.create_session_node(
                    item.node_id,
                    self._workspace_id,
                    item.parent_node_id,
                    item.display_name,
                    item.created_at,
                    item.storage_relative_locator,
                    item.main_thread_id,
                )
            return
        self._assert_node_matches(existing, item)

    def _assert_node_matches(
        self, existing: SessionCatalogNode, item: _FrozenNode
    ) -> None:
        """逐字段对账;任一不一致说明库被外部改动,无法证明一致 → fail closed。"""
        mismatches: list[str] = []
        if existing.kind != item.kind:
            mismatches.append(f"kind={existing.kind!r} != {item.kind!r}")
        if existing.parent_node_id != item.parent_node_id:
            mismatches.append(
                f"parent_node_id={existing.parent_node_id!r} != {item.parent_node_id!r}"
            )
        if existing.display_name != item.display_name:
            mismatches.append(
                f"display_name={existing.display_name!r} != {item.display_name!r}"
            )
        if existing.state != "active":
            mismatches.append(f"state={existing.state!r} != 'active'")
        if existing.workspace_id != self._workspace_id:
            mismatches.append(
                f"workspace_id={existing.workspace_id!r} != {self._workspace_id!r}"
            )
        if item.kind == "session":
            existing_created_at = self._parse_persisted_created_at(existing.created_at)
            if existing_created_at != item.created_at:
                mismatches.append(
                    f"created_at={existing.created_at!r} != {item.created_at!r}"
                )
            if existing.storage_relative_locator != item.storage_relative_locator:
                mismatches.append(
                    f"storage_relative_locator={existing.storage_relative_locator!r} "
                    f"!= {item.storage_relative_locator!r}"
                )
            if existing.main_thread_id != item.main_thread_id:
                mismatches.append(
                    f"main_thread_id={existing.main_thread_id!r} "
                    f"!= {item.main_thread_id!r}"
                )
        if mismatches:
            raise self._fail(
                "sqlite-重建对账",
                "SQLite 既有节点与冻结映射不一致(库被外部改动,拒绝覆盖): "
                f"node_id={item.node_id}: " + "; ".join(mismatches),
            )

    @staticmethod
    def _parse_persisted_created_at(value: str | None) -> datetime | None:
        """解析 store 持久化的 created_at;无法解析按不一致处理(返回 None)。"""
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _reconcile(
        self, store: SessionCatalogStore, frozen: list[_FrozenNode]
    ) -> None:
        """全量对账:SQLite 树与冻结映射完全一致(节点集合 + 逐字段)。"""
        stored = self._collect_store_nodes(store)
        frozen_by_id = {item.node_id: item for item in frozen}
        if set(stored) != set(frozen_by_id):
            missing = sorted(set(frozen_by_id) - set(stored))
            unexpected = sorted(set(stored) - set(frozen_by_id))
            raise self._fail(
                "sqlite-全量对账",
                "SQLite 节点集合与冻结映射不一致: "
                f"missing={missing}, unexpected={unexpected}",
            )
        for node_id in sorted(stored):
            self._assert_node_matches(stored[node_id], frozen_by_id[node_id])

    def _collect_store_nodes(
        self, store: SessionCatalogStore
    ) -> dict[str, SessionCatalogNode]:
        """用 store 读 API(list_children 递归)收集全部节点投影。"""
        collected: dict[str, SessionCatalogNode] = {}

        def walk(parent_node_id: str | None) -> None:
            cursor: str | None = None
            while True:
                items, next_cursor, has_more = store.list_children(
                    parent_node_id,
                    limit=_RECONCILE_PAGE_LIMIT,
                    cursor=cursor,
                )
                for node in items:
                    if node.node_id in collected:
                        raise RuntimeError(
                            f"SQLite 目录遍历发现重复节点: {node.node_id}"
                        )
                    collected[node.node_id] = node
                    walk(node.node_id)
                if not has_more:
                    return
                cursor = next_cursor

        walk(None)
        return collected
