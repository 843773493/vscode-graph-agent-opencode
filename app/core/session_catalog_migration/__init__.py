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

import hashlib
from pathlib import Path

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    validate_workspace_id,
    workspace_identity_path,
)

from ._catalog import SessionCatalogMigratorCatalogMixin
from ._constants import (
    _JOURNAL_DIRECTORY_NAME,
    _JOURNAL_FILE_NAME,
    _ORPHANED_DIR_NAME,
    _STAGING_DIR_NAME,
)
from ._contracts import QuarantinedNode as QuarantinedNode
from ._contracts import SessionCatalogMigrationError as SessionCatalogMigrationError
from ._contracts import SessionCatalogMigrationResult as SessionCatalogMigrationResult
from ._fresh import SessionCatalogMigratorFreshMixin
from ._journal import SessionCatalogMigratorJournalMixin
from ._physical import SessionCatalogMigratorPhysicalMixin
from ._pipeline import SessionCatalogMigratorPipelineMixin
from ._preflight import SessionCatalogMigratorPreflightMixin

__all__ = [
    "QuarantinedNode",
    "SessionCatalogMigrationError",
    "SessionCatalogMigrationResult",
    "SessionCatalogMigrator",
    "migrate_workspace_session_catalog",
]


class SessionCatalogMigrator(
    SessionCatalogMigratorJournalMixin,
    SessionCatalogMigratorFreshMixin,
    SessionCatalogMigratorPreflightMixin,
    SessionCatalogMigratorPipelineMixin,
    SessionCatalogMigratorCatalogMixin,
    SessionCatalogMigratorPhysicalMixin,
):
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
