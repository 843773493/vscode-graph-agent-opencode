"""SessionCreationRecord 新模型 Session 创建流编排（OpenSpec 8.1-A，R13）。

本模块实现 design.md §9 定义的 Session 创建流：
**gate 内 create-or-get record（冻结身份/locator/preimage）→ staging 完整
准备（剥离版 session.json、session-control.sqlite 与唯一 main thread 节点）
→ durability barrier → 原子 rename 到冻结日期桶 → gate 内 CAS publish
（唯一可见性提交点）**。

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
  冻结的 ``session_id``；R12 迁移机器（``session_catalog_migration.py``）
  staging 目录名 = ``migration_id``。
  两者共享 ``sessions/.staging/`` 父目录，但各自只触碰自己的子目录，
  残留互不扫描、互不吸收；幂等 key 只用于 journal 查询与进程内 key lock。
- **CAS 失败定点回收**：publish CAS 失败后保留 preparing record 与物理目录；
  fork 调用方先释放真实 retention，再调用 ``abort_prepared`` 将唯一 stage/target
  隔离到 ``.boxteam/orphaned/session-creation/<session_id>/``。普通创建也显式
  调用同一 abort 入口，不静默改绑或遗失审计现场。
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
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.core.atomic_fs import (
    atomic_write_bytes as _atomic_write_bytes,
)
from app.core.atomic_fs import (
    fsync_directory as _fsync_directory,
)
from app.core.atomic_fs import (
    fsync_file as _fsync_file,
)
from app.core.key_lock_pool import KeyLockPool
from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    SessionCreationPurpose,
    SessionCreationRecord,
    validate_path_budget,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadGraphBindingRecord
from app.core.session_lifecycle_gate import NavigationTopologyGate
from app.core.sqlite_state import validate_sqlite_path_budget

__all__ = [
    "ForkCompletionProof",
    "SessionCreationResult",
    "SessionCreationService",
    "build_session_creation_manifest",
    "compute_session_creation_preimage_hash",
    "serialize_session_creation_manifest",
    "validate_session_metadata_keys",
]

# session.json 文件名（创建流自用常量）。
_SESSION_MANIFEST_NAME = "session.json"

# staging 区：sessions_root / ".staging" / <session_id>。
_STAGING_DIR_NAME = ".staging"

# CAS 失败定点回收隔离区：sessions_root.parent("orphaned") / "session-creation"
# / <session_id>（sessions_root.parent 即 workspace .boxteam/ 根）。
_ORPHANED_DIR_NAME = "orphaned"
_ORPHANED_CREATION_DIR_NAME = "session-creation"
_SESSION_ID_PATH_PLACEHOLDER = "ses_00000000000070008000000000000000"

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


def _validate_creation_purpose_metadata(
    purpose: SessionCreationPurpose,
    session_metadata: dict[str, object],
) -> None:
    """校验创建 purpose 与 session kind/source 的唯一对应合同。"""
    kind = session_metadata["kind"]
    context_source_session_id = session_metadata["context_source_session_id"]
    if purpose == "normal":
        valid = kind == "normal" and context_source_session_id is None
    elif purpose == "context_fork":
        valid = (
            kind == "context_fork"
            and isinstance(context_source_session_id, str)
            and bool(context_source_session_id)
        )
    else:
        raise ValueError(f"session creation purpose 非法: {purpose!r}")
    if not valid:
        raise ValueError(
            "session creation purpose 与 session_metadata 不匹配: "
            f"purpose={purpose!r}, kind={kind!r}, "
            f"context_source_session_id={context_source_session_id!r}"
        )


def compute_session_creation_preimage_hash(
    *,
    workspace_id: str,
    parent_node_id: str | None,
    title: str,
    title_source: str = "user",
    purpose: SessionCreationPurpose,
    graph_binding: ThreadGraphBindingRecord,
    session_metadata: dict[str, object],
    request_intent: dict[str, object] | None = None,
) -> str:
    """计算 preimage_hash = sha256(canonical JSON of preimage 四元组)。

    canonical 形式：``sort_keys=True`` + 紧凑分隔符 + UTF-8（本模块与测试
    共用，保证同 preimage 判定跨重入稳定；不追求跨实现 JCS 兼容）。
    ``session_metadata`` 值必须 JSON 可序列化（``datetime`` 等对象须先转
    文本，否则 ``json.dumps`` 抛 ``TypeError`` fail fast）。
    """
    if not isinstance(graph_binding, ThreadGraphBindingRecord):
        raise TypeError("graph_binding 必须是 ThreadGraphBindingRecord")
    payload = {
        "workspace_id": workspace_id,
        "parent_node_id": parent_node_id,
        "title": title,
        "title_source": title_source,
        "purpose": purpose,
        "graph_binding": asdict(graph_binding),
        "session_metadata": session_metadata,
    }
    # 创建控制意图只归 creation ledger/preimage，不展开到业务 session.json。
    if request_intent is not None:
        if not isinstance(request_intent, dict):
            raise TypeError("session creation request_intent 必须是对象")
        payload["request_intent"] = request_intent
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _serialize_session_creation_metadata(
    session_metadata: dict[str, object],
) -> str:
    """将已校验 metadata 以稳定 canonical JSON 冻结到 creation journal。"""
    validate_session_metadata_keys(session_metadata)
    return json.dumps(
        session_metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class ForkCompletionProof:
    """fork rollout journal 已为目标 Session durable committed 的窄证明。"""

    target_session_id: str
    fork_id: str
    materialization_id: str
    journal_state: Literal["committed"] = "committed"

    def __post_init__(self) -> None:
        for field_name in (
            "target_session_id",
            "fork_id",
            "materialization_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} 不能为空")
        if self.journal_state != "committed":
            raise ValueError(
                "fork completion journal_state 必须为 committed: "
                f"{self.journal_state!r}"
            )


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
    """以同一 creation journal 管理普通创建与 context fork 的准备和发布。

    ``prepare`` 冻结 purpose/metadata 并完整准备 staging；``publish_prepared``
    将 prepared 物理树 rename 到 record locator，再以 catalog CAS 发布。fork
    必须持有 committed journal 的 :class:`ForkCompletionProof` 才能发布。CAS
    失败保留 preparing record 和物理目录；调用方先释放 fork retention，再用
    ``abort_prepared`` 定点隔离并终结 record。
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
        self._key_locks = KeyLockPool()

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def prepare(
        self,
        *,
        idempotency_key: str,
        title: str,
        title_source: str = "user",
        parent_node_id: str | None,
        session_metadata: dict[str, object],
        graph_binding: ThreadGraphBindingRecord,
        purpose: SessionCreationPurpose = "normal",
        request_intent: dict[str, object] | None = None,
    ) -> SessionCreationRecord:
        """冻结创建 envelope 并准备物理目录，保留 Session 为不可见状态。

        ``title`` 写入 catalog ``display_name`` 与 preimage，**不写入**
        session.json；``session_metadata`` 为剥离 manifest 的调用方六字段
        （见 :func:`validate_session_metadata_keys`）。重复 key 只能以完全相同
        purpose 与 preimage 重入；已 published record 原样返回供 publish 幂等收敛。
        """
        self._validate_create_inputs(
            idempotency_key=idempotency_key,
            title=title,
            title_source=title_source,
            parent_node_id=parent_node_id,
            purpose=purpose,
            graph_binding=graph_binding,
            session_metadata=session_metadata,
        )
        session_metadata_json = _serialize_session_creation_metadata(
            session_metadata
        )
        preimage_hash = compute_session_creation_preimage_hash(
            workspace_id=self._workspace_id,
            parent_node_id=parent_node_id,
            title=title,
            title_source=title_source,
            purpose=purpose,
            graph_binding=graph_binding,
            session_metadata=session_metadata,
            request_intent=request_intent,
        )
        request_intent_json = (
            json.dumps(request_intent, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
            if request_intent is not None else None
        )
        # 进程内同 key 串行：并发同 key prepare 收敛到同一 record；gate 只包
        # 住两个 catalog 短临界区（锁序 gate → 单 SQLite 写事务）。
        async with self._key_lock(idempotency_key):
            async with self._gate.exclusive():
                record = self._store.create_or_get_creation_record(
                    idempotency_key=idempotency_key,
                    workspace_id=self._workspace_id,
                    parent_node_id=parent_node_id,
                    display_name=title,
                    title_source=title_source,
                    purpose=purpose,
                    graph_binding=graph_binding,
                    session_metadata_json=session_metadata_json,
                    preimage_hash=preimage_hash,
                    request_intent_json=request_intent_json,
                )
            if record.state == "published":
                return record
            if record.state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，调用方须换新 "
                    f"idempotency_key 重试: key={idempotency_key!r}, "
                    f"reason={record.abort_reason!r}"
                )
            frozen_metadata = self._session_metadata_from_record(record)
            self._verify_record_preimage(record, frozen_metadata)
            staging = self._staging_dir(record.session_id)
            target = self._date_bucket_dir(record.storage_relative_locator)
            if staging.exists() or staging.is_symlink():
                if target.exists() or target.is_symlink():
                    raise RuntimeError(
                        "prepared 日期桶与 staging 同时存在（外部改动，"
                        "fail closed）: "
                        f"key={idempotency_key!r}, target={target}, staging={staging}"
                    )
                prepared = staging
            elif target.exists() or target.is_symlink():
                prepared = target
            else:
                self._prepare_staging(record, frozen_metadata)
                prepared = staging
            self._verify_session_directory(
                prepared,
                record,
                frozen_metadata,
                stage="prepared Session 复验",
            )
            return record

    async def publish_prepared(
        self,
        record: SessionCreationRecord,
        *,
        fork_completion: ForkCompletionProof | None = None,
    ) -> SessionCreationResult:
        """仅在准备 envelope 与 fork proof 完整时 CAS 发布 Session。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError(
                f"record 必须是 SessionCreationRecord: {record!r}"
            )
        async with self._key_lock(record.session_creation_idempotency_key):
            current = self._require_record_capability(record)
            self._validate_fork_completion(current, fork_completion)
            if current.state == "published":
                self._session_metadata_from_record(current)
                return self._result_for_published(current)
            if current.state == "aborted":
                raise RuntimeError(
                    "session creation record 已中止，拒绝发布: "
                    f"key={current.session_creation_idempotency_key!r}, "
                    f"reason={current.abort_reason!r}"
                )
            metadata = self.read_prepared_session_metadata(current)
            prepared = self._prepared_directory(current)
            target = self._date_bucket_dir(current.storage_relative_locator)
            if prepared == self._staging_dir(current.session_id):
                self._rename_staging_to_target(current, metadata)
            elif prepared != target:
                raise RuntimeError(
                    "prepared Session path 与冻结 locator 不一致（fail closed）: "
                    f"session_id={current.session_id}, prepared={prepared}, "
                    f"target={target}"
                )
            try:
                async with self._gate.exclusive():
                    node = self._store.publish_creation_record(
                        current.session_creation_idempotency_key,
                        expected_purpose=current.purpose,
                    )
            except RuntimeError as error:
                raise RuntimeError(
                    "session creation publish CAS 失败；preparing record 与物理树"
                    "已保留，调用方必须先释放真实 retention，再显式 abort/quarantine: "
                    f"key={current.session_creation_idempotency_key!r}: {error}"
                ) from error
            return SessionCreationResult(
                session_id=current.session_id,
                main_thread_id=current.main_thread_id,
                storage_relative_locator=current.storage_relative_locator,
                node=node,
                record_state="published",
            )

    async def abort_prepared(
        self,
        record: SessionCreationRecord,
        reason: str,
    ) -> SessionCreationRecord:
        """先定点隔离 prepared 物理树，再把 preparing record 终结为 aborted。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError(
                f"record 必须是 SessionCreationRecord: {record!r}"
            )
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        async with self._key_lock(record.session_creation_idempotency_key):
            current = self._require_record_capability(record)
            if current.state == "aborted":
                return current
            if current.state == "published":
                raise RuntimeError(
                    "session creation record 已发布，不可 abort: "
                    f"key={current.session_creation_idempotency_key!r}"
                )
            self._quarantine_prepared(current)
            return self._store.abort_creation_record(
                current.session_creation_idempotency_key,
                reason,
            )

    def list_preparing_creation_records(
        self,
        *,
        purpose: SessionCreationPurpose,
    ) -> list[SessionCreationRecord]:
        """返回本 workspace 对应 purpose 的 durable preparing record。"""
        return self._store.list_preparing_creation_records(
            workspace_id=self._workspace_id,
            purpose=purpose,
        )

    def read_prepared_session_metadata(
        self,
        record: SessionCreationRecord,
    ) -> dict[str, object]:
        """从 record 精确 locator 对应的 stage/frozen manifest 恢复 metadata。"""
        current = self._require_record_capability(record)
        if current.state != "preparing":
            raise RuntimeError(
                "读取 prepared metadata 要求 preparing record: "
                f"session_id={current.session_id}, state={current.state}"
            )
        directory = self._prepared_directory(current)
        manifest_path = directory / _SESSION_MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "prepared session.json 无法读取（fail closed）: "
                f"path={manifest_path}: {error}"
            ) from error
        if not isinstance(manifest, dict):
            raise RuntimeError(  # noqa: TRY004 - 磁盘数据损坏属于语义冲突。
                f"prepared session.json 必须是 JSON 对象: {manifest_path}"
            )
        try:
            metadata = {
                key: manifest[key]
                for key in _METADATA_CALLER_KEYS
            }
        except KeyError as error:
            raise RuntimeError(
                "prepared session.json 缺少冻结 metadata 字段（fail closed）: "
                f"path={manifest_path}, field={error.args[0]!r}"
            ) from error
        validate_session_metadata_keys(metadata)
        try:
            _validate_creation_purpose_metadata(current.purpose, metadata)
        except ValueError as error:
            raise RuntimeError(
                "prepared purpose 与 session_metadata 不匹配（fail closed）: "
                f"session_id={current.session_id}"
            ) from error
        if _serialize_session_creation_metadata(metadata) != current.session_metadata_json:
            raise RuntimeError(
                "prepared session.json metadata 与 durable creation envelope 不一致"
                f"（fail closed）: session_id={current.session_id}"
            )
        self._verify_record_preimage(current, metadata)
        self._verify_session_directory(
            directory,
            current,
            metadata,
            stage="prepared metadata 恢复",
        )
        return metadata

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    def _validate_create_inputs(
        self,
        *,
        idempotency_key: str,
        title: str,
        title_source: str,
        parent_node_id: str | None,
        purpose: SessionCreationPurpose,
        graph_binding: ThreadGraphBindingRecord,
        session_metadata: dict[str, object],
    ) -> None:
        """prepare 入参校验（在任何状态变更之前 fail fast）。"""
        if not isinstance(idempotency_key, str):
            raise TypeError(
                f"idempotency_key 必须是字符串: {idempotency_key!r}"
            )
        if not idempotency_key:
            raise ValueError("idempotency_key 不能为空")
        if not isinstance(title, str) or not title:
            raise ValueError(f"title 不能为空: {title!r}")
        if title_source not in ("default", "user", "auto"):
            raise ValueError(f"title_source 非法: {title_source!r}")
        if purpose not in ("normal", "context_fork"):
            raise ValueError(f"session creation purpose 非法: {purpose!r}")
        if parent_node_id is not None and not isinstance(parent_node_id, str):
            raise TypeError(
                f"parent_node_id 必须是字符串或 None: {parent_node_id!r}"
            )
        validate_session_metadata_keys(session_metadata)
        _validate_creation_purpose_metadata(purpose, session_metadata)
        # preimage 计算兼作 JSON 可序列化校验（不可序列化值 fail fast）。
        compute_session_creation_preimage_hash(
            workspace_id=self._workspace_id,
            parent_node_id=parent_node_id,
            title=title,
            title_source=title_source,
            purpose=purpose,
            graph_binding=graph_binding,
            session_metadata=session_metadata,
        )
        self._validate_filesystem_budget()

    def _require_record_capability(
        self,
        record: SessionCreationRecord,
    ) -> SessionCreationRecord:
        """校验传入 record 与同 workspace durable row 的冻结字段完全相同。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError(
                f"record 必须是 SessionCreationRecord: {record!r}"
            )
        current = self._store.get_creation_record(
            record.session_creation_idempotency_key
        )
        immutable_fields = (
            "session_creation_idempotency_key",
            "session_id",
            "main_thread_id",
            "workspace_id",
            "parent_node_id",
            "display_name",
            "title_source",
            "purpose",
            "graph_id",
            "graph_revision",
            "graph_schema_hash",
            "capability_profile_hash",
            "session_metadata_json",
            "request_intent_json",
            "created_at",
            "storage_relative_locator",
            "preimage_hash",
            "parent_revision",
            "record_created_at",
        )
        if any(
            getattr(record, field) != getattr(current, field)
            for field in immutable_fields
        ):
            raise RuntimeError(
                "SessionCreationRecord capability 与 durable row 不一致，"
                f"拒绝使用: key={record.session_creation_idempotency_key!r}"
            )
        if current.workspace_id != self._workspace_id:
            raise RuntimeError(
                "SessionCreationRecord workspace 不属于当前 service: "
                f"record={current.workspace_id!r}, service={self._workspace_id!r}"
            )
        if current.purpose not in ("normal", "context_fork"):
            raise RuntimeError(
                "durable creation purpose 非法（record 被外部改动，fail closed）: "
                f"purpose={current.purpose!r}"
            )
        return current

    @staticmethod
    def _session_metadata_from_record(
        record: SessionCreationRecord,
    ) -> dict[str, object]:
        try:
            metadata = json.loads(record.session_metadata_json)
        except ValueError as error:
            raise RuntimeError(
                "durable creation metadata 无法解析（record 被外部改动，"
                f"fail closed）: key={record.session_creation_idempotency_key!r}"
            ) from error
        if not isinstance(metadata, dict):
            raise RuntimeError(  # noqa: TRY004 - journal 数据损坏属于语义冲突。
                "durable creation metadata 必须是 JSON 对象（fail closed）: "
                f"key={record.session_creation_idempotency_key!r}"
            )
        validate_session_metadata_keys(metadata)
        try:
            _validate_creation_purpose_metadata(record.purpose, metadata)
        except ValueError as error:
            raise RuntimeError(
                "durable creation purpose 与 session_metadata 不匹配 "
                "（fail closed）: "
                f"key={record.session_creation_idempotency_key!r}"
            ) from error
        if _serialize_session_creation_metadata(metadata) != record.session_metadata_json:
            raise RuntimeError(
                "durable creation metadata 不是 canonical JSON（fail closed）: "
                f"key={record.session_creation_idempotency_key!r}"
            )
        return metadata

    @staticmethod
    def _verify_record_preimage(
        record: SessionCreationRecord,
        metadata: dict[str, object],
    ) -> None:
        actual = compute_session_creation_preimage_hash(
            workspace_id=record.workspace_id,
            parent_node_id=record.parent_node_id,
            title=record.display_name,
            title_source=record.title_source,
            purpose=record.purpose,
            graph_binding=record.graph_binding,
            session_metadata=metadata,
            request_intent=(
                json.loads(record.request_intent_json)
                if record.request_intent_json is not None else None
            ),
        )
        if actual != record.preimage_hash:
            raise RuntimeError(
                "durable creation preimage 与 envelope 不一致（record 被外部改动，"
                f"fail closed）: key={record.session_creation_idempotency_key!r}"
            )

    def _prepared_directory(self, record: SessionCreationRecord) -> Path:
        staging = self._staging_dir(record.session_id)
        target = self._date_bucket_dir(record.storage_relative_locator)
        staging_exists = staging.exists() or staging.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if staging_exists and target_exists:
            raise RuntimeError(
                "prepared 日期桶与 staging 同时存在（外部改动，fail closed）: "
                f"session_id={record.session_id}, target={target}, staging={staging}"
            )
        if not staging_exists and not target_exists:
            raise RuntimeError(
                "durable preparing record 对应的物理 Session 缺失: "
                f"session_id={record.session_id}, target={target}, staging={staging}"
            )
        directory = staging if staging_exists else target
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError(
                "prepared Session 不是普通目录（fail closed）: "
                f"session_id={record.session_id}, path={directory}"
            )
        return directory

    @staticmethod
    def _validate_fork_completion(
        record: SessionCreationRecord,
        proof: ForkCompletionProof | None,
    ) -> None:
        if record.purpose == "normal":
            if proof is not None:
                raise ValueError("普通 Session creation 不接受 fork completion proof")
            return
        if proof is None:
            raise ValueError("context_fork publish 必须提供 committed completion proof")
        if not isinstance(proof, ForkCompletionProof):
            raise TypeError(f"fork_completion 类型非法: {proof!r}")
        if proof.target_session_id != record.session_id:
            raise RuntimeError(
                "fork completion target 与 prepared Session 不一致: "
                f"proof={proof.target_session_id}, record={record.session_id}"
            )

    def _key_lock(self, idempotency_key: str) -> asyncio.Lock:
        """按 key 取进程内串行锁（固定分片，同 key 恒命中同一把）。"""
        return self._key_locks.lock_for(idempotency_key)

    # ------------------------------------------------------------------
    # 路径定位
    # ------------------------------------------------------------------

    def _staging_dir(self, session_id: str) -> Path:
        return self._sessions_root / _STAGING_DIR_NAME / session_id

    def _validate_filesystem_budget(self) -> None:
        """在 creation record 前校验固定形态目录与 SQLite 主库/sidecar。"""
        session_id = _SESSION_ID_PATH_PLACEHOLDER
        relative_directories = (
            (self._sessions_root, f"{_STAGING_DIR_NAME}/{session_id}"),
            (self._sessions_root, f"1970/01/01/{session_id}"),
            (
                self._sessions_root.parent,
                f"{_ORPHANED_DIR_NAME}/{_ORPHANED_CREATION_DIR_NAME}/{session_id}",
            ),
        )
        for base, relative_directory in relative_directories:
            relative_database = f"{relative_directory}/{CONTROL_DATABASE_NAME}"
            for suffix in ("", "-journal", "-wal", "-shm"):
                validate_path_budget(base, f"{relative_database}{suffix}")
            validate_sqlite_path_budget(base / relative_database)

        rollout_database = f"1970/01/01/{session_id}/rollout/index.sqlite"
        for suffix in ("", "-journal", "-wal", "-shm"):
            validate_path_budget(
                self._sessions_root, f"{rollout_database}{suffix}"
            )
        validate_sqlite_path_budget(self._sessions_root / rollout_database)

    def _date_bucket_dir(self, storage_relative_locator: str) -> Path:
        relative = storage_relative_locator[len("sessions/"):]
        return self._sessions_root / relative

    def _orphaned_dir(self, session_id: str) -> Path:
        return (
            self._sessions_root.parent
            / _ORPHANED_DIR_NAME
            / _ORPHANED_CREATION_DIR_NAME
            / session_id
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
    ) -> None:
        """staging 已存在 → 逐文件校验复用；不存在 → 完整准备 + barrier。"""
        staging = self._staging_dir(record.session_id)
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
        threads_root = staging / "threads"
        threads_root.mkdir()
        _fsync_directory(threads_root)
        # durability barrier：session.json、控制库与 child-thread 根目录均已
        # 持久化；此处补 staging 目录与 .staging 父目录 fsync。
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
        control_path = staging / CONTROL_DATABASE_NAME
        store = SessionControlStore(control_path)
        try:
            store.initialize_main_thread(
                record.main_thread_id,
                datetime.fromisoformat(record.created_at),
                graph_binding=record.graph_binding,
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
        required_entries = {
            _SESSION_MANIFEST_NAME,
            CONTROL_DATABASE_NAME,
            "threads",
        }
        allowed_entries = (
            required_entries | {"rollout", ".rollout.write.lock", "debug"}
            if record.purpose == "context_fork"
            else required_entries
        )
        if not required_entries.issubset(entries) or set(entries) - allowed_entries:
            raise RuntimeError(
                f"{stage}: 条目集与预期不一致（外部改动或上次准备中断，"
                f"fail closed）: path={directory}, "
                f"required={sorted(required_entries)}, actual={entries}"
            )
        rollout_lock = directory / ".rollout.write.lock"
        if rollout_lock.is_symlink() or (rollout_lock.exists() and not rollout_lock.is_file()):
            raise RuntimeError(f"{stage}: rollout lock 必须是普通文件: {rollout_lock}")
        debug_root = directory / "debug"
        if debug_root.is_symlink() or (debug_root.exists() and not debug_root.is_dir()):
            raise RuntimeError(f"{stage}: debug 必须是普通目录: {debug_root}")

        threads_root = directory / "threads"
        if threads_root.is_symlink() or not threads_root.is_dir():
            raise RuntimeError(
                f"{stage}: threads 必须是普通目录: "
                f"path={threads_root}"
            )
        thread_entries = sorted(entry.name for entry in threads_root.iterdir())
        if thread_entries:
            raise RuntimeError(
                f"{stage}: prepared Session 不得包含 child thread: "
                f"path={threads_root}, actual={thread_entries}"
            )
        rollout_root = directory / "rollout"
        if rollout_root.exists() or rollout_root.is_symlink():
            if rollout_root.is_symlink() or not rollout_root.is_dir():
                raise RuntimeError(
                    f"{stage}: prepared Session rollout 必须是普通目录: "
                    f"path={rollout_root}"
                )
            if record.purpose != "context_fork":
                raise RuntimeError(
                    f"{stage}: normal Session 不得包含 rollout: path={rollout_root}"
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
        control_path = session_dir / CONTROL_DATABASE_NAME
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
                "SELECT thread_id, graph_id, graph_revision, graph_schema_hash, "
                "capability_profile_hash FROM thread_catalog WHERE kind = 'main'"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != record.main_thread_id:
                raise RuntimeError(
                    f"{stage}: session-control main row 与 record 不一致: "
                    f"path={control_path}, "
                    f"rows={[str(row[0]) for row in rows]}, "
                    f"expected={record.main_thread_id!r}"
                )
            if tuple(rows[0][1:]) != record.graph_binding.as_tuple():
                raise RuntimeError(
                    f"{stage}: session-control GraphBinding 与冻结 record 不一致: "
                    f"path={control_path}, actual={tuple(rows[0][1:])!r}, "
                    f"expected={record.graph_binding.as_tuple()!r}"
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
            "title_source": record.title_source,
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
            "title_source": node.title_source,
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
    ) -> None:
        """staging 原子 rename 到冻结日期桶（locator 来自 record）。"""
        stage = "rename 到日期桶"
        staging = self._staging_dir(record.session_id)
        if not staging.is_dir() or staging.is_symlink():
            raise RuntimeError(
                f"{stage}: staging 缺失或不是目录: {staging}"
            )
        target = self._date_bucket_dir(record.storage_relative_locator)
        if target.exists() or target.is_symlink():
            raise RuntimeError(
                f"{stage}: 目标日期桶已存在，拒绝覆盖或丢弃 staging: "
                f"target={target}, staging={staging}"
            )
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

    def _quarantine_prepared(
        self,
        record: SessionCreationRecord,
    ) -> None:
        """把唯一 prepared stage/target 定点移入 orphaned/session-creation。"""
        stage = "prepared creation 隔离"
        staging = self._staging_dir(record.session_id)
        target = self._date_bucket_dir(record.storage_relative_locator)
        staging_exists = staging.exists() or staging.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if staging_exists and target_exists:
            raise RuntimeError(
                f"{stage}: staging 与冻结日期桶同时存在，拒绝隔离: "
                f"staging={staging}, target={target}"
            )
        if not staging_exists and not target_exists:
            return
        source = staging if staging_exists else target
        if not source.is_dir() or source.is_symlink():
            raise RuntimeError(
                f"{stage}: prepared 目录缺失或不是普通目录: {source}"
            )
        quarantine_target = self._orphaned_dir(record.session_id)
        if quarantine_target.exists() or quarantine_target.is_symlink():
            raise RuntimeError(
                f"{stage}: 隔离目标已存在，拒绝覆盖: {quarantine_target}"
            )
        quarantine_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, quarantine_target)
        except OSError as error:
            raise RuntimeError(
                f"{stage}: prepared 目录隔离失败: {source} -> "
                f"{quarantine_target}: {error}"
            ) from error
        _fsync_directory(quarantine_target.parent)
        _fsync_directory(source.parent)
