"""旧 session catalog(JSON index + 物理树)→ SQLite + 日期桶的一次性迁移机器。

对应 OpenSpec ``add-itemized-rollout-context`` 任务 8.2 **切片2**(在切片1
catalog 重建之上扩展物理树迁移与 session-control 初始化):读取旧权威
(``navigation/session-catalog-index.json`` + 嵌套 Session/Folder/children
物理树),用 R10 ``SessionCatalogStore`` API 幂等重建目标树,再把每个
Session 物理目录经 ``.staging/<migration_id>/`` staging 到日期桶
``sessions/YYYY/MM/DD/{session_id}``(canonical JSONL/item/Turn/assembly
原 bytes 保持),quarantine 目录隔离到 ``orphaned/session-catalog-migration/``,
Folder 物理目录删除,并在新位置初始化 per-session
``session-control.sqlite``(thread catalog main row + fence)。全程以 durable
迁移 journal(v2,含 ``migration_id`` 与 ``physical`` 节)记录单一可恢复
切换点。

红线(模块边界,违反即失去切片2 资格):

- 本模块是**一次性迁移机器**(迁移窗口结束后整体删除),**不切权威**:
  生产 resolver 仍读旧 index;本机器执行后该工作区旧 index 退役为审计件
  (bytes 不改不删),生产切换属切片3 装配。本模块不写旧 index/manifest。
- 调用方必须先在 workspace maintenance gate 下 quiesce execution、
  communication、attachment 等 mutation;本模块只把 SQLite 重建段包进
  ``NavigationTopologyGate`` exclusive 临界区,物理树段依赖维护窗口单人
  操作约定(跨进程互斥归 8.1-C)。
- **不做 rollout 内部 thread 化**(``rollout/`` 留在 session 目录,不搬去
  ``threads/<main_thread_id>/``):main thread node == session node 折叠
  保持(与 R3a 一致),8.5 落地时升级。
- 不装配 container/main.py；8.2-A 调试域步骤已作为同一 journal phase 接入；不做附件迁移
  (依赖 8.3 attachment catalog,遗留如实记录),不建 ThreadCreationRecord
  (8.5-A)。
- 非法旧 ID/date、缺失 parent、物理树/备份不一致、journal 冲突、
  staging 残留、日期桶目标冲突一律 fail closed 或隔离,绝不默认 active,
  绝不扫盘吸收旧树改动;失败保留旧树/隔离区审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_legacy_reader import (
    SessionCatalogLegacyReader,
    SessionCatalogLegacyReaderError,
)
from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.session_tree.support import (
    FOLDER_MANIFEST_NAME,
    SESSION_MANIFEST_NAME,
    SessionPhysicalNode,
)
from app.core.workspace_identity import (
    load_or_create_workspace_id,
    validate_workspace_id,
    workspace_identity_path,
)

__all__ = [
    "QuarantinedNode",
    "SessionCatalogMigrationError",
    "SessionCatalogMigrationResult",
    "SessionCatalogMigrator",
    "migrate_workspace_session_catalog",
]

# journal 落盘位置:maintenance_root / "session-catalog-migration" / "journal.json"。
_JOURNAL_DIRECTORY_NAME = "session-catalog-migration"
_JOURNAL_FILE_NAME = "journal.json"

# 物理迁移 staging 区:sessions_root / ".staging" / <migration_id> / {session_id}。
_STAGING_DIR_NAME = ".staging"

# quarantine 物理隔离目标:boxteam_root / "orphaned" / "session-catalog-migration" /
# {session_id}(boxteam_root = sessions_root.parent;对齐 app/core/AGENTS.md
# 「无法可靠归属的旧数据移入 .boxteam/orphaned/ 并保留可诊断信息」)。
_ORPHANED_DIR_NAME = "orphaned"

# quarantine 原因闭集与节点种类闭集(journal 恢复时逐一校验)。
_QUARANTINE_REASONS = frozenset({"illegal_id", "illegal_date", "parent_quarantined"})
_NODE_KINDS = frozenset({"folder", "session"})

# session.json 剥离键:可变导航父节点/显示名已进 SQLite catalog,物理
# manifest 不再承载(其余字段原样保留)。
_STRIP_MANIFEST_KEYS = ("title", "title_source", "parent_session_id")

# journal physical 节的 per-session 分类与状态闭集。
_CLASSIFICATIONS = frozenset({"migrate", "quarantine"})
_SESSION_PHYSICAL_STATES = frozenset(
    {"pending", "staged", "placed", "quarantine_isolated"}
)
_FOLDER_PHYSICAL_STATES = frozenset({"pending", "deleted", "quarantine_isolated"})
_CONTROL_STATES = frozenset({"pending", "initialized"})

# migration_id 形态:uuid4().hex,32 位小写 hex(staging 目录名,安全单段)。
_MIGRATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

# 内容清单排除项:session.json 单独记录 sha256;session-control.sqlite(+WAL
# 边车)是本机器 placed 后新建的控制库,不属于「迁移的 canonical bytes」,
# 由 _verify_session_control_rows 单独校验。
_CONTENT_MANIFEST_EXCLUDED_NAMES = frozenset(
    {
        SESSION_MANIFEST_NAME,
        "session-control.sqlite",
        "session-control.sqlite-wal",
        "session-control.sqlite-shm",
    }
)

# 全量对账时 list_children 的分页大小。
_RECONCILE_PAGE_LIMIT = 500

QuarantineReason = Literal["illegal_id", "illegal_date", "parent_quarantined"]


@dataclass(frozen=True, slots=True)
class QuarantinedNode:
    """被隔离的旧节点:不进 SQLite 目标树,原样保留在旧树中供审计。"""

    node_id: str
    reason: QuarantineReason


@dataclass(frozen=True, slots=True)
class SessionCatalogMigrationResult:
    """一次迁移的最终计数结果(与 completed journal 的 result 节同构)。"""

    migrated_session_nodes: int
    migrated_folder_nodes: int
    quarantined_nodes: tuple[QuarantinedNode, ...]
    journal_path: Path


@dataclass(frozen=True, slots=True)
class _FrozenNode:
    """journal 冻结的节点映射:重建 SQLite 树的唯一依据。

    folder 只冻结导航四元组;session 额外冻结 created_at、storage 相对
    locator 与已分配的 main_thread_id(恢复时复用,不重新生成)。
    """

    node_id: str
    kind: str
    parent_node_id: str | None
    display_name: str
    created_at: datetime | None
    storage_relative_locator: str | None
    main_thread_id: str | None


@dataclass
class _MigrationContext:
    """一次迁移运行的 journal 工作态(解析自 journal 或 fresh 构造)。

    ``physical`` 是可变 dict:物理迁移阶段逐动作更新并整体落盘;其余字段
    在一次运行内只读。"""

    backup: dict[str, object]
    frozen: list[_FrozenNode]
    quarantined: list[QuarantinedNode]
    migration_id: str
    physical: dict[str, object]


class SessionCatalogMigrationError(RuntimeError):
    """迁移 fail-closed 总类:旧权威不一致、journal 冲突、恢复无法证明、备份复验失败。"""


def _fsync_directory(directory: Path) -> None:
    """fsync 目录项,保证 rename 后的持久性(Linux 上目录可以打开 fsync)。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """tempfile + fsync + os.replace 的原子写(对齐 session_tree/support 的模式)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _topological_order(nodes: list[SessionPhysicalNode]) -> list[SessionPhysicalNode]:
    """返回父先子后的确定性拓扑序;每轮按 node_id 排序保证结果稳定。"""
    remaining = sorted(nodes, key=lambda item: item.node_id)
    placed: set[str] = set()
    ordered: list[SessionPhysicalNode] = []
    while remaining:
        progressed: list[SessionPhysicalNode] = []
        deferred: list[SessionPhysicalNode] = []
        for node in remaining:
            if node.parent_node_id is None or node.parent_node_id in placed:
                progressed.append(node)
            else:
                deferred.append(node)
        if not progressed:
            # resolver 已保证无环;此处是防御性兜底,不静默吞掉。
            raise RuntimeError(
                "迁移冻结阶段无法推进拓扑序(疑似环): "
                f"node_ids={[item.node_id for item in deferred]}"
            )
        for node in progressed:
            ordered.append(node)
            placed.add(node.node_id)
        remaining = deferred
    return ordered


def _frozen_node_to_dict(item: _FrozenNode) -> dict[str, object]:
    """冻结节点 → journal JSON 记录(folder 不携带 session 专属字段)。"""
    payload: dict[str, object] = {
        "node_id": item.node_id,
        "kind": item.kind,
        "parent_node_id": item.parent_node_id,
        "display_name": item.display_name,
    }
    if item.kind == "session":
        payload["created_at"] = (
            item.created_at.isoformat() if item.created_at is not None else None
        )
        payload["storage_relative_locator"] = item.storage_relative_locator
        payload["main_thread_id"] = item.main_thread_id
    return payload


def _quarantined_to_dict(item: QuarantinedNode) -> dict[str, object]:
    return {"node_id": item.node_id, "reason": item.reason}


def _result_to_dict(result: SessionCatalogMigrationResult) -> dict[str, object]:
    return {
        "migrated_session_nodes": result.migrated_session_nodes,
        "migrated_folder_nodes": result.migrated_folder_nodes,
        "quarantined_nodes": [
            _quarantined_to_dict(item) for item in result.quarantined_nodes
        ],
    }


class SessionCatalogMigrator:
    """旧 JSON index + 物理树 → SQLite catalog + 日期桶的一次性迁移器(切片2)。

    临界区约定:SQLite 重建与全量对账包在 ``NavigationTopologyGate``
    exclusive 内;预检、旧权威读取、备份清单、journal 写入与物理树迁移段
    都在 gate 外(物理段依赖 maintenance 窗口单人操作约定,跨进程互斥归
    8.1-C)。

    ``migrate`` 的状态机(单一可恢复切换点,journal v2):

    - 无 journal → 预检 → 读旧权威 → 备份清单 → 冻结/quarantine →
      构造 ``migration_id`` + ``physical`` 节 → 写 ``preparing`` →
      gate 内幂等重建 → 写 ``catalog_rebuilt`` → 完整备份复验 →
      物理迁移(staging → 日期桶 / quarantine 隔离 / folder 删除 /
      session-control 初始化)→ 写 ``physical_migrated`` → 分层终验 →
      ``completed``。
    - journal ``preparing`` / ``catalog_rebuilt`` / ``physical_migrated`` →
      预检 → 按物理段进度分层复验(未动物理树:完整复验 index+manifests;
      已动:仅 index)→ 复用冻结映射(含已分配 main_thread_id)→ gate 内
      幂等重建 → 物理段按 ``physical`` 节逐节点定点继续 → 终验 →
      ``completed``。
    - journal ``completed`` → 分层复验(index + 新位置 session.json sha +
      内容清单 + 隔离/删除布局)后从 result 短路返回(不重跑)。

    物理段恢复语义(任务书 §2.2-C):pending 从旧位置、staged 从 staging、
    placed 跳过(校验新位置 session.json hash 与内容清单)、folder deleted
    跳过;旧位置目录已不存在且 journal 记 pending → fail closed(外部改动
    无法证明);staging 残留目录(journal 无 staged 记录)→ fail closed;
    日期桶目标已存在且 journal 记 pending/staged → fail closed(不覆盖)。
    已知残余崩溃窗口(rename 与 journal 写之间):重入按上述规则 fail
    closed,数据完整保留在 staging/隔离区/日期桶,由人工核账后推进——
    绝不自动吸收。
    """

    JOURNAL_SCHEMA_VERSION = 2
    MIGRATION_NAME = "session-catalog-json-to-sqlite"

    def __init__(
        self,
        *,
        workspace_id: str,
        sessions_root: Path,
        database_path: Path,
        maintenance_root: Path,
    ) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        self._workspace_id = workspace_id
        self._sessions_root = sessions_root
        self._resolved_sessions_root = sessions_root.expanduser().resolve()
        self._database_path = database_path
        self._maintenance_root = maintenance_root
        self._journal_path = (
            maintenance_root / _JOURNAL_DIRECTORY_NAME / _JOURNAL_FILE_NAME
        )
        self._index_path = (
            sessions_root.parent / "navigation" / "session-catalog-index.json"
        )
        # 物理迁移 staging 区与 quarantine 隔离目标(均按 resolve 后根定位)。
        self._resolved_staging_root = self._resolved_sessions_root / _STAGING_DIR_NAME
        self._resolved_orphaned_root = (
            self._resolved_sessions_root.parent
            / _ORPHANED_DIR_NAME
            / _JOURNAL_DIRECTORY_NAME
        )

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def migrate(self) -> SessionCatalogMigrationResult:
        """执行(或恢复)迁移;completed 后幂等短路。

        并发约定:同一目标的并发 migrate 在 gate 内串行重建;两个并发
        **首次**迁移各自冻结映射可能冲突,后进入者会对账失败 fail closed
        (maintenance 窗口内应由单一调用方执行)。

        恢复语义限定(单调用方维护窗口,审查 N5):「失败可从 journal
        恢复重试」只覆盖同进程内、无并发覆盖的窗口。跨进程并发**首次**
        迁移存在败者 journal 覆盖胜者 journal 的死账场景:库中已有胜者
        写入的行而 journal 被败者的 preparing 覆盖时,重入将持续对账
        fail closed,需人工清理非权威 SQLite 后重迁。

        gate 并发语义边界(B2):NavigationTopologyGate 已是跨进程
        fcntl.flock 文件锁,同进程并发与跨进程 migrate 在重建段互斥;
        flock 语义经独立 open fd 获取,不存在事件循环绑定问题。
        """
        journal = self._load_journal()
        if journal is None:
            return await self._fresh_migrate()
        return await self._migrate_with_journal(journal)

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
                if state == "quarantine_isolated":
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
                if state not in ("pending", "quarantine_isolated"):
                    raise self._fail(
                        stage, f"{prefix} quarantine 记录出现非法状态: {state!r}"
                    )
                reason = record.get("quarantine_reason")
                if not isinstance(reason, str) or reason not in _QUARANTINE_REASONS:
                    raise self._fail(
                        stage, f"{prefix}.quarantine_reason 非法: {reason!r}"
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
            if classification == "quarantine" and record.get("state") == "deleted":
                raise self._fail(
                    stage, f"{prefix} quarantine folder 出现非法状态: deleted"
                )
            if classification == "migrate" and record.get("state") == (
                "quarantine_isolated"
            ):
                raise self._fail(
                    stage, f"{prefix} migrate folder 出现非法状态: quarantine_isolated"
                )
            self._validate_old_relative_path(
                record.get("old_relative_path"), stage=stage, context=prefix
            )
            folder_ids.add(folder_id)
            if classification == "quarantine":
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
    # 首次迁移:预检 → 旧权威 → 备份 → 冻结 → preparing
    # ------------------------------------------------------------------

    async def _fresh_migrate(self) -> SessionCatalogMigrationResult:
        self._preflight_index(stage="预检")
        nodes = self._read_old_authority()
        try:
            backup = self._compute_backup(nodes, stage="备份清单")
        except (OSError, ValueError) as error:
            raise self._fail("备份清单", f"旧树备份清单计算失败: {error}") from error
        frozen, quarantined = self._freeze_and_quarantine(nodes)
        migration_id = uuid.uuid4().hex
        physical = self._build_initial_physical(nodes, frozen, quarantined)
        context = _MigrationContext(
            backup=backup,
            frozen=frozen,
            quarantined=quarantined,
            migration_id=migration_id,
            physical=physical,
        )
        self._write_journal_context(context, state="preparing", result=None)
        return await self._run_pipeline(context, entry_state="preparing")

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
        - quarantine_isolated:隔离目录存在于 orphaned;
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
                target = self._resolved_orphaned_root / session_id
                if not target.is_dir():
                    raise self._fail(
                        stage,
                        "quarantine 隔离目录缺失(物理树被外部改动): "
                        f"session_id={session_id}, target={target}",
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
                target = self._resolved_orphaned_root / folder_id
                if not target.is_dir():
                    raise self._fail(
                        stage,
                        "quarantine folder 隔离目录缺失(物理树被外部改动): "
                        f"folder_id={folder_id}, target={target}",
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
        # TODO(identifier): "thr" 前缀待 8.5 thread catalog 落地时补入
        # IdentifierPrefix Literal;create_prefixed_id 基于 uuid4().hex,
        # 天然满足 v4 位 profile。
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

    # ------------------------------------------------------------------
    # 迁移主管线:gate 内重建 → 完整复验 → 物理迁移 → 终验 → completed
    # ------------------------------------------------------------------

    async def _run_pipeline(
        self, context: _MigrationContext, *, entry_state: str
    ) -> SessionCatalogMigrationResult:
        """preparing→catalog_rebuilt→physical_migrated→completed 的公共管线。

        ``entry_state`` 是本次调用进入时的 journal 状态;各阶段 checkpoint
        只在状态发生转移时写入,重入按 physical 节定点幂等继续。
        """
        # 阶段1:catalog 幂等重建(gate 内 create-or-verify + 全量对账)。
        await self._rebuild_catalog_in_gate(context)
        if entry_state == "preparing":
            self._write_journal_context(
                context, state="catalog_rebuilt", result=None
            )
        # 阶段2:物理迁移前完整复验(R11 语义;物理已开始则只复验 index)。
        if self._physical_started(context.physical):
            self._verify_index_only(
                context.backup,
                stage="备份复验(物理迁移已开始,分层 index-only)",
            )
        else:
            self._verify_backup_full(context.backup, stage="备份复验(物理迁移前)")
        # 阶段3:物理树迁移 + session-control 初始化(按 physical 节定点继续)。
        self._run_physical_stage(context)
        if entry_state in ("preparing", "catalog_rebuilt"):
            self._write_journal_context(
                context, state="physical_migrated", result=None
            )
        # 阶段4:分层终验(index + 新位置 sha/清单 + 隔离/删除布局)。
        self._verify_post_physical(context, stage="终验(物理迁移后)")
        # 阶段5:completed。
        result = SessionCatalogMigrationResult(
            migrated_session_nodes=sum(
                1 for item in context.frozen if item.kind == "session"
            ),
            migrated_folder_nodes=sum(
                1 for item in context.frozen if item.kind == "folder"
            ),
            quarantined_nodes=tuple(context.quarantined),
            journal_path=self._journal_path,
        )
        self._write_journal_context(
            context, state="completed", result=_result_to_dict(result)
        )
        return result

    async def _rebuild_catalog_in_gate(self, context: _MigrationContext) -> None:
        """gate 内幂等重建 SQLite 目标树并全量对账(复用切片1 语义)。"""
        gate = NavigationTopologyGate(self._sessions_root)
        async with gate.exclusive():
            try:
                store = SessionCatalogStore(
                    self._database_path, self._sessions_root
                )
            except (
                RuntimeError,
                sqlite3.Error,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                raise self._fail(
                    "sqlite-重建", f"session catalog store 构造失败: {error}"
                ) from error
            try:
                for item in context.frozen:
                    self._create_or_verify_node(store, item)
                self._reconcile(store, context.frozen)
                store.verify_workspace_consistency()
            except SessionCatalogMigrationError:
                raise
            except (
                RuntimeError,
                KeyError,
                sqlite3.Error,
                TypeError,
                ValueError,
                OSError,
            ) as error:
                raise self._fail(
                    "sqlite-重建",
                    "SQLite 目标树重建/对账失败(库保持当前状态,"
                    f"可从 journal 恢复): {error}",
                ) from error
            finally:
                store.close()

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
                self._process_quarantine_session(session_id, record, context)
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
                self._process_quarantine_folder(folder_id, record, context)
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

    def _process_quarantine_folder(
        self,
        folder_id: str,
        record: dict[str, object],
        context: _MigrationContext,
    ) -> None:
        """quarantine folder:pending → 隔离到 orphaned;isolated → 复验存在。

        其内 session 已在深序 session 段先搬出,目录此刻只余 folder manifest。"""
        stage = "物理迁移(quarantine 隔离)"
        target = self._resolved_orphaned_root / folder_id
        if record["state"] == "quarantine_isolated":
            if not target.is_dir():
                raise self._fail(
                    stage,
                    "journal 记 quarantine_isolated 但隔离目录缺失(外部改动): "
                    f"folder_id={folder_id}, target={target}",
                )
            return
        old_path = self._old_path_for(record, stage=stage)
        if not old_path.is_dir() or old_path.is_symlink():
            raise self._fail(
                stage,
                "folder 目录已不存在且 journal 记 pending,无法证明(外部改动,拒绝继续): "
                f"folder_id={folder_id}, path={old_path}",
            )
        if PurePosixPath(folder_id).name != folder_id or folder_id in (".", ".."):
            raise self._fail(
                stage,
                f"quarantine 节点 ID 不能作为隔离目录叶名: {folder_id!r}",
            )
        if (target.exists() or target.is_symlink()) and not (
            target.is_dir() and not any(target.iterdir())
        ):
            raise self._fail(
                stage,
                "隔离目录已存在且非空(拒绝覆盖,保留审计): "
                f"folder_id={folder_id}, target={target}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(old_path, target)
        except OSError as error:
            raise self._fail(
                stage,
                f"旧位置 rename 到隔离区失败: {old_path} -> {target}: {error}",
            ) from error
        _fsync_directory(target.parent)
        _fsync_directory(old_path.parent)
        record["state"] = "quarantine_isolated"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

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

    def _process_quarantine_session(
        self,
        session_id: str,
        record: dict[str, object],
        context: _MigrationContext,
    ) -> None:
        """quarantine session:pending → 隔离到 orphaned;isolated → 复验存在。"""
        stage = "物理迁移(quarantine 隔离)"
        target = self._resolved_orphaned_root / session_id
        if record["state"] == "quarantine_isolated":
            if not target.is_dir():
                raise self._fail(
                    stage,
                    "journal 记 quarantine_isolated 但隔离目录缺失(外部改动): "
                    f"session_id={session_id}, target={target}",
                )
            return
        old_path = self._old_path_for(record, stage=stage)
        if not old_path.is_dir() or old_path.is_symlink():
            raise self._fail(
                stage,
                "旧位置目录已不存在且 journal 记 pending,无法证明(外部改动,拒绝继续): "
                f"session_id={session_id}, path={old_path}",
            )
        if PurePosixPath(session_id).name != session_id or session_id in (".", ".."):
            raise self._fail(
                stage,
                f"quarantine 节点 ID 不能作为隔离目录叶名: {session_id!r}",
            )
        if (target.exists() or target.is_symlink()) and not (
            target.is_dir() and not any(target.iterdir())
        ):
            raise self._fail(
                stage,
                "隔离目录已存在且非空(拒绝覆盖,保留审计): "
                f"session_id={session_id}, target={target}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(old_path, target)
        except OSError as error:
            raise self._fail(
                stage,
                f"旧位置 rename 到隔离区失败: {old_path} -> {target}: {error}",
            ) from error
        _fsync_directory(target.parent)
        _fsync_directory(old_path.parent)
        record["state"] = "quarantine_isolated"
        self._write_journal_context(context, state="catalog_rebuilt", result=None)

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

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def _sha256_file(self, path: Path, *, stage: str) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise self._fail(stage, f"旧树文件无法读取: {path}: {error}") from error

    def _fail(self, stage: str, detail: str) -> SessionCatalogMigrationError:
        """构造带操作阶段与源/目标路径的 fail-closed 错误(app/core 规范)。"""
        return SessionCatalogMigrationError(
            "session catalog 迁移 fail-closed: "
            f"stage={stage}, workspace_id={self._workspace_id}, "
            f"sessions_root={self._sessions_root}, database={self._database_path}, "
            f"journal={self._journal_path}: {detail}"
        )


async def migrate_workspace_session_catalog(
    *,
    workspace_root: Path,
    workspace_id: str | None = None,
) -> SessionCatalogMigrationResult:
    """8.2 显式一次性迁移的 operator 入口：按生产布局推导路径并执行迁移。

    这是 ``SessionCatalogMigrator`` 的最小可运行封装（R19），供
    ``scripts/migrate_session_catalog.py`` 与维护工具调用；不改变迁移
    机器的任何 fail-closed 语义。约定：

    - ``sessions_root`` = ``<workspace_root>/.boxteam/sessions``；
    - ``database_path`` = ``<workspace_root>/.boxteam/navigation/
      session-catalog.sqlite``；
    - ``maintenance_root`` = ``<workspace_root>/.boxteam/maintenance``
      （journal 落在 ``maintenance/session-catalog-migration/journal.json``）；
    - ``workspace_id`` 未给定时经 identity API
      （``load_or_create_workspace_id``，与生产 resolver 同源）读取或
      创建；显式给定时先过 ``validate_workspace_id`` 形态校验，且**当
      identity 文件已存在时必须与之一致**（不一致直接 ``ValueError``，
      拒绝产出与生产 identity 脱节的 catalog）。identity 文件尚不存在的
      工作区允许显式指定并在迁移中记录该值（不创建 identity 文件）；
      这种用法下 catalog 的 workspace_id 属于调用方承诺，生产 resolver
      后续以 identity 文件为准——**跨 workspace 一致性目前没有读取路径
      强制校验**（``verify_workspace_consistency`` 只校验父子同
      workspace），因此显式指定时请优先让 identity 先落地。

    调用约定与迁移机器一致：必须在 workspace maintenance 窗口内单人
    执行（quiesce execution/communication/attachment 等 mutation，跨进程
    互斥归 8.1-C）；迁移 fail closed 时抛出
    ``SessionCatalogMigrationError``，旧树与隔离区原样保留供人工核账。
    """
    resolved_root = workspace_root.expanduser().resolve()
    sessions_root = resolved_root / ".boxteam" / "sessions"
    database_path = resolved_root / ".boxteam" / "navigation" / "session-catalog.sqlite"
    maintenance_root = resolved_root / ".boxteam" / "maintenance"
    if workspace_id is None:
        workspace_id = load_or_create_workspace_id(resolved_root)
    else:
        _ensure_entry_workspace_id_bound(resolved_root, workspace_id)
    migrator = SessionCatalogMigrator(
        workspace_id=workspace_id,
        sessions_root=sessions_root,
        database_path=database_path,
        maintenance_root=maintenance_root,
    )
    return await migrator.migrate()


def _ensure_entry_workspace_id_bound(workspace_root: Path, workspace_id: str) -> None:
    """入口显式 workspace_id 的形态校验与 identity 一致性预检（R19 审查 N3）。

    - 形态非法 → ``ValueError``（``validate_workspace_id``）；
    - identity 文件存在且 workspace_id 不一致 → ``ValueError``（拒绝产出与
      生产 identity 脱节的 catalog；与 runner 的预检同口径但**不要求**
      identity 文件必须存在——入口允许 identity 尚未落地的工作区）；
    """
    normalized = validate_workspace_id(workspace_id)
    identity_path = workspace_identity_path(workspace_root)
    if not identity_path.is_file():
        return
    existing = load_or_create_workspace_id(workspace_root)
    if existing != normalized:
        raise ValueError(
            "显式 workspace_id 与 identity 文件不一致，拒绝迁移: "
            f"explicit={normalized}, identity={existing}, file={identity_path}"
        )
