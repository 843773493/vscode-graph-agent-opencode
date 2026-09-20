"""SessionSubtreeDeleteService —— NavigationSubtreeDeleteRecord 删除流编排
（OpenSpec add-itemized-rollout-context 任务 8.1-B，R14）。

本模块实现 design.md §9（约 794-808 行）定义的子树删除协议：Session 删除
（含全部逻辑后代）与 ``recursive=true`` 的 Folder 删除走**同一协议**——

1. **gate exclusive 内冻结 + 原子关闭可见性**：
   ``create_or_get_subtree_delete_record``（递归 CTE 冻结精确
   node/revision 集合与每个 session 的不可变 locator）→
   ``mark_subtree_deleting``（单事务 CAS 整树 active→deleting——**唯一
   逻辑可见性关闭点**，「正常导航/业务 reader 不得看到部分子树仍
   active」由该事务原子性保证）→ 出 gate。
2. **drain（逐 session 按冻结顺序）**：对每个未记录进度的 session，先在
   对应 ``SessionLifecycleGate`` exclusive 临界区内执行已绑定的运行时与
   外部资源复合 drain 回调，再 CAS 关闭其 session-control fence（``(active, 1)`` →
   ``(deleting, 2)``；已 deleting 幂等跳过；generation 不符 fail
   closed），再把日期桶目录原子 rename 到
   ``sessions_root/.deleting/<idempotency_key>/<session_id>/`` 并做目录
   fsync durability barrier（**barrier 先于进度记录**：进度只承诺已
   durable 的事实，避免「进度已记、rename 未持久」的崩溃窗口产生源位置
   孤儿目录），最后 ``record_drain_progress`` 持久化进度。
3. **gate exclusive 内 finish**：``finish_subtree_delete`` 单事务校验
   drain 完整性并删除全部冻结 node 行（tombstone=行删除）、record →
   completed。

红线（模块边界，违反即失去本轮资格）：

- **不切权威**：生产路径由 ``SessionCatalogPathResolver`` 装配，SQLite
  catalog 仍是唯一权威；本流只负责编排冻结子树、资源 drain、物理隔离
  与 tombstone，不直接承载导航业务规则。
- **单/批同一协议**：单 Session 删除也走本流，避免单/批两种互相矛盾的
  线性化点；非空 folder 的非递归删除仍由
  ``SessionCatalogStore.delete_empty_folder`` 明确拒绝（design.md §9 约
  776 行）。
- **恢复语义**：任一中途崩溃保持整棵子树 deleting 并按 record 定点继续
  （deleting 未 drain → 重入 drain；drain 中途 → 按
  ``drained_session_ids`` 定点继续；finish 前 → 重入 finish），**不回滚
  active**、不向 UI 逐个暴露已删/未删混合状态、不扫盘猜测目标；
  ``.deleting/`` 下无 record 对应的残留目录不属于本流，不触碰、不吸收
  （对齐 app/core/AGENTS.md「不得扫描磁盘并静默吸收改动」）。
- **简化边界（如实记录）**：read guard/旧 lease/通信/附件收敛等待属
  8.1-C/8.8——当前进程内无跨进程 reader 与 waiter，drain 不实现等待段；
  跨进程 topology/lifecycle 文件锁与 shared/exclusive gate 语义归 8.1-C
  （``NavigationTopologyGate`` 为进程内原语）；retention/
  ForkRetentionClaim 预检属 8.1-D。
- **并发边界**：进程内同 key 并发由 per-key ``asyncio.Lock`` 串行收敛到
  同一结果；同 key 的 preimage 是 (workspace_id, root_node_id)，不同
  preimage 冲突；子树闭包由 gate 纪律与 mark CAS（revision 漂移即拒绝）
  保证，跨进程并发写归 8.1-C。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.session_catalog_store import (
    SessionCatalogStore,
    SubtreeDeleteRecord,
    validate_session_id,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionLifecycleGate,
)

__all__ = ["SessionSubtreeDeleteService", "SubtreeDeleteResult"]

# 物理隔离区：sessions_root / ".deleting" / <idempotency_key> / <session_id>。
_DELETING_DIR_NAME = ".deleting"

# 控制库文件名（与 R12 迁移机器、R13 创建流一致）。
_CONTROL_DATABASE_NAME = "session-control.sqlite"

# fence 初始 generation（R12/R13 初始化值）；CAS 成功后推进为 2。
_FENCE_INITIAL_GENERATION = 1


def _fsync_directory(directory: Path) -> None:
    """fsync 目录项，保证新建/改名条目的持久性（模式对齐 R12/R13）。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SubtreeDeleteResult:
    """一次子树删除流的最终结果（record 已 completed 的幂等投影）。"""

    root_node_id: str
    frozen_node_ids: tuple[str, ...]
    drained_session_ids: tuple[str, ...]
    record_state: str


class SessionSubtreeDeleteService:
    """子树删除流编排（不切权威，供切换轮装配）。

    ``delete`` 状态机（幂等：同 key 重入按 record 状态分支收敛）：

    1. gate exclusive 内 ``create_or_get_subtree_delete_record``（新
       record 或幂等取既有）→ ``preparing`` 时 ``mark_subtree_deleting``
       （CAS 整树 deleting，唯一逻辑可见性关闭点）→ 出 gate。既有
       record：``completed`` → 幂等返回；``aborted`` → RuntimeError（含
       reason，换新 key 重试）；``preparing`` → 重入从 mark 继续；
       ``deleting``/``draining`` → 重入从 drain 继续。
    2. drain：对冻结集合内每个 session（folder 跳过——无物理目录）按冻结
       顺序在 SessionLifecycleGate exclusive 临界区内执行运行时排空 →
       fence CAS → rename 隔离 → durability barrier → 进度记录；已在
       ``drained_session_ids`` → 跳过。
    3. gate exclusive 内 ``finish_subtree_delete``（全树 tombstone）→
       出 gate。
    4. 返回 :class:`SubtreeDeleteResult`（frozen_node_ids、drained、
       record state）。
    """

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        sessions_root: Path,
        workspace_id: str,
        gate: NavigationTopologyGate | None = None,
        session_gate: SessionLifecycleGate | None = None,
        session_drain_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        self._store = store
        self._sessions_root = sessions_root.expanduser().resolve()
        # service 与 catalog store 必须指向同一物理根（日期桶/隔离区定位
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
        # 2.3-D drain 的 per-session 生命周期 gate：exclusive 获取天然等待
        # 全部 SessionReadGuard（共享读者）释放后才能进入。
        self._session_gate = (
            session_gate
            if session_gate is not None
            else SessionLifecycleGate(self._sessions_root)
        )
        if session_drain_callback is not None and not callable(
            session_drain_callback
        ):
            raise TypeError(
                "session_drain_callback 必须可调用: "
                f"{session_drain_callback!r}"
            )
        self._session_drain_callback = session_drain_callback
        self._key_locks: dict[str, asyncio.Lock] = {}

    def set_session_drain_callback(
        self,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """绑定删除前的 Session 运行时与资源复合排空回调。

        回调属于共享删除协议的一部分，调用方负责在一个显式回调中按
        固定顺序收敛各类 owner；它必须在对应
        :class:`SessionLifecycleGate` exclusive 临界区内完成，且抛错时
        删除流立即停止，绝不能继续 fence、物理隔离或 finish。生产装配
        在 NodeDebugService 与 SessionResourceService 都创建后绑定；未
        绑定时仅用于没有运行时/资源服务的低层存储测试。
        """
        if not callable(callback):
            raise TypeError(f"session_drain_callback 必须可调用: {callback!r}")
        if self._session_drain_callback is not None:
            raise RuntimeError("Session 删除排空回调已绑定")
        self._session_drain_callback = callback

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def delete(
        self,
        *,
        idempotency_key: str,
        root_node_id: str,
    ) -> SubtreeDeleteResult:
        """执行（或幂等恢复）一次子树删除（协议见模块/类 docstring）。

        ``idempotency_key`` 同时是 ``.deleting/`` 隔离目录名，必须是安全
        单段路径名；``root_node_id`` 是被删子树的根（folder 或 session）。
        """
        self._validate_delete_inputs(
            idempotency_key=idempotency_key,
            root_node_id=root_node_id,
        )
        # 进程内同 key 串行：并发同 key delete 收敛到同一结果；gate 只包
        # 住两个 catalog 短临界区（锁序 gate → 单 SQLite 写事务）。
        async with self._key_lock(idempotency_key):
            # 步骤 1：gate exclusive 内冻结 + mark（唯一逻辑可见性关闭点）。
            async with self._gate.exclusive():
                record = self._store.create_or_get_subtree_delete_record(
                    idempotency_key=idempotency_key,
                    workspace_id=self._workspace_id,
                    root_node_id=root_node_id,
                )
                if record.state == "preparing":
                    self._store.mark_subtree_deleting(idempotency_key)
                    record = self._store.get_subtree_delete_record(
                        idempotency_key
                    )
            if record.state == "completed":
                return self._result_from_record(record)
            if record.state == "aborted":
                raise RuntimeError(
                    "subtree delete record 已中止，调用方须换新 "
                    f"idempotency_key 重试: key={idempotency_key!r}, "
                    f"reason={record.abort_reason!r}"
                )
            # 步骤 2：drain（gate 外，逐 session 按冻结顺序；每 session
            # 在 Session gate exclusive 内完成 lease 收敛 → fence CAS → 隔离）。
            await self._drain(record, idempotency_key)
            # 步骤 3：gate exclusive 内 finish（全树 tombstone）。
            async with self._gate.exclusive():
                self._store.finish_subtree_delete(idempotency_key)
            record = self._store.get_subtree_delete_record(idempotency_key)
            return self._result_from_record(record)

    # ------------------------------------------------------------------
    # 输入校验与并发原语
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_delete_inputs(
        *,
        idempotency_key: str,
        root_node_id: str,
    ) -> None:
        """delete 入参校验（在任何状态变更之前 fail fast）。"""
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError(
                f"idempotency_key 不能为空: {idempotency_key!r}"
            )
        # idempotency_key 是 .deleting/ 隔离目录名，必须是安全单段路径名。
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
        validate_session_id(root_node_id)

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

    def _date_bucket_dir(self, storage_relative_locator: str) -> Path:
        relative = storage_relative_locator[len("sessions/"):]
        return self._sessions_root / relative

    def _deleting_dir(self, idempotency_key: str) -> Path:
        return self._sessions_root / _DELETING_DIR_NAME / idempotency_key

    # ------------------------------------------------------------------
    # drain 阶段
    # ------------------------------------------------------------------

    async def _drain(
        self, record: SubtreeDeleteRecord, idempotency_key: str
    ) -> None:
        """drain 阶段：逐 session 运行时排空 + fence CAS + 物理隔离 + 进度记录。

        按冻结顺序（node_id 排序）遍历冻结 session 集合；folder 跳过
        （无物理目录，由 finish 的行删除承担）；已在
        ``drained_session_ids`` 的 session 跳过（崩溃重入定点继续）。
        每个 session 在 SessionLifecycleGate exclusive 内完成复合资源回调、
        lease 收敛与隔离（2.3-D：删除等待原 reader/收敛旧 lease 后才隔离目录）。
        """
        for session_id in sorted(record.frozen_session_locators):
            if session_id in record.drained_session_ids:
                continue
            async with self._session_gate.exclusive(session_id):
                # 复合资源回调必须先于 fence CAS 与物理 rename。回调失败时
                # 保留源目录与 catalog deleting 状态，供同一 record 定点
                # 重试；不得制造“目录已删但进程/claim 未收敛”的伪成功。
                if self._session_drain_callback is not None:
                    await self._session_drain_callback(session_id)
                self._drain_session(
                    idempotency_key=idempotency_key,
                    session_id=session_id,
                    storage_relative_locator=record.frozen_session_locators[
                        session_id
                    ],
                )
            self._store.record_drain_progress(idempotency_key, session_id)

    def _drain_session(
        self,
        *,
        idempotency_key: str,
        session_id: str,
        storage_relative_locator: str,
    ) -> None:
        """单个 session 的物理隔离：fence CAS → rename → durability barrier。

        恢复窗口（上次运行 rename 已完成、进度未记）：隔离目标已存在且
        源日期桶目录已不在 → 校验目标一致性后幂等跳过 fence CAS 与
        rename，仅由调用方补记进度（rename 严格后于 fence CAS，目标存在
        即证明 fence 已关闭）。
        """
        stage = "子树删除 drain"
        source = self._date_bucket_dir(storage_relative_locator)
        target = self._deleting_dir(idempotency_key) / session_id
        if target.exists() or target.is_symlink():
            self._verify_isolated_target(
                source, target, session_id, stage=stage
            )
            return
        if not source.is_dir() or source.is_symlink():
            raise RuntimeError(
                f"{stage}: 源日期桶目录缺失且隔离目标不存在，无法证明"
                f"（外部改动，fail closed）: session_id={session_id}, "
                f"source={source}, target={target}"
            )
        self._cas_session_fence(source, session_id, stage=stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)
        except OSError as error:
            raise RuntimeError(
                f"{stage}: 日期桶目录隔离失败: {source} -> {target}: {error}"
            ) from error
        # durability barrier（目录 fsync）。barrier 先于进度记录：进度
        # 只承诺已 durable 的事实，避免「进度已记、rename 未持久」的崩
        # 溃窗口把目录留在源位置造成孤儿。
        _fsync_directory(target.parent)
        _fsync_directory(target.parent.parent)
        _fsync_directory(source.parent)

    def _cas_session_fence(
        self,
        source: Path,
        session_id: str,
        *,
        stage: str,
    ) -> None:
        """关闭 session 的本地 fence：CAS ``(active, 1)`` → ``(deleting, 2)``。

        - ``False`` 且 fence 已 ``deleting`` → 幂等跳过（上次运行 CAS
          后、rename 前崩溃的恢复窗口）；
        - ``False`` 且 generation 不符 → fail closed；
        - 控制库文件缺失 → fail closed（``SessionControlStore`` 构造会
          新建空库，必须先判存在性，绝不在源位置制造假库）。
        """
        control_path = source / _CONTROL_DATABASE_NAME
        if not control_path.is_file():
            raise RuntimeError(
                f"{stage}: session 日期桶目录缺少 {_CONTROL_DATABASE_NAME}"
                f"（无法执行 fence CAS，fail closed）: "
                f"session_id={session_id}, path={control_path}"
            )
        try:
            control = SessionControlStore(control_path)
        except sqlite3.Error as error:
            raise RuntimeError(
                f"{stage}: session-control.sqlite 无法打开: "
                f"{control_path}: {error}"
            ) from error
        try:
            advanced = control.cas_fence_transition(
                _FENCE_INITIAL_GENERATION, "deleting"
            )
            if not advanced:
                state, generation = control.get_fence()
                if state != "deleting":
                    raise RuntimeError(
                        f"{stage}: session fence CAS 失败且 fence 非 "
                        f"deleting（generation 不符，fail closed）: "
                        f"session_id={session_id}, fence_state={state!r}, "
                        f"fence_generation={generation}"
                    )
        except KeyError as error:
            raise RuntimeError(
                f"{stage}: session-control fence row 缺失（库被外部改动，"
                f"fail closed）: session_id={session_id}, "
                f"path={control_path}: {error}"
            ) from error
        finally:
            control.close()

    def _verify_isolated_target(
        self,
        source: Path,
        target: Path,
        session_id: str,
        *,
        stage: str,
    ) -> None:
        """恢复窗口校验：隔离目标存在时验证源已不在且目标内容可归属。

        一致性判据（record 未冻结内容清单，无法逐字节对账，按可归属性
        fail closed）：源必须已不在（源+目标并存 = 外部改动）；目标必须
        是目录且携带 session-control.sqlite；其 fence 必须已是
        ``deleting``（fence CAS 严格先于 rename，该状态无法由本协议之外
        产生）。
        """
        if source.exists() or source.is_symlink():
            raise RuntimeError(
                f"{stage}: 源日期桶目录与隔离目标同时存在（外部改动，"
                f"fail closed）: session_id={session_id}, source={source}, "
                f"target={target}"
            )
        if not target.is_dir() or target.is_symlink():
            raise RuntimeError(
                f"{stage}: 隔离目标不是目录（外部改动，fail closed）: "
                f"session_id={session_id}, target={target}"
            )
        control_path = target / _CONTROL_DATABASE_NAME
        if not control_path.is_file():
            raise RuntimeError(
                f"{stage}: 隔离目标缺少 {_CONTROL_DATABASE_NAME}"
                f"（内容不一致，fail closed）: "
                f"session_id={session_id}, target={target}"
            )
        try:
            control = SessionControlStore(control_path)
        except sqlite3.Error as error:
            raise RuntimeError(
                f"{stage}: 隔离目标 session-control.sqlite 无法打开: "
                f"{control_path}: {error}"
            ) from error
        try:
            state, _generation = control.get_fence()
        except (KeyError, sqlite3.Error) as error:
            raise RuntimeError(
                f"{stage}: 隔离目标控制库 fence 不可读（内容不一致，"
                f"fail closed）: session_id={session_id}, "
                f"path={control_path}: {error}"
            ) from error
        finally:
            control.close()
        if state != "deleting":
            raise RuntimeError(
                f"{stage}: 隔离目标 fence 非 deleting（fence CAS 严格先于 "
                f"rename，该状态无法由本协议产生，外部改动 fail closed）: "
                f"session_id={session_id}, fence_state={state!r}"
            )

    # ------------------------------------------------------------------
    # 结果投影
    # ------------------------------------------------------------------

    def _result_from_record(
        self,
        record: SubtreeDeleteRecord,
    ) -> SubtreeDeleteResult:
        return SubtreeDeleteResult(
            root_node_id=record.root_node_id,
            frozen_node_ids=tuple(item.node_id for item in record.frozen_node_ids),
            drained_session_ids=record.drained_session_ids,
            record_state=record.state,
        )
