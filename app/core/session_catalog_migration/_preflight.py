"""预检、旧权威读取、备份复验、冻结映射与 quarantine 分类。"""

from __future__ import annotations

import json
from datetime import UTC
from typing import cast

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_legacy_layout import (
    FOLDER_MANIFEST_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
)
from app.core.session_catalog_legacy_reader import (
    SessionCatalogLegacyReader,
    SessionCatalogLegacyReaderError,
)
from app.core.session_catalog_store import (
    validate_session_id,
    validate_storage_relative_locator,
)

from ._contracts import (
    QuarantinedNode,
    QuarantineReason,
    _FrozenNode,
    _MigrationContext,
    _topological_order,
)


class SessionCatalogMigratorPreflightMixin:
    """预检、旧权威读取、备份复验、冻结映射与 quarantine 分类。"""

    def _build_initial_physical(
        self,
        nodes: list[SessionPhysicalNode],
        frozen: list[_FrozenNode],
        quarantined: list[QuarantinedNode],
    ) -> dict[str, object]:
        """构造 physical 节初值:每节点 pending,分类与冻结/隔离台账一致。"""
        frozen_by_id = {item.node_id: item for item in frozen}
        quarantined_by_id = {item.node_id: item for item in quarantined}
        sessions: dict[str, dict[str, object]] = {}
        folders: dict[str, dict[str, object]] = {}
        for node in nodes:
            old_relative_path = node.path.relative_to(
                self._resolved_sessions_root
            ).as_posix()
            if node.node_id in frozen_by_id:
                classification = "migrate"
                reason: str | None = None
            elif node.node_id in quarantined_by_id:
                classification = "quarantine"
                reason = quarantined_by_id[node.node_id].reason
            else:
                # 冻结/隔离台账必须划分全部节点;否则冻结阶段有缺陷,fail closed。
                raise self._fail(
                    "构造物理节",
                    f"节点未被冻结映射或隔离台账覆盖: node_id={node.node_id}",
                )
            if node.kind == "folder":
                folders[node.node_id] = {
                    "classification": classification,
                    "old_relative_path": old_relative_path,
                    "state": "pending",
                }
                continue
            record: dict[str, object] = {
                "classification": classification,
                "old_relative_path": old_relative_path,
                "state": "pending",
                "original_session_json_sha256": None,
                "stripped_session_json_sha256": None,
                "content_manifest": None,
            }
            if classification == "migrate":
                record["control_state"] = "pending"
            else:
                record["quarantine_reason"] = reason
            sessions[node.node_id] = record
        if len(sessions) != sum(1 for item in nodes if item.kind == "session") or len(
            folders
        ) != sum(1 for item in nodes if item.kind == "folder"):
            raise self._fail("构造物理节", "节点清单存在重复 node_id")
        return {"sessions": sessions, "folders": folders}

    def _preflight_index(self, *, stage: str) -> None:
        """预检旧权威 index:必须存在且 schema_version 与 resolver 权威版本一致。"""
        if self._sessions_root.name != "sessions":
            # 迁移机器只支持生产布局;否则 resolver 的 index 定位会与本模块错位,
            # 并可能触发 resolver 的旧布局扫描导入(违反"不动物理树")。
            raise self._fail(
                stage,
                f"sessions_root 必须是 .boxteam/sessions 生产布局: {self._sessions_root}",
            )
        if not self._index_path.is_file():
            raise self._fail(
                stage,
                f"旧权威 index 缺失,拒绝迁移(也不得扫盘重建): {self._index_path}",
            )
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise self._fail(
                stage, f"旧权威 index 无法读取: {self._index_path}: {error}"
            ) from error
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        if version != SessionCatalogLegacyReader.INDEX_SCHEMA_VERSION:
            raise self._fail(
                stage,
                "旧权威 index schema 版本非法: "
                f"path={self._index_path}, schema_version={version!r}, "
                f"expected={SessionCatalogLegacyReader.INDEX_SCHEMA_VERSION}",
            )

    def _read_old_authority(self) -> list[SessionPhysicalNode]:
        """只读读取旧权威 index 与物理树；失败时原树保持不变。"""
        reader = SessionCatalogLegacyReader(
            self._sessions_root,
            self._index_path,
        )
        try:
            return reader.read()
        except (SessionCatalogLegacyReaderError, RuntimeError, TypeError, ValueError, OSError) as error:
            raise self._fail(
                "读取旧权威",
                "旧权威只读 reader fail-closed 校验未通过(index 内部一致性或物理树"
                f"对账失败),旧树保持原样: {error}",
            ) from error

    def _compute_backup(
        self, nodes: list[SessionPhysicalNode], *, stage: str
    ) -> dict[str, object]:
        """备份清单:index + 每个节点 manifest 的 sha256(可校验旧树一致性)。

        切片1 的备份口径是"可校验旧树一致性";物理 rollout bytes 的全量
        备份属切片2 切换点(本轮不动物理树,旧树本体即审计)。
        """
        manifests: dict[str, dict[str, str]] = {}
        for node in sorted(nodes, key=lambda item: item.node_id):
            manifest_name = (
                FOLDER_MANIFEST_NAME if node.kind == "folder" else SESSION_MANIFEST_NAME
            )
            manifest_path = node.path / manifest_name
            relative = manifest_path.relative_to(self._resolved_sessions_root)
            manifests[node.node_id] = {
                "path": relative.as_posix(),
                "sha256": self._sha256_file(manifest_path, stage=stage),
            }
        return {
            "index_sha256": self._sha256_file(self._index_path, stage=stage),
            "manifests": manifests,
        }

    def _verify_backup_full(self, backup: dict[str, object], *, stage: str) -> None:
        """完整备份复验(R11 语义):重算 index + 全部 manifests 的 sha256 对账。

        使用层(任务书 §2.2-D):catalog 重建后、物理迁移前,以及物理迁移
        尚未开始的重入(preparing/catalog_rebuilt 且 physical 节全 pending)。

        复验边界(审查 N4 及其切片2 延伸):只覆盖 journal backup 清单内的
        文件(index + 已登记 manifests),不检测迁移窗口内新增的未托管
        目录/文件;物理迁移开始后旧 manifests 已被合法消费,改走
        :meth:`_verify_index_only` + :meth:`_verify_post_physical` 分层口径。
        """
        index_sha256 = backup.get("index_sha256")
        manifests = backup.get("manifests")
        if not isinstance(index_sha256, str) or not index_sha256:
            raise self._fail(stage, f"迁移 journal backup.index_sha256 非法: {index_sha256!r}")
        if not isinstance(manifests, dict):
            raise self._fail(stage, f"迁移 journal backup.manifests 非法: {type(manifests).__name__}")
        actual_index = self._sha256_file(self._index_path, stage=stage)
        if actual_index != index_sha256:
            raise self._fail(
                stage,
                "旧权威 index 校验和漂移(旧树在迁移窗口内被改动,拒绝继续): "
                f"index={self._index_path}, expected={index_sha256}, actual={actual_index}",
            )
        for node_id in sorted(manifests):
            entry = manifests[node_id]
            if not isinstance(entry, dict):
                raise self._fail(stage, f"迁移 journal backup.manifests[{node_id}] 非法")
            relative = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise self._fail(
                    stage, f"迁移 journal backup.manifests[{node_id}] 字段非法"
                )
            manifest_path = (self._resolved_sessions_root / relative).resolve()
            if not manifest_path.is_relative_to(self._resolved_sessions_root):
                raise self._fail(
                    stage,
                    f"迁移 journal backup.manifests[{node_id}] 路径越界: {relative!r}",
                )
            actual = self._sha256_file(manifest_path, stage=stage)
            if actual != expected:
                raise self._fail(
                    stage,
                    "旧树 manifest 校验和漂移(旧树在迁移窗口内被改动,拒绝继续): "
                    f"manifest={manifest_path}, node_id={node_id}, "
                    f"expected={expected}, actual={actual}",
                )

    def _verify_index_only(self, backup: dict[str, object], *, stage: str) -> None:
        """分层复验(index-only):物理迁移已开始后,旧 manifests 已被合法
        消费(staging/rename),仅复验 index 审计件校验和;其余分层校验由
        :meth:`_verify_post_physical` 承担。"""
        index_sha256 = backup.get("index_sha256")
        if not isinstance(index_sha256, str) or not index_sha256:
            raise self._fail(stage, f"迁移 journal backup.index_sha256 非法: {index_sha256!r}")
        actual_index = self._sha256_file(self._index_path, stage=stage)
        if actual_index != index_sha256:
            raise self._fail(
                stage,
                "旧权威 index 校验和漂移(旧树在迁移窗口内被改动,拒绝继续): "
                f"index={self._index_path}, expected={index_sha256}, actual={actual_index}",
            )

    def _verify_post_physical(
        self, context: _MigrationContext, *, stage: str
    ) -> None:
        """物理迁移后的分层终验(任务书 §2.2-D):

        - index 审计件校验和不变;
        - 已 placed session:新位置 ``session.json`` sha256 == journal
          physical 节记录(剥离后口径,不复验旧值)+ 新位置内容清单
          (排除 session.json)逐文件 sha256/size 一致;
        - session-control:main row thread_id == 冻结映射 main_thread_id、
          fence == (active, 1);
        - quarantine_isolated:隔离目录源/目标布局与 journal durable intent
          的全树内容证据一致;
        - folder:journal 记 deleted 则旧目录必须已删除;
        - staging 区已清理(无 staged 记录、无残留、目录不存在);
        - completed journal 的 physical 节必须全部处于终态。
        """
        physical_sessions, physical_folders = self._typed_physical(context.physical)
        self._verify_index_only(context.backup, stage=stage)
        frozen_by_id = {item.node_id: item for item in context.frozen}
        for session_id in sorted(physical_sessions):
            record = physical_sessions[session_id]
            state = record["state"]
            if state == "placed":
                target = self._date_bucket_dir(frozen_by_id[session_id])
                if not target.is_dir():
                    raise self._fail(
                        stage,
                        "placed session 新位置目录缺失(物理树被外部改动): "
                        f"session_id={session_id}, target={target}",
                    )
                self._verify_placed_session(session_id, record, target, stage=stage)
                if record["control_state"] != "initialized":
                    raise self._fail(
                        stage,
                        "placed session 的 session-control 未初始化: "
                        f"session_id={session_id}",
                    )
                self._verify_session_control_rows(
                    target, frozen_by_id[session_id], stage=stage
                )
            elif state == "quarantine_isolated":
                self._verify_quarantine_isolated(
                    session_id,
                    record,
                    kind="session",
                    stage=stage,
                )
            else:
                raise self._fail(
                    stage,
                    f"physical 节存在非终态 session(与已完成迁移不一致): "
                    f"session_id={session_id}, state={state!r}",
                )
        for folder_id in sorted(physical_folders):
            record = physical_folders[folder_id]
            if record["classification"] == "quarantine":
                if record["state"] != "quarantine_isolated":
                    raise self._fail(
                        stage,
                        f"quarantine folder 未隔离(与已完成迁移不一致): "
                        f"folder_id={folder_id}, state={record['state']!r}",
                    )
                self._verify_quarantine_isolated(
                    folder_id,
                    record,
                    kind="folder",
                    stage=stage,
                )
                continue
            if record["state"] != "deleted":
                raise self._fail(
                    stage,
                    f"physical 节存在未删除 folder(与已完成迁移不一致): "
                    f"folder_id={folder_id}, state={record['state']!r}",
                )
            old_path = self._old_path_for(record, stage=stage)
            if old_path.exists() or old_path.is_symlink():
                raise self._fail(
                    stage,
                    "folder 目录在 journal 记 deleted 后重现(外部改动,拒绝继续): "
                    f"folder_id={folder_id}, path={old_path}",
                )
        self._assert_staging_clean(context, stage=stage)

    def _typed_physical(
        self, physical: dict[str, object]
    ) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
        """把 physical 节拆成 (sessions, folders) 强类型视图(校验失败 fail closed)。"""
        raw_sessions = physical.get("sessions")
        raw_folders = physical.get("folders")
        if not isinstance(raw_sessions, dict) or not isinstance(raw_folders, dict):
            raise self._fail(
                "物理迁移",
                "physical 节 sessions/folders 结构非法: "
                f"{type(raw_sessions).__name__}, {type(raw_folders).__name__}",
            )
        for mapping in (raw_sessions, raw_folders):
            for node_id, record in mapping.items():
                if not isinstance(record, dict):
                    raise self._fail(
                        "物理迁移", f"physical 节记录必须是 object: {node_id}"
                    )
        return (
            cast(dict[str, dict[str, object]], raw_sessions),
            cast(dict[str, dict[str, object]], raw_folders),
        )

    # ------------------------------------------------------------------
    # 冻结映射 + quarantine 分类
    # ------------------------------------------------------------------

    def _freeze_and_quarantine(
        self, nodes: list[SessionPhysicalNode]
    ) -> tuple[list[_FrozenNode], list[QuarantinedNode]]:
        """按拓扑序冻结合法节点并分类 quarantine(根向下级联判定)。"""
        try:
            ordered = _topological_order(nodes)
        except RuntimeError as error:
            raise self._fail("冻结映射", str(error)) from error
        quarantined: list[QuarantinedNode] = []
        quarantined_ids: set[str] = set()
        frozen: list[_FrozenNode] = []
        for node in ordered:
            reason = self._quarantine_reason_for(node, quarantined_ids)
            if reason is not None:
                quarantined_ids.add(node.node_id)
                quarantined.append(QuarantinedNode(node_id=node.node_id, reason=reason))
                continue
            frozen.append(self._freeze_node(node))
        return frozen, quarantined

    def _quarantine_reason_for(
        self, node: SessionPhysicalNode, quarantined_ids: set[str]
    ) -> QuarantineReason | None:
        """返回 quarantine 原因;合法节点返回 None。

        判定顺序(拓扑序保证 parent 先判):级联 parent_quarantined →
        illegal_id → illegal_date(folder 无 created_at 要求,不检查)。

        口径记录(审查 N3):created_at 缺失/无法解析在旧权威 reader 加载层
        (`_parse_optional_datetime` 要求非空可解析,对解析失败直接 fail
        closed)已被拒绝,到达本分类的 session
        created_at 必为已解析的 datetime;本迁移机的 ``illegal_date``
        quarantine 只覆盖 naive(无 tzinfo)与无法定位 UTC 日期桶的场景。
        """
        if node.parent_node_id is not None and node.parent_node_id in quarantined_ids:
            return "parent_quarantined"
        try:
            validate_session_id(node.node_id)
        except (TypeError, ValueError):
            return "illegal_id"
        if node.kind == "session" and not self._created_at_is_valid(node):
            return "illegal_date"
        return None

    @staticmethod
    def _created_at_is_valid(node: SessionPhysicalNode) -> bool:
        """session created_at 必须带时区且能定位 UTC 日期桶。"""
        created_at = node.created_at
        if created_at.tzinfo is None:
            return False
        try:
            locator = (
                f"sessions/{created_at.astimezone(UTC).date():%Y/%m/%d}/{node.node_id}"
            )
            validate_storage_relative_locator(locator)
        except (TypeError, ValueError, OverflowError):
            return False
        return True

    def _freeze_node(self, node: SessionPhysicalNode) -> _FrozenNode:
        if node.kind == "folder":
            return _FrozenNode(
                node_id=node.node_id,
                kind="folder",
                parent_node_id=node.parent_node_id,
                display_name=node.name,
                created_at=None,
                storage_relative_locator=None,
                main_thread_id=None,
            )
        created_at = node.created_at
        locator = (
            f"sessions/{created_at.astimezone(UTC).date():%Y/%m/%d}/{node.node_id}"
        )
        # "thr" 已在 IdentifierPrefix Literal 中声明；create_prefixed_id
        # 基于 uuid_utils.uuid7()，天然满足 v7 位 profile。
        main_thread_id = create_prefixed_id("thr")
        return _FrozenNode(
            node_id=node.node_id,
            kind="session",
            parent_node_id=node.parent_node_id,
            display_name=node.name,
            created_at=created_at,
            storage_relative_locator=locator,
            main_thread_id=main_thread_id,
        )
