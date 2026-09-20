"""SessionCreationRecord 新模型 Session 创建流编排（OpenSpec 8.1-A，R13）。

本模块实现 design.md §9 定义的 Session 创建流：
**gate 内 create-or-get record（冻结身份/locator/preimage）→ staging 完整
准备（剥离版 session.json + session-control.sqlite）→ durability barrier →
原子 rename 到冻结日期桶 → gate 内 CAS publish（唯一可见性提交点）**。

红线（模块边界，违反即失去本轮资格）：

- **单一权威**：生产 Session 创建通过 SQLite catalog 写入
  ``nodes``，publish 是 catalog 可见性提交点；物理目录只按冻结 locator
  落盘，不能反向成为导航来源。
  container/main.py/path_utils。
- **publish 是唯一可见性提交点**：「任何 reader 不得看到缺 main、双 main
  或非法初始 fence 的 Session」由此保证——``publish_creation_record``
  在同一 workspace catalog 事务内插入 nodes 行；nodes 行插入之前，session
  目录不可见（staging 与日期桶中的未发布目录不被 reader 扫描：正常
  runtime 只按 catalog locator 定位、不扫盘）。
- **staging 命名空间约定**：本流 staging 目录名 =
  ``session_creation_idempotency_key``（即 operation_id）；R12 迁移机器
  （``session_catalog_migration.py``）staging 目录名 = ``migration_id``。
  两者共享 ``sessions/.staging/`` 父目录，但各自只触碰自己的子目录，
  残留互不扫描、互不吸收；本流对非本流 key 的 staging 目录不做任何
  假设（「staging 残留无 record → 不属于本流」）。
- **CAS 失败定点回收**：publish CAS 失败（父节点漂移/删除/目标被占）时，
  把日期桶目录隔离到 ``.boxteam/orphaned/session-creation/<key>/`` 后
  abort record 并抛错；调用方须换新 idempotency_key 重试，不静默改绑。
  选隔离而非 rename 回 staging 的理由：(1) record 已终态 aborted，同 key
  重试必被拒，保留在 staging 会与「staging 目录名 = 活跃 operation」的
  命名空间约定混淆；(2) orphaned/ 隔离区保留可诊断审计（对齐
  app/core/AGENTS.md「无法可靠归属的旧数据移入 .boxteam/orphaned/」与
  R12 迁移机器的隔离惯例）；(3) 回收后 staging 区干净。
- **并发边界**：进程内同 key 并发由 per-key ``asyncio.Lock`` 串行收敛
  到同一结果；gate 为进程内原语，跨进程互斥归 8.1-C（构造时可注入共享
  workspace gate；``gate=None`` 时自建进程内 gate，仅适合测试/单进程）。
- **准备中断窗口**：staging 准备中途崩溃会留下不完整 staging（staging
  目录已建但文件不全）；重入按「staging 已存在 → 逐文件校验」fail
  closed（保留现场供人工核账，不自动删除重建——无法与外部篡改区分）。
  人工清理 staging 后同 key 重试即可完整重准备（record 仍 preparing）。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    SessionCreationRecord,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import NavigationTopologyGate

__all__ = [
    "SessionCreationResult",
    "SessionCreationService",
    "build_session_creation_manifest",
    "compute_session_creation_preimage_hash",
    "serialize_session_creation_manifest",
    "validate_session_metadata_keys",
]

# session.json 文件名（创建流自用常量）。
_SESSION_MANIFEST_NAME = "session.json"

# 控制库文件名（与 R12 迁移机器一致）。
_CONTROL_DATABASE_NAME = "session-control.sqlite"

# staging 区：sessions_root / ".staging" / <idempotency_key>。
_STAGING_DIR_NAME = ".staging"

# CAS 失败定点回收隔离区：sessions_root.parent("orphaned") / "session-creation"
# / <idempotency_key>（sessions_root.parent 即 workspace .boxteam/ 根）。
_ORPHANED_DIR_NAME = "orphaned"
_ORPHANED_CREATION_DIR_NAME = "session-creation"

# 调用方须提供的剥离 manifest 字段闭集（与真实 session.json 的剥离版一致：
# 真实 manifest 含 created_at/updated_at/session_id/workspace_id/kind/
# delegation/generation_origin/current_agent_id/current_provider_id/
# context_source_session_id 十字段，剥去 title/title_source/
# parent_session_id 后剩余的调用方侧六字段）。
_METADATA_CALLER_KEYS = frozenset(
    {
        "kind",
        "delegation",
        "generation_origin",
        "current_agent_id",
        "current_provider_id",
        "context_source_session_id",
    }
)

# 禁止进入剥离 manifest 的可变导航字段（catalog 是唯一权威）。
_FORBIDDEN_MANIFEST_KEYS = ("title", "title_source", "parent_session_id")


def _fsync_directory(directory: Path) -> None:
    """fsync 目录项，保证新建/改名条目的持久性（模式对齐 R12 迁移机器）。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    """fsync 已存在文件（durability barrier 组成部分）。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """tempfile + fsync + os.replace 的原子写（模式对齐 R12 迁移机器，
    自行实现）。"""
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


def validate_session_metadata_keys(session_metadata: dict[str, object]) -> None:
    """校验调用方 session_metadata 字段闭集（缺/多一律 fail closed）。

    剥离 manifest 的字段集是封闭的：调用方恰好提供六个非派生字段；禁止
    出现 title/title_source/parent_session_id（可变导航字段只存 catalog）。
    """
    if not isinstance(session_metadata, dict):
        raise TypeError(
            f"session_metadata 必须是 dict: {type(session_metadata).__name__}"
        )
    keys = set(session_metadata)
    missing = sorted(_METADATA_CALLER_KEYS - keys)
    unexpected = sorted(keys - _METADATA_CALLER_KEYS)
    if missing or unexpected:
        raise ValueError(
            "session_metadata 字段集非法（剥离 manifest 字段闭集）: "
            f"missing={missing}, unexpected={unexpected}, "
            f"allowed={sorted(_METADATA_CALLER_KEYS)}"
        )


def compute_session_creation_preimage_hash(
    *,
    workspace_id: str,
    parent_node_id: str | None,
    title: str,
    session_metadata: dict[str, object],
) -> str:
    """计算 preimage_hash = sha256(canonical JSON of preimage 四元组)。

    canonical 形式：``sort_keys=True`` + 紧凑分隔符 + UTF-8（本模块与测试
    共用，保证同 preimage 判定跨重入稳定；不追求跨实现 JCS 兼容）。
    ``session_metadata`` 值必须 JSON 可序列化（``datetime`` 等对象须先转
    文本，否则 ``json.dumps`` 抛 ``TypeError`` fail fast）。
    """
    payload = {
        "workspace_id": workspace_id,
        "parent_node_id": parent_node_id,
        "title": title,
        "session_metadata": session_metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_session_creation_manifest(
    record: SessionCreationRecord,
    session_metadata: dict[str, object],
) -> dict[str, object]:
    """构造剥离版 session.json 内容（确定性纯函数）。

    - 调用方六字段闭集校验（:func:`validate_session_metadata_keys`）；
    - session_id / workspace_id / created_at 来自 record 冻结值；
    - updated_at 初始等于 created_at（确定性：幂等重入校验 staging 字节
      一致的前提）；
    - 最终 manifest 复查不得含可变导航字段（防御性，闭集校验已保证）。
    """
    validate_session_metadata_keys(session_metadata)
    manifest: dict[str, object] = dict(session_metadata)
    manifest["session_id"] = record.session_id
    manifest["workspace_id"] = record.workspace_id
    manifest["created_at"] = record.created_at
    manifest["updated_at"] = record.created_at
    for key in _FORBIDDEN_MANIFEST_KEYS:
        if key in manifest:
            raise RuntimeError(
                f"剥离 manifest 不允许包含可变导航字段: {key!r}"
            )
    return manifest


def serialize_session_creation_manifest(
    manifest: dict[str, object],
) -> bytes:
    """剥离 manifest 的确定性序列化（服务写入与校验、测试构造共用同一来源）。

    ``sort_keys=True`` 保证字节只依赖内容（同 preimage 重入的元数据字典
    插入顺序不同也不影响校验）。
    """
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SessionCreationResult:
    """一次创建流的最终结果（node 已发布、record 已 published）。"""

    session_id: str
    main_thread_id: str
    storage_relative_locator: str
    node: SessionCatalogNode
    record_state: str


class SessionCreationService:
    """SQLite catalog Session 创建流编排。

    ``create`` 状态机（幂等：同 key 重入按 record 状态分支收敛）：

    1. gate exclusive 内 ``create_or_get_creation_record``（新 record 或
       幂等取既有）→ 出 gate。published → 校验 nodes 行一致后幂等返回；
       aborted → RuntimeError（含 reason，换新 key 重试）；preparing →
       恢复/继续。
    2. preparing 时先探目标日期桶：已存在且内容与预期一致 → 视为
       rename 已完成（rename 后、publish 前崩溃的恢复窗口），直接进入
       步骤 4；不一致 → fail closed。目标不存在 → 步骤 3。
    3. staging 准备（gate 外）：``.staging/<key>/`` 已存在 → 逐文件校验
       （session.json 字节、session-control main row/fence、条目集）复用；
       不存在 → 完整准备 + durability barrier（文件 fsync + 目录 fsync）。
    4. 原子 rename staging → 冻结日期桶（父目录链 mkdir + fsync）。
    5. gate exclusive 内 ``publish_creation_record``（CAS）。CAS 失败 →
       定点回收日期桶目录到 orphaned → abort record → RuntimeError
       （含原因；调用方换新 key 重试）。

    恢复语义覆盖四个崩溃点：record 后/staging 前（staging 不存在 → 重
    准备）；staging 后/rename 前（staging 校验复用 → 继续 rename）；
    rename 后/publish 前（目标已存在且一致 → 直接 publish）；publish 后
    （record published → 幂等返回）。staging 准备**中途**崩溃留下不完整
    staging 时重入 fail closed（见模块 docstring「准备中断窗口」）。
    """

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        sessions_root: Path,
        workspace_id: str,
        gate: NavigationTopologyGate | None = None,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        self._store = store
        self._sessions_root = sessions_root.expanduser().resolve()
        # service 与 catalog store 必须指向同一物理根（staging/日期桶定位
        # 与 catalog locator 一致的前提），不一致 fail fast。
        if self._sessions_root != store.sessions_root:
            raise ValueError(
                "sessions_root 与 catalog store 的 sessions_root 不一致: "
                f"service={self._sessions_root}, store={store.sessions_root}"
            )
        self._workspace_id = workspace_id
        self._gate = (
            gate if gate is not None else NavigationTopologyGate(self._sessions_root)
        )
        self._key_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        idempotency_key: str,
        title: str,
        parent_node_id: str | None,
        session_metadata: dict[str, object],
    ) -> SessionCreationResult:
        """执行（或幂等恢复）一次新模型 Session 创建。

        ``title`` 写入 catalog ``display_name`` 与 preimage，**不写入**
        session.json；``session_metadata`` 为剥离 manifest 的调用方六字段
        （见 :func:`validate_session_metadata_keys`）。
        """
        self._validate_create_inputs(
            idempotency_key=idempotency_key,
            title=title,
            parent_node_id=parent_node_id,
            session_metadata=session_metadata,
        )
        preimage_hash = compute_session_creation_preimage_hash(
            workspace_id=self._workspace_id,
            parent_node_id=parent_node_id,
            title=title,
            session_metadata=session_metadata,
        )
        # 进程内同 key 串行：并发同 key create 收敛到同一结果；gate 只包
        # 住两个 catalog 短临界区（锁序 gate → 单 SQLite 写事务）。
        async with self._key_lock(idempotency_key):
            async with self._gate.exclusive():
                record = self._store.create_or_get_creation_record(
                    idempotency_key=idempotency_key,
                    workspace_id=self._workspace_id,
                    parent_node_id=parent_node_id,
                    display_name=title,
                    created_at=datetime.now(UTC),
                    preimage_hash=preimage_hash,
                )
            if record.state == "published":
                return self._result_for_published(record)
            if record.state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，调用方须换新 "
                    f"idempotency_key 重试: key={idempotency_key!r}, "
                    f"reason={record.abort_reason!r}"
                )
            # record.state == preparing：恢复或继续。
            target = self._date_bucket_dir(record.storage_relative_locator)
            if target.exists() or target.is_symlink():
                # rename 后、publish 前崩溃的恢复窗口：目标内容一致才继续。
                self._verify_session_directory(
                    target,
                    record,
                    session_metadata,
                    stage="恢复(目标日期桶已存在)",
                )
                staging = self._staging_dir(idempotency_key)
                if staging.exists() or staging.is_symlink():
                    raise RuntimeError(
                        "目标日期桶与 staging 同时存在（外部改动，fail "
                        f"closed）: key={idempotency_key!r}, "
                        f"target={target}, staging={staging}"
                    )
                return await self._publish_in_gate(
                    record, idempotency_key
                )
            # 步骤 2/3：staging 准备 + 原子 rename（gate 外）。
            self._prepare_staging(record, session_metadata, idempotency_key)
            self._rename_staging_to_target(
                record, session_metadata, idempotency_key
            )
            # 步骤 4：gate exclusive 内 CAS publish（唯一可见性提交点）。
            return await self._publish_in_gate(record, idempotency_key)

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    def _validate_create_inputs(
        self,
        *,
        idempotency_key: str,
        title: str,
        parent_node_id: str | None,
        session_metadata: dict[str, object],
    ) -> None:
        """create 入参校验（在任何状态变更之前 fail fast）。"""
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError(
                f"idempotency_key 不能为空: {idempotency_key!r}"
            )
        # idempotency_key 是 staging/隔离目录名，必须是安全单段路径名。
        if (
            idempotency_key in (".", "..")
            or "/" in idempotency_key
            or "\\" in idempotency_key
            or "\x00" in idempotency_key
        ):
            raise ValueError(
                "idempotency_key 必须是安全单段路径名（不含分隔符/./..）: "
                f"{idempotency_key!r}"
            )
        if not isinstance(title, str) or not title:
            raise ValueError(f"title 不能为空: {title!r}")
        if parent_node_id is not None and not isinstance(parent_node_id, str):
            raise TypeError(
                f"parent_node_id 必须是字符串或 None: {parent_node_id!r}"
            )
        validate_session_metadata_keys(session_metadata)
        # preimage 计算兼作 JSON 可序列化校验（不可序列化值 fail fast）。
        compute_session_creation_preimage_hash(
            workspace_id=self._workspace_id,
            parent_node_id=parent_node_id,
            title=title,
            session_metadata=session_metadata,
        )

    def _key_lock(self, idempotency_key: str) -> asyncio.Lock:
        """按 key create-or-get 进程内串行锁（锁随进程生命周期保留）。"""
        lock = self._key_locks.get(idempotency_key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[idempotency_key] = lock
        return lock

    # ------------------------------------------------------------------
    # 路径定位
    # ------------------------------------------------------------------

    def _staging_dir(self, idempotency_key: str) -> Path:
        return self._sessions_root / _STAGING_DIR_NAME / idempotency_key

    def _date_bucket_dir(self, storage_relative_locator: str) -> Path:
        relative = storage_relative_locator[len("sessions/"):]
        return self._sessions_root / relative

    def _orphaned_dir(self, idempotency_key: str) -> Path:
        return (
            self._sessions_root.parent
            / _ORPHANED_DIR_NAME
            / _ORPHANED_CREATION_DIR_NAME
            / idempotency_key
        )

    # ------------------------------------------------------------------
    # staging 准备 + durability barrier
    # ------------------------------------------------------------------

    def _expected_manifest_bytes(
        self,
        record: SessionCreationRecord,
        session_metadata: dict[str, object],
    ) -> bytes:
        """预期的剥离 session.json 字节（写入与校验共用同一确定性来源）。"""
        manifest = build_session_creation_manifest(record, session_metadata)
        return serialize_session_creation_manifest(manifest)

    def _prepare_staging(
        self,
        record: SessionCreationRecord,
        session_metadata: dict[str, object],
        idempotency_key: str,
    ) -> None:
        """staging 已存在 → 逐文件校验复用；不存在 → 完整准备 + barrier。"""
        staging = self._staging_dir(idempotency_key)
        if staging.exists() or staging.is_symlink():
            self._verify_session_directory(
                staging, record, session_metadata, stage="staging 复验"
            )
            return
        manifest_bytes = self._expected_manifest_bytes(record, session_metadata)
        staging.mkdir(parents=True, exist_ok=False)
        # 准备中断（任一步骤抛错）：不删除现场（无法与外部篡改区分），
        # 保留不完整 staging 供人工核账，重入 fail closed（见模块
        # docstring「准备中断窗口」）。
        _atomic_write_bytes(staging / _SESSION_MANIFEST_NAME, manifest_bytes)
        self._initialize_control_database(staging, record)
        # durability barrier：session.json（文件+父目录）与控制库（文件）
        # 已各自 fsync；此处补 staging 目录与 .staging 父目录 fsync
        # （新建 <key> 目录条目的持久性）。
        _fsync_directory(staging)
        _fsync_directory(staging.parent)

    def _initialize_control_database(
        self,
        staging: Path,
        record: SessionCreationRecord,
    ) -> None:
        """staging 内初始化 session-control.sqlite（main row + active fence）。

        「任何 reader 不得看到缺 main、双 main 或非法初始 fence 的
        Session」的物理侧保证：main row 与 fence 在目录可见（rename 到
        日期桶）之前初始化并校验完毕。
        """
        control_path = staging / _CONTROL_DATABASE_NAME
        store = SessionControlStore(control_path)
        try:
            store.initialize_main_thread(
                record.main_thread_id,
                datetime.fromisoformat(record.created_at),
            )
            store.initialize_fence("active", 1)
            store.verify_matches_catalog_main_thread(record.main_thread_id)
        finally:
            store.close()
        _fsync_file(control_path)
        _fsync_directory(staging)

    # ------------------------------------------------------------------
    # 校验（staging 复用 / 恢复目标 / published record ↔ node）
    # ------------------------------------------------------------------

    def _verify_session_directory(
        self,
        directory: Path,
        record: SessionCreationRecord,
        session_metadata: dict[str, object],
        *,
        stage: str,
    ) -> None:
        """逐文件校验 staging/日期桶目录与预期一致；不一致 fail closed。"""
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError(
                f"{stage}: 目录缺失或不是目录（外部改动，fail closed）: "
                f"{directory}"
            )
        entries = sorted(entry.name for entry in directory.iterdir())
        expected_entries = sorted(
            [_SESSION_MANIFEST_NAME, _CONTROL_DATABASE_NAME]
        )
        if entries != expected_entries:
            raise RuntimeError(
                f"{stage}: 条目集与预期不一致（外部改动或上次准备中断，"
                f"fail closed）: path={directory}, "
                f"expected={expected_entries}, actual={entries}"
            )
        manifest_path = directory / _SESSION_MANIFEST_NAME
        expected_bytes = self._expected_manifest_bytes(record, session_metadata)
        try:
            actual_bytes = manifest_path.read_bytes()
        except OSError as error:
            raise RuntimeError(
                f"{stage}: session.json 无法读取: {manifest_path}: {error}"
            ) from error
        expected_sha = hashlib.sha256(expected_bytes).hexdigest()
        actual_sha = hashlib.sha256(actual_bytes).hexdigest()
        if actual_sha != expected_sha:
            raise RuntimeError(
                f"{stage}: session.json sha256 与预期不一致（外部改动，"
                f"fail closed）: path={manifest_path}, "
                f"expected={expected_sha}, actual={actual_sha}"
            )
        self._verify_control_database(directory, record, stage=stage)

    def _verify_control_database(
        self,
        session_dir: Path,
        record: SessionCreationRecord,
        *,
        stage: str,
    ) -> None:
        """只读复验 session-control：唯一 main row == record.main_thread_id
        且 fence == (active, 1)（对齐 R12 迁移机器的只读校验模式）。"""
        control_path = session_dir / _CONTROL_DATABASE_NAME
        if not control_path.is_file():
            raise RuntimeError(
                f"{stage}: session-control.sqlite 缺失: {control_path}"
            )
        try:
            connection = sqlite3.connect(control_path)
        except sqlite3.Error as error:
            raise RuntimeError(
                f"{stage}: session-control.sqlite 无法打开: "
                f"{control_path}: {error}"
            ) from error
        try:
            rows = connection.execute(
                "SELECT thread_id FROM thread_catalog WHERE kind = 'main'"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != record.main_thread_id:
                raise RuntimeError(
                    f"{stage}: session-control main row 与 record 不一致: "
                    f"path={control_path}, "
                    f"rows={[str(row[0]) for row in rows]}, "
                    f"expected={record.main_thread_id!r}"
                )
            fence_rows = connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = 1"
            ).fetchall()
            if (
                len(fence_rows) != 1
                or str(fence_rows[0][0]) != "active"
                or int(fence_rows[0][1]) != 1
            ):
                raise RuntimeError(
                    f"{stage}: session-control fence 与期望 (active, 1) "
                    f"不一致: path={control_path}, rows={fence_rows!r}"
                )
        except sqlite3.Error as error:
            raise RuntimeError(
                f"{stage}: session-control.sqlite 读取失败: "
                f"{control_path}: {error}"
            ) from error
        finally:
            connection.close()

    def _verify_node_matches_record(
        self,
        node: SessionCatalogNode,
        record: SessionCreationRecord,
    ) -> None:
        """校验已发布 node 行与 record 冻结值一致；不一致 fail closed。"""
        expected = {
            "kind": "session",
            "state": "active",
            "parent_node_id": record.parent_node_id,
            "display_name": record.display_name,
            "workspace_id": record.workspace_id,
            "created_at": record.created_at,
            "storage_relative_locator": record.storage_relative_locator,
            "main_thread_id": record.main_thread_id,
        }
        actual = {
            "kind": node.kind,
            "state": node.state,
            "parent_node_id": node.parent_node_id,
            "display_name": node.display_name,
            "workspace_id": node.workspace_id,
            "created_at": node.created_at,
            "storage_relative_locator": node.storage_relative_locator,
            "main_thread_id": node.main_thread_id,
        }
        if actual != expected:
            mismatched = sorted(
                key for key, value in expected.items() if actual[key] != value
            )
            raise RuntimeError(
                "published record 与 nodes 行不一致（catalog 被外部改动，"
                f"fail closed）: session_id={record.session_id}, "
                f"mismatched={mismatched}, expected={expected}, "
                f"actual={actual}"
            )

    def _result_for_published(
        self,
        record: SessionCreationRecord,
    ) -> SessionCreationResult:
        """record published 的幂等返回：校验 nodes 行存在且一致。"""
        try:
            node = self._store.get_node(record.session_id)
        except KeyError as error:
            raise RuntimeError(
                "creation record 已 published 但 nodes 行缺失（catalog 被"
                f"外部改动，fail closed）: session_id={record.session_id}"
            ) from error
        self._verify_node_matches_record(node, record)
        return SessionCreationResult(
            session_id=record.session_id,
            main_thread_id=record.main_thread_id,
            storage_relative_locator=record.storage_relative_locator,
            node=node,
            record_state=record.state,
        )

    # ------------------------------------------------------------------
    # rename 与 publish
    # ------------------------------------------------------------------

    def _rename_staging_to_target(
        self,
        record: SessionCreationRecord,
        session_metadata: dict[str, object],
        idempotency_key: str,
    ) -> None:
        """staging 原子 rename 到冻结日期桶（locator 来自 record）。"""
        stage = "rename 到日期桶"
        staging = self._staging_dir(idempotency_key)
        if not staging.is_dir() or staging.is_symlink():
            raise RuntimeError(
                f"{stage}: staging 缺失或不是目录: {staging}"
            )
        target = self._date_bucket_dir(record.storage_relative_locator)
        if target.exists() or target.is_symlink():
            # 校验与 rename 之间目标被外部放置：内容一致 → 视为 rename 已
            # 完成（清理已校验一致的冗余 staging 副本后继续 publish）；
            # 不一致 → fail closed。
            self._verify_session_directory(
                target, record, session_metadata, stage=stage
            )
            shutil.rmtree(staging)
            _fsync_directory(staging.parent)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging, target)
        except OSError as error:
            raise RuntimeError(
                f"{stage}: staging rename 到日期桶失败: "
                f"{staging} -> {target}: {error}"
            ) from error
        _fsync_directory(target.parent)
        _fsync_directory(staging.parent)

    async def _publish_in_gate(
        self,
        record: SessionCreationRecord,
        idempotency_key: str,
    ) -> SessionCreationResult:
        """gate exclusive 内 CAS publish；失败定点回收 + abort。"""
        try:
            async with self._gate.exclusive():
                node = self._store.publish_creation_record(idempotency_key)
        except RuntimeError as error:
            # CAS 失败：定点回收日期桶目录到 orphaned 隔离区（物理回收
            # 失败则保持 record preparing 并直接抛错，人工核账）。
            self._quarantine_target(record, idempotency_key)
            self._store.abort_creation_record(idempotency_key, str(error))
            raise RuntimeError(
                "session creation publish CAS 失败，已定点回收日期桶目录并 "
                "abort record，调用方须换新 idempotency_key 重试: "
                f"key={idempotency_key!r}: {error}"
            ) from error
        return SessionCreationResult(
            session_id=record.session_id,
            main_thread_id=record.main_thread_id,
            storage_relative_locator=record.storage_relative_locator,
            node=node,
            record_state="published",
        )

    def _quarantine_target(
        self,
        record: SessionCreationRecord,
        idempotency_key: str,
    ) -> None:
        """CAS 失败后的定点回收：日期桶目录 → orphaned/session-creation/<key>。"""
        stage = "CAS 失败定点回收"
        target = self._date_bucket_dir(record.storage_relative_locator)
        if not target.is_dir() or target.is_symlink():
            raise RuntimeError(
                f"{stage}: 日期桶目录缺失或不是目录，无法定点回收"
                f"（外部改动，fail closed）: key={idempotency_key!r}, "
                f"target={target}"
            )
        quarantine_target = self._orphaned_dir(idempotency_key)
        if quarantine_target.exists() or quarantine_target.is_symlink():
            raise RuntimeError(
                f"{stage}: 隔离目标已存在，拒绝覆盖: {quarantine_target}"
            )
        quarantine_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(target, quarantine_target)
        except OSError as error:
            raise RuntimeError(
                f"{stage}: 日期桶目录隔离失败: {target} -> "
                f"{quarantine_target}: {error}"
            ) from error
        _fsync_directory(quarantine_target.parent)
        _fsync_directory(target.parent)
