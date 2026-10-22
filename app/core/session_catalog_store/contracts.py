"""session-catalog 的不可变投影 DTO、fail-closed 错误类与形态 helper。

承载 nodes 行投影与创建/删除/fork claim journal 的强类型记录、记录内
JSON 槽的解析 helper（外部改动一律 fail closed）、“只能进入维护模式”的
错误合同，以及 apply_navigation_mutation 的 “不改父” 哨兵 ``_UNSET``。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.core.session_control_store.contracts import ThreadGraphBindingRecord

from app.core.session_catalog_store.validators import (
    validate_session_id,
    validate_storage_relative_locator,
)

SessionKind = Literal["normal", "context_fork"]
SessionCreationPurpose = SessionKind


class SessionGenerationOriginDTO(BaseModel):
    """nodes 与 API 共用的不可变生成来源合同，不依赖业务 schema 聚合出口。"""

    model_config = ConfigDict(extra="forbid")

    generator_id: str
    run_id: str
    idempotency_key: str
    generator_type_id: str
    generator_type_version: str


@dataclass(frozen=True, slots=True)
class SessionCatalogNode:
    """nodes 表行的不可变投影。

    ``revision`` 是本节点的整数版本，重命名、移动等节点写入递增，并用于
    ``expected_revision`` CAS；API 对应 ``node_revision``。它不是目录读 DTO
    的字符串 SHA256 内容摘要 ``revision``，也不是整体 ``catalog_revision``。
    """

    node_id: str
    kind: str
    parent_node_id: str | None
    display_name: str
    title_source: str | None
    state: str
    revision: int
    workspace_id: str
    created_at: str | None
    storage_relative_locator: str | None
    main_thread_id: str | None
    # SessionDTO 业务字段只归 nodes；folder 对应槽全部为空。
    session_kind: str | None
    current_agent_id: str | None
    current_provider_id: str | None
    context_source_session_id: str | None
    generation_origin_json: str | None
    updated_at: str | None


def fork_source_capture_relative_locator(snapshot_id: str) -> str:
    """私有存储坐标只在本地由冻结快照身份定位，不进入跨 owner 合同。"""
    if len(snapshot_id) != 32 or any(char not in "0123456789abcdef" for char in snapshot_id):
        raise RuntimeError("fork capture 内部 snapshot identity 非法")
    return f"fork-source-captures/{snapshot_id}"


@dataclass(frozen=True, slots=True)
class ForkSourceCaptureRecord:
    """创建 journal 预分配的 source capture；私有 locator 由存储 owner 核验。"""

    copy_operation_id: str
    creation_key: str | None
    source_session_id: str
    source_thread_id: str
    target_session_id: str
    target_thread_id: str
    # 创建 journal 冻结的 fork capture 内部快照身份；跨端由目标分配、来源 export 原样复用，不是 Session/Thread ID。
    snapshot_id: str
    # fork-source-captures/{snapshot_id} 内部 locator，普通会话 resolver 不可见。
    snapshot_relative_locator: str
    fork_id: str
    materialization_id: str
    request_json: str
    preimage_hash: str
    state: str
    snapshot_sha256: str | None
    marker_sha256: str | None
    abort_reason: str | None


@dataclass(frozen=True, slots=True)
class SessionCreationRecord:
    """session_creation_records 表行的不可变投影（8.1-A 创建流 journal）。

    ``state`` 闭集为 ``preparing/published/aborted``；``parent_revision``
    是 record 建立时冻结的父节点 revision（publish 时 CAS 校验，漂移即
    失败）；``created_at`` 是冻结的 Session UTC 创建时刻（ISO 文本），与
    ``record_created_at/record_updated_at``（journal 自身记账时刻）不同。
    """

    session_creation_idempotency_key: str
    session_id: str
    main_thread_id: str
    workspace_id: str
    parent_node_id: str | None
    display_name: str
    title_source: str
    purpose: SessionCreationPurpose
    graph_id: str
    graph_revision: int
    graph_schema_hash: str
    capability_profile_hash: str
    session_metadata_json: str
    request_intent_json: str | None
    created_at: str
    storage_relative_locator: str
    preimage_hash: str
    parent_revision: int | None
    state: str
    abort_reason: str | None
    record_created_at: str
    record_updated_at: str

    # 与创建意图同事务冻结的传输 Trace，不属于创建或 capture 的业务 preimage。
    fork_delivery_request_id: str | None = None

    @property
    def graph_binding(self) -> ThreadGraphBindingRecord:
        """从创建记录冻结的四字段取得 selector，不读取当前图注册表。"""
        from app.core.session_control_store.contracts import ThreadGraphBindingRecord

        return ThreadGraphBindingRecord(
            self.graph_id, self.graph_revision,
            self.graph_schema_hash, self.capability_profile_hash,
        )


@dataclass(frozen=True, slots=True)
class SubtreeFrozenNode:
    """``subtree_delete_records.frozen_node_ids`` JSON 数组元素的强类型投影。

    ``revision`` 是 record 建立时冻结的节点 revision（mark CAS 校验漂移的
    期望值来源，对齐 design.md「冻结……父子revision」）。
    """

    node_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class SubtreeDeleteRecord:
    """subtree_delete_records 表行的不可变投影（8.1-B 子树删除流 journal）。

    ``state`` 闭集为 ``preparing/deleting/draining/completed/aborted``；
    ``frozen_node_ids`` 字段保存 SubtreeFrozenNode 元组，不是纯 ID 列表；
    它是递归 CTE 冻结的 (node_id, revision) 集合（含
    root，按 node_id 排序）；``frozen_session_locators`` 是
    session_id → 不可变 storage locator 映射（恢复时按此定点继续，不重查
    当前树）；``drained_session_ids`` 是已物理隔离 session 的追加序数组。
    """

    subtree_delete_idempotency_key: str
    workspace_id: str
    root_node_id: str
    frozen_node_ids: tuple[SubtreeFrozenNode, ...]
    frozen_session_locators: dict[str, str]
    state: str
    abort_reason: str | None
    record_created_at: str
    record_updated_at: str
    drained_session_ids: tuple[str, ...]


CatalogTransactionHook = Callable[[sqlite3.Connection, SubtreeDeleteRecord], None]


def _validate_workspace_id(workspace_id: object) -> None:
    """校验 workspace_id：非空字符串。"""
    if not isinstance(workspace_id, str):
        raise TypeError(f"workspace_id 必须是字符串: {workspace_id!r}")
    if not workspace_id:
        raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")


def _parse_frozen_node_ids(raw: str) -> tuple[SubtreeFrozenNode, ...]:
    """解析 frozen_node_ids JSON 数组；结构非法即 fail closed（外部改动）。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record frozen_node_ids 无法解析（record 被外部改动，"
            f"fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record frozen_node_ids 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    items: list[SubtreeFrozenNode] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"node_id", "revision"}:
            raise RuntimeError(
                "subtree delete record frozen_node_ids 元素结构非法"
                f"（record 被外部改动，fail closed）: {entry!r}"
            )
        node_id = entry["node_id"]
        revision = entry["revision"]
        try:
            validate_session_id(node_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record 冻结 node_id 非法"
                f"（record 被外部改动，fail closed）: {node_id!r}: {error}"
            ) from error
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise RuntimeError(
                "subtree delete record 冻结 revision 非法"
                f"（record 被外部改动，fail closed）: {revision!r}"
            )
        if node_id in seen:
            raise RuntimeError(
                "subtree delete record 冻结集合包含重复 node_id"
                f"（record 被外部改动，fail closed）: {node_id!r}"
            )
        seen.add(node_id)
        items.append(SubtreeFrozenNode(node_id=node_id, revision=revision))
    return tuple(items)


def _parse_frozen_session_locators(raw: str) -> dict[str, str]:
    """解析 frozen_session_locators JSON 数组；结构非法即 fail closed。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record frozen_session_locators 无法解析"
            f"（record 被外部改动，fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record frozen_session_locators 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    locators: dict[str, str] = {}
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {
            "session_id",
            "storage_relative_locator",
        }:
            raise RuntimeError(
                "subtree delete record frozen_session_locators 元素结构非法"
                f"（record 被外部改动，fail closed）: {entry!r}"
            )
        session_id = entry["session_id"]
        locator = entry["storage_relative_locator"]
        try:
            validate_session_id(session_id)
            validate_storage_relative_locator(locator)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record 冻结 session locator 非法"
                f"（record 被外部改动，fail closed）: {entry!r}: {error}"
            ) from error
        if session_id in locators:
            raise RuntimeError(
                "subtree delete record 冻结 locator 集合包含重复 session_id"
                f"（record 被外部改动，fail closed）: {session_id!r}"
            )
        locators[session_id] = locator
    return locators


def _parse_drained_session_ids(raw: str) -> tuple[str, ...]:
    """解析 drained_session_ids JSON 数组；结构非法即 fail closed。"""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(
            "subtree delete record drained_session_ids 无法解析"
            f"（record 被外部改动，fail closed）: {raw!r}: {error}"
        ) from error
    if not isinstance(payload, list):
        # 结构被篡改属「外部改动 fail closed」语义冲突，按模块错误分类抛
        # RuntimeError 而非调用方输入类型错误（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "subtree delete record drained_session_ids 必须是 JSON 数组"
            f"（record 被外部改动，fail closed）: {raw!r}"
        )
    seen: set[str] = set()
    for session_id in payload:
        try:
            validate_session_id(session_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "subtree delete record drained session_id 非法"
                f"（record 被外部改动，fail closed）: {session_id!r}: {error}"
            ) from error
        if session_id in seen:
            raise RuntimeError(
                "subtree delete record drained_session_ids 包含重复 session_id"
                f"（record 被外部改动，fail closed）: {session_id!r}"
            )
        seen.add(session_id)
    return tuple(str(session_id) for session_id in payload)


class CatalogMaintenanceRequiredError(RuntimeError):
    """catalog 损坏、备份落后或存在未登记日期目录：只能进入维护模式核对（8.1-F）。

    这是 fail-closed 的单一错误合同：绝不允许扫盘补 active node、回退旧 JSON
    或把未知目录交给 GC。调用方收到本错误必须停止自动恢复，保留原数据，交由
    operator 从可校验备份恢复或人工核对。
    """


class SubtreeDeleteMarkRejectedError(RuntimeError):
    """mark 的业务前置条件拒绝，事务未提交任何导航状态变更。"""


class SourceRetainedByForkError(SubtreeDeleteMarkRejectedError):
    """整树删除提交前发现 source Session 被 **active** pinned fork claim 保留。

    删除必须在提交 catalog deleting 之前返回具体 blocker（哪个 Session、哪个
    claim），且整棵子树保持 active。本错误不重试；调用方须先释放对应 pinned
    fork（删除 target）或换新 key。
    """

    def __init__(
        self,
        *,
        source_session_id: str,
        claim_id: str,
        target_session_id: str,
    ) -> None:
        self.source_session_id = source_session_id
        self.claim_id = claim_id
        self.target_session_id = target_session_id
        super().__init__(
            "source_retained_by_fork: Session 被 active pinned fork 保留，"
            "拒绝提交整树 catalog deleting（整棵子树保持 active）: "
            f"source_session_id={source_session_id}, "
            f"claim_id={claim_id}, target_session_id={target_session_id}"
        )


class SourceRetentionOperationPendingError(SubtreeDeleteMarkRejectedError):
    """整树删除提交前发现 source Session 存在 **preparing** pinned fork claim。

    preparing claim 无墙钟过期；删除必须 fail closed 并要求显式 recovery，不能
    猜测为 stale 后释放。``SourceRetainedByForkError`` 的对应准备期变体。
    """

    def __init__(
        self,
        *,
        source_session_id: str,
        claim_id: str,
        target_session_id: str,
    ) -> None:
        self.source_session_id = source_session_id
        self.claim_id = claim_id
        self.target_session_id = target_session_id
        super().__init__(
            "source_retention_operation_pending: Session 存在 preparing pinned "
            "fork claim，拒绝提交整树 catalog deleting（整棵子树保持 active，"
            "须显式 recovery）: "
            f"source_session_id={source_session_id}, "
            f"claim_id={claim_id}, target_session_id={target_session_id}"
        )


@dataclass(frozen=True, slots=True)
class ForkRetentionClaim:
    """fork_retention_claims 表行的不可变投影（8.1-D pinned retention 占位）。

    ``state`` 闭集为 ``preparing/active/released``：``preparing`` 在 source
    capture 前建立、无墙钟过期；target ``target_committed`` 后 CAS 为
    ``active``；abort/target 删除经 ``released`` 终结。``claim_id`` 即 fork_id
    （uuid hex）；``source_lifecycle_generation`` 冻结准入时的 source fence
    generation，激活时 CAS 校验漂移。
    """

    fork_retention_claim_id: str
    workspace_id: str
    source_session_id: str
    target_session_id: str
    source_lifecycle_generation: int
    state: str
    release_reason: str | None
    record_created_at: str
    record_updated_at: str


@dataclass(frozen=True, slots=True)
class CatalogBackupManifest:
    """一次 SQLite online backup 的完整性清单（8.1-F generation/checksum）。

    ``generation`` 是备份时刻 catalog 的单调 generation；``checksum`` 是备份
    DB 文件的 sha256。恢复/启动核对时 generation 落后于当前值即「备份落后于
    已提交操作」，只能进入维护模式。
    """

    generation: int
    checksum: str
    database_path: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CatalogIntegrityReport:
    """catalog 启动/备份核对的结果投影（8.1-F）。"""

    generation: int
    quick_check: str
    backup_generation: int | None
    backup_checksum: str | None


# ``apply_navigation_mutation`` 区分「不改父」与「显式移到根(None)」的哨兵。
_UNSET: object = object()
