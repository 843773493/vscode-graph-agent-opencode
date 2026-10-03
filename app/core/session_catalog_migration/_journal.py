"""journal 读取/校验与写入(迁移状态机的单一可恢复切换点)。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from app.core.atomic_fs import atomic_write_bytes as _atomic_write_bytes
from app.core.session_catalog_store import (
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)

from ._constants import (
    _CLASSIFICATIONS,
    _CONTENT_MANIFEST_EXCLUDED_NAMES,
    _CONTROL_STATES,
    _FOLDER_PHYSICAL_STATES,
    _MIGRATION_ID_PATTERN,
    _NODE_KINDS,
    _QUARANTINE_REASONS,
    _SESSION_PHYSICAL_STATES,
)
from ._contracts import (
    QuarantinedNode,
    QuarantineReason,
    SessionCatalogMigrationResult,
    _frozen_node_to_dict,
    _FrozenNode,
    _MigrationContext,
    _quarantined_to_dict,
)
from ._quarantine import validate_quarantine_intent


class SessionCatalogMigratorJournalMixin:
    """journal 读取/校验与写入(迁移状态机的单一可恢复切换点)。"""

    # ------------------------------------------------------------------
    # journal 读取与校验
    # ------------------------------------------------------------------

    def _load_journal(self) -> dict[str, object] | None:
        """读取 journal;不存在返回 None;损坏/不兼容 fail closed(保留原文件)。"""
        if not self._journal_path.is_file():
            return None
        try:
            raw = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise self._fail(
                "journal-读取",
                f"迁移 journal 无法解析,保留原文件供人工恢复,不得重置台账: {error}",
            ) from error
        if not isinstance(raw, dict):
            raise self._fail(
                "journal-读取",
                f"迁移 journal 必须是 JSON object: path={self._journal_path}",
            )
        if raw.get("schema_version") != self.JOURNAL_SCHEMA_VERSION:
            # v1 journal 是切片1(R11)测试期产物,生产从未执行迁移;读到 v1
            # 一律 fail closed,不猜测升级路径。
            version_note = (
                "(v1 为切片1 旧格式,拒绝静默升级,须人工核对后删除旧台账重迁) "
                if raw.get("schema_version") == 1
                else ""
            )
            raise self._fail(
                "journal-校验",
                "迁移 journal schema 版本不兼容,拒绝重置或静默升级: "
                f"{version_note}schema_version={raw.get('schema_version')!r}, "
                f"expected={self.JOURNAL_SCHEMA_VERSION}",
            )
        if raw.get("migration_name") != self.MIGRATION_NAME:
            raise self._fail(
                "journal-校验",
                "迁移 journal migration_name 不匹配: "
                f"actual={raw.get('migration_name')!r}, "
                f"expected={self.MIGRATION_NAME!r}",
            )
        if raw.get("workspace_id") != self._workspace_id:
            raise self._fail(
                "journal-校验",
                "迁移 journal workspace_id 与迁移器不一致: "
                f"journal={raw.get('workspace_id')!r}, "
                f"migrator={self._workspace_id!r}",
            )
        state = raw.get("state")
        if state not in ("preparing", "catalog_rebuilt", "physical_migrated", "completed"):
            raise self._fail(
                "journal-校验",
                "迁移 journal state 非法: "
                f"{state!r}(只允许 preparing/catalog_rebuilt/"
                "physical_migrated/completed)",
            )
        self._migration_id_from_journal(raw)
        return raw

    def _migration_id_from_journal(self, journal: dict[str, object]) -> str:
        """校验并返回 journal migration_id(uuid4 hex,即 staging 目录名)。"""
        stage = "journal-校验"
        migration_id = journal.get("migration_id")
        if (
            not isinstance(migration_id, str)
            or _MIGRATION_ID_PATTERN.fullmatch(migration_id) is None
        ):
            raise self._fail(
                stage,
                "迁移 journal migration_id 非法(必须是 32 位小写 hex): "
                f"{migration_id!r}",
            )
        return migration_id

    def _backup_from_journal(
        self, journal: dict[str, object]
    ) -> dict[str, object]:
        """从 journal 解析 backup 节(只做结构校验,校验和复验按层另行执行)。"""
        stage = "journal-恢复备份清单"
        backup = journal.get("backup")
        if not isinstance(backup, dict):
            raise self._fail(
                stage, f"迁移 journal backup 节必须是 object: {type(backup).__name__}"
            )
        index_sha256 = backup.get("index_sha256")
        manifests = backup.get("manifests")
        if not isinstance(index_sha256, str) or not index_sha256:
            raise self._fail(
                stage, f"迁移 journal backup.index_sha256 非法: {index_sha256!r}"
            )
        if not isinstance(manifests, dict):
            raise self._fail(
                stage, f"迁移 journal backup.manifests 非法: {type(manifests).__name__}"
            )
        for node_id, entry in manifests.items():
            if not isinstance(entry, dict):
                raise self._fail(
                    stage, f"迁移 journal backup.manifests[{node_id}] 非法"
                )
            if not isinstance(entry.get("path"), str) or not isinstance(
                entry.get("sha256"), str
            ):
                raise self._fail(
                    stage, f"迁移 journal backup.manifests[{node_id}] 字段非法"
                )
        return backup

    def _physical_from_journal(
        self,
        journal: dict[str, object],
        frozen: list[_FrozenNode],
        quarantined: list[QuarantinedNode],
    ) -> dict[str, object]:
        """解析并交叉校验 journal physical 节;结构/一致性非法即 fail closed。

        交叉校验(frozen/quarantined ↔ physical 三向一致):

        - frozen session 集合 == classification=migrate 的 session 记录集合;
        - quarantined 节点集合 == classification=quarantine 的 session 记录
          集合 ∪ (folders 记录集合 − frozen folder 集合);
        - 每条记录的 state/control_state 在对应闭集内,migrate 记录不得
          出现 quarantine_isolated,quarantine 记录不得出现 staged/placed。
        """
        stage = "journal-恢复物理节"
        raw = journal.get("physical")
        if not isinstance(raw, dict):
            raise self._fail(
                stage, f"迁移 journal physical 节必须是 object: {type(raw).__name__}"
            )
        raw_sessions = raw.get("sessions")
        raw_folders = raw.get("folders")
        if not isinstance(raw_sessions, dict) or not isinstance(raw_folders, dict):
            raise self._fail(
                stage,
                "迁移 journal physical.sessions/folders 必须是 object: "
                f"{type(raw_sessions).__name__}, {type(raw_folders).__name__}",
            )
        frozen_sessions = {
            item.node_id for item in frozen if item.kind == "session"
        }
        frozen_folders = {item.node_id for item in frozen if item.kind == "folder"}
        quarantined_ids = {item.node_id for item in quarantined}
        if len(quarantined_ids) != len(quarantined):
            raise self._fail(stage, "quarantined_nodes 存在重复 node_id")
        migrate_session_ids: set[str] = set()
        quarantine_session_ids: set[str] = set()
        journal_state = journal.get("state")
        for session_id, record in raw_sessions.items():
            prefix = f"physical.sessions[{session_id}]"
            if not isinstance(record, dict):
                raise self._fail(stage, f"{prefix} 必须是 object")
            classification = record.get("classification")
            if classification not in _CLASSIFICATIONS:
                raise self._fail(stage, f"{prefix}.classification 非法: {classification!r}")
            state = record.get("state")
            if state not in _SESSION_PHYSICAL_STATES:
                raise self._fail(stage, f"{prefix}.state 非法: {state!r}")
            self._validate_old_relative_path(
                record.get("old_relative_path"), stage=stage, context=prefix
            )
            if classification == "migrate":
                if state in ("quarantine_intent", "quarantine_isolated"):
                    raise self._fail(
                        stage, f"{prefix} migrate 记录出现非法状态: {state!r}"
                    )
                if record.get("control_state") not in _CONTROL_STATES:
                    raise self._fail(
                        stage, f"{prefix}.control_state 非法: {record.get('control_state')!r}"
                    )
                if state in ("staged", "placed"):
                    self._validate_content_manifest(
                        record.get("content_manifest"), stage=stage, context=prefix
                    )
                    for key in (
                        "original_session_json_sha256",
                        "stripped_session_json_sha256",
                    ):
                        value = record.get(key)
                        if not isinstance(value, str) or not value:
                            raise self._fail(
                                stage, f"{prefix}.{key} 非法: {value!r}"
                            )
                migrate_session_ids.add(session_id)
            else:
                if state not in (
                    "pending",
                    "quarantine_intent",
                    "quarantine_isolated",
                ):
                    raise self._fail(
                        stage, f"{prefix} quarantine 记录出现非法状态: {state!r}"
                    )
                reason = record.get("quarantine_reason")
                if not isinstance(reason, str) or reason not in _QUARANTINE_REASONS:
                    raise self._fail(
                        stage, f"{prefix}.quarantine_reason 非法: {reason!r}"
                    )
                self._validate_quarantine_physical_record(
                    record,
                    node_id=session_id,
                    kind="session",
                    state=cast(str, state),
                    journal_state=journal_state,
                    context=prefix,
                    stage=stage,
                )
                quarantine_session_ids.add(session_id)
        folder_ids: set[str] = set()
        quarantine_folder_ids_parsed: set[str] = set()
        for folder_id, record in raw_folders.items():
            prefix = f"physical.folders[{folder_id}]"
            if not isinstance(record, dict):
                raise self._fail(stage, f"{prefix} 必须是 object")
            classification = record.get("classification")
            if classification not in _CLASSIFICATIONS:
                raise self._fail(
                    stage, f"{prefix}.classification 非法: {classification!r}"
                )
            if record.get("state") not in _FOLDER_PHYSICAL_STATES:
                raise self._fail(
                    stage, f"{prefix}.state 非法: {record.get('state')!r}"
                )
            folder_state = record.get("state")
            if classification == "quarantine" and folder_state not in (
                "pending",
                "quarantine_intent",
                "quarantine_isolated",
            ):
                raise self._fail(
                    stage, f"{prefix} quarantine folder 出现非法状态: {folder_state!r}"
                )
            if classification == "migrate" and folder_state in (
                "quarantine_intent",
                "quarantine_isolated",
            ):
                raise self._fail(
                    stage, f"{prefix} migrate folder 出现非法状态: {folder_state!r}"
                )
            self._validate_old_relative_path(
                record.get("old_relative_path"), stage=stage, context=prefix
            )
            folder_ids.add(folder_id)
            if classification == "quarantine":
                self._validate_quarantine_physical_record(
                    record,
                    node_id=folder_id,
                    kind="folder",
                    state=cast(str, folder_state),
                    journal_state=journal_state,
                    context=prefix,
                    stage=stage,
                )
                quarantine_folder_ids_parsed.add(folder_id)
        # 三向一致性校验。
        if migrate_session_ids != frozen_sessions:
            raise self._fail(
                stage,
                "physical migrate session 集合与冻结映射不一致: "
                f"missing={sorted(frozen_sessions - migrate_session_ids)}, "
                f"unexpected={sorted(migrate_session_ids - frozen_sessions)}",
            )
        if frozen_folders & quarantine_session_ids or frozen_sessions & folder_ids:
            raise self._fail(stage, "physical 记录把 session/folder 归类错位")
        if folder_ids - frozen_folders != quarantine_folder_ids_parsed:
            raise self._fail(
                stage,
                "physical folder 分类与冻结映射不一致: "
                f"folders={sorted(folder_ids)}, frozen={sorted(frozen_folders)}",
            )
        if (
            quarantine_session_ids | quarantine_folder_ids_parsed
            != quarantined_ids
        ):
            raise self._fail(
                stage,
                "physical quarantine 记录与隔离台账不一致: "
                f"physical={sorted(quarantine_session_ids | quarantine_folder_ids_parsed)}, "
                f"journal={sorted(quarantined_ids)}",
            )
        if folder_ids & raw_sessions.keys() or raw_sessions.keys() & folder_ids:
            raise self._fail(stage, "physical sessions/folders 存在重复 node_id")
        return raw

    def _validate_quarantine_physical_record(
        self,
        record: dict[str, object],
        *,
        node_id: str,
        kind: str,
        state: str,
        journal_state: object,
        context: str,
        stage: str,
    ) -> None:
        """任何 quarantine_isolated 状态都必须带可校验 intent。"""
        has_intent = "quarantine_intent" in record
        if state == "pending":
            if has_intent:
                raise self._fail(
                    stage, f"{context} pending 记录不应包含 quarantine_intent"
                )
            return
        if state == "quarantine_isolated" and not has_intent:
            raise self._fail(
                stage,
                f"{context} quarantine_isolated 缺少 durable intent",
            )
        if state == "quarantine_intent" and journal_state == "completed":
            raise self._fail(
                stage, f"{context} completed journal 仍处于 quarantine_intent"
            )
        try:
            validate_quarantine_intent(
                record.get("quarantine_intent"),
                node_id=node_id,
                kind=kind,
                old_relative_path=cast(str, record["old_relative_path"]),
            )
        except (TypeError, ValueError) as error:
            raise self._fail(
                stage, f"{context}.quarantine_intent 非法: {error}"
            ) from error

    def _validate_old_relative_path(
        self, value: object, *, stage: str, context: str
    ) -> None:
        """校验 journal 内旧位置相对路径:相对、posix、无 ``..``、不越界。"""
        if not isinstance(value, str) or not value:
            raise self._fail(stage, f"{context}.old_relative_path 非法: {value!r}")
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or "\\" in value:
            raise self._fail(
                stage, f"{context}.old_relative_path 必须是相对 posix 路径: {value!r}"
            )
        if any(part in ("", ".", "..") for part in candidate.parts):
            raise self._fail(
                stage, f"{context}.old_relative_path 含非法路径段: {value!r}"
            )
        resolved = (self._resolved_sessions_root / candidate).resolve()
        if not resolved.is_relative_to(self._resolved_sessions_root):
            raise self._fail(
                stage, f"{context}.old_relative_path 越界: {value!r}"
            )

    def _validate_content_manifest(
        self, value: object, *, stage: str, context: str
    ) -> None:
        """校验 per-session 内容清单结构:相对路径+size+sha256 列表。"""
        if not isinstance(value, list):
            raise self._fail(
                stage, f"{context}.content_manifest 必须是 list: {type(value).__name__}"
            )
        seen_paths: set[str] = set()
        for offset, entry in enumerate(value):
            prefix = f"{context}.content_manifest[{offset}]"
            if not isinstance(entry, dict):
                raise self._fail(stage, f"{prefix} 必须是 object")
            path_value = entry.get("path")
            size_value = entry.get("size")
            sha_value = entry.get("sha256")
            if not isinstance(path_value, str) or not path_value:
                raise self._fail(stage, f"{prefix}.path 非法: {path_value!r}")
            candidate = PurePosixPath(path_value)
            if (
                candidate.is_absolute()
                or any(part in ("", ".", "..") for part in candidate.parts)
                or path_value in _CONTENT_MANIFEST_EXCLUDED_NAMES
            ):
                raise self._fail(stage, f"{prefix}.path 非法: {path_value!r}")
            if path_value in seen_paths:
                raise self._fail(stage, f"{prefix}.path 重复: {path_value!r}")
            seen_paths.add(path_value)
            if not isinstance(size_value, int) or isinstance(size_value, bool) or size_value < 0:
                raise self._fail(stage, f"{prefix}.size 非法: {size_value!r}")
            if not isinstance(sha_value, str) or len(sha_value) != 64:
                raise self._fail(stage, f"{prefix}.sha256 非法: {sha_value!r}")

    async def _migrate_with_journal(
        self, journal: dict[str, object]
    ) -> SessionCatalogMigrationResult:
        state = journal.get("state")
        if state == "completed":
            # 幂等短路:不重跑迁移;按分层复验(物理迁移后口径)核对审计件与
            # 新位置产物,防止完成后台账与物理树漂移。
            frozen = self._frozen_nodes_from_journal(journal)
            quarantined = self._quarantined_from_journal(journal)
            context = self._context_from_journal(journal, frozen, quarantined)
            self._verify_post_physical(context, stage="备份复验(completed 短路)")
            return self._result_from_completed_journal(journal)
        assert state in ("preparing", "catalog_rebuilt", "physical_migrated")
        # 重入:预检 → 解析 journal → 分层备份复验 → 幂等重建 → 物理段定点继续。
        self._preflight_index(stage=f"预检({state} 重入)")
        frozen = self._frozen_nodes_from_journal(journal)
        quarantined = self._quarantined_from_journal(journal)
        context = self._context_from_journal(journal, frozen, quarantined)
        return await self._run_pipeline(context, entry_state=cast(str, state))

    def _context_from_journal(
        self,
        journal: dict[str, object],
        frozen: list[_FrozenNode],
        quarantined: list[QuarantinedNode],
    ) -> _MigrationContext:
        """从 journal 组装迁移工作态(backup/migration_id/physical 全量校验)。"""
        backup = self._backup_from_journal(journal)
        migration_id = self._migration_id_from_journal(journal)
        physical = self._physical_from_journal(journal, frozen, quarantined)
        return _MigrationContext(
            backup=backup,
            frozen=frozen,
            quarantined=quarantined,
            migration_id=migration_id,
            physical=physical,
        )

    def _result_from_completed_journal(
        self, journal: dict[str, object]
    ) -> SessionCatalogMigrationResult:
        """从 completed journal 的 result 节重建结果;结构非法即 fail closed。"""
        stage = "journal-恢复结果"
        raw = journal.get("result")
        if not isinstance(raw, dict):
            raise self._fail(
                stage, f"completed journal 缺少合法 result 节: {type(raw).__name__}"
            )
        sessions = raw.get("migrated_session_nodes")
        folders = raw.get("migrated_folder_nodes")
        quarantined_raw = raw.get("quarantined_nodes")
        if not isinstance(sessions, int) or isinstance(sessions, bool):
            raise self._fail(stage, f"result.migrated_session_nodes 非法: {sessions!r}")
        if not isinstance(folders, int) or isinstance(folders, bool):
            raise self._fail(stage, f"result.migrated_folder_nodes 非法: {folders!r}")
        if not isinstance(quarantined_raw, list):
            raise self._fail(stage, f"result.quarantined_nodes 非法: {quarantined_raw!r}")
        quarantined = tuple(
            self._quarantined_from_journal_item(item, stage=stage, offset=offset)
            for offset, item in enumerate(quarantined_raw)
        )
        return SessionCatalogMigrationResult(
            migrated_session_nodes=sessions,
            migrated_folder_nodes=folders,
            quarantined_nodes=quarantined,
            journal_path=self._journal_path,
        )

    def _frozen_nodes_from_journal(
        self, journal: dict[str, object]
    ) -> list[_FrozenNode]:
        """恢复冻结映射(含已分配 main_thread_id);结构非法即 fail closed。"""
        stage = "journal-恢复冻结映射"
        raw_nodes = journal.get("frozen_nodes")
        if not isinstance(raw_nodes, list):
            raise self._fail(stage, f"迁移 journal 缺少 frozen_nodes 列表: {type(raw_nodes).__name__}")
        frozen: list[_FrozenNode] = []
        seen: set[str] = set()
        for offset, raw in enumerate(raw_nodes):
            item = self._frozen_node_from_journal_item(raw, stage=stage, offset=offset)
            if item.node_id in seen:
                raise self._fail(stage, f"冻结映射包含重复节点: {item.node_id}")
            seen.add(item.node_id)
            frozen.append(item)
        return frozen

    def _frozen_node_from_journal_item(
        self, raw: object, *, stage: str, offset: int
    ) -> _FrozenNode:
        prefix = f"frozen_nodes[{offset}]"
        if not isinstance(raw, dict):
            raise self._fail(stage, f"{prefix} 必须是 object: {type(raw).__name__}")
        node_id = raw.get("node_id")
        kind = raw.get("kind")
        parent_node_id = raw.get("parent_node_id")
        display_name = raw.get("display_name")
        if not isinstance(node_id, str) or not node_id:
            raise self._fail(stage, f"{prefix}.node_id 非法: {node_id!r}")
        try:
            validate_session_id(node_id)
        except (TypeError, ValueError) as error:
            raise self._fail(stage, f"{prefix} 节点 ID 非法: {node_id!r}: {error}") from error
        if kind not in _NODE_KINDS:
            raise self._fail(stage, f"{prefix}.kind 非法: {kind!r}")
        if not isinstance(display_name, str) or not display_name:
            raise self._fail(stage, f"{prefix}.display_name 非法: {display_name!r}")
        if parent_node_id is not None and not isinstance(parent_node_id, str):
            raise self._fail(stage, f"{prefix}.parent_node_id 非法: {parent_node_id!r}")
        if kind == "folder":
            return _FrozenNode(
                node_id=node_id,
                kind="folder",
                parent_node_id=parent_node_id,
                display_name=display_name,
                created_at=None,
                storage_relative_locator=None,
                main_thread_id=None,
            )
        created_at_raw = raw.get("created_at")
        locator = raw.get("storage_relative_locator")
        main_thread_id = raw.get("main_thread_id")
        if not isinstance(created_at_raw, str):
            raise self._fail(stage, f"{prefix}.created_at 非法: {created_at_raw!r}")
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except ValueError as error:
            raise self._fail(
                stage, f"{prefix}.created_at 无法解析: {created_at_raw!r}: {error}"
            ) from error
        if created_at.tzinfo is None:
            raise self._fail(stage, f"{prefix}.created_at 缺少时区: {created_at_raw!r}")
        if not isinstance(locator, str) or not isinstance(main_thread_id, str):
            raise self._fail(
                stage,
                f"{prefix}.storage_relative_locator/main_thread_id 非法: "
                f"{locator!r}, {main_thread_id!r}",
            )
        try:
            validate_storage_relative_locator(locator)
            validate_thread_id(main_thread_id)
        except (TypeError, ValueError) as error:
            raise self._fail(
                stage, f"{prefix} locator/main_thread_id 校验失败: {error}"
            ) from error
        return _FrozenNode(
            node_id=node_id,
            kind="session",
            parent_node_id=parent_node_id,
            display_name=display_name,
            created_at=created_at,
            storage_relative_locator=locator,
            main_thread_id=main_thread_id,
        )

    def _quarantined_from_journal(
        self, journal: dict[str, object]
    ) -> list[QuarantinedNode]:
        stage = "journal-恢复隔离台账"
        raw_items = journal.get("quarantined_nodes")
        if not isinstance(raw_items, list):
            raise self._fail(
                stage, f"迁移 journal 缺少 quarantined_nodes 列表: {type(raw_items).__name__}"
            )
        return [
            self._quarantined_from_journal_item(item, stage=stage, offset=offset)
            for offset, item in enumerate(raw_items)
        ]

    def _quarantined_from_journal_item(
        self, raw: object, *, stage: str, offset: int
    ) -> QuarantinedNode:
        prefix = f"quarantined_nodes[{offset}]"
        if not isinstance(raw, dict):
            raise self._fail(stage, f"{prefix} 必须是 object: {type(raw).__name__}")
        node_id = raw.get("node_id")
        reason = raw.get("reason")
        if not isinstance(node_id, str) or not node_id:
            raise self._fail(stage, f"{prefix}.node_id 非法: {node_id!r}")
        if not isinstance(reason, str) or reason not in _QUARANTINE_REASONS:
            raise self._fail(stage, f"{prefix}.reason 非法: {reason!r}")
        return QuarantinedNode(
            node_id=node_id, reason=cast(QuarantineReason, reason)
        )

    # ------------------------------------------------------------------
    # journal 写入
    # ------------------------------------------------------------------

    def _write_journal_context(
        self,
        context: _MigrationContext,
        *,
        state: str,
        result: dict[str, object] | None,
    ) -> None:
        """把迁移工作态整体落盘为 journal v2(原子写,含 physical 节)。"""
        payload: dict[str, object] = {
            "schema_version": self.JOURNAL_SCHEMA_VERSION,
            "migration_name": self.MIGRATION_NAME,
            "state": state,
            "workspace_id": self._workspace_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "migration_id": context.migration_id,
            "backup": context.backup,
            "frozen_nodes": [
                _frozen_node_to_dict(item) for item in context.frozen
            ],
            "quarantined_nodes": [
                _quarantined_to_dict(item) for item in context.quarantined
            ],
            "physical": context.physical,
        }
        if result is not None:
            payload["result"] = result
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(self._journal_path, encoded)
