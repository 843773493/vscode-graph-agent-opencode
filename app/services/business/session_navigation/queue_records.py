"""会话目录队列的不可变行投影、跨进程幂等 preimage 与 catalog revision 读取。

本模块是「记录模型」这一条垂直链的唯一承载点：

- ``NavigationMutationRecord`` / ``NavigationEventRecord`` 精确对应两张旁挂表的行形态；
- ``_record_from_row`` / ``_event_from_row`` 是两个投影的唯一构造点；前者在
  投影前按原 typed intent 重验冻结参数、镜像身份与 preimage，调用方统一经由
  ``NavigationMutationQueueStore`` 上的同名静态方法入口使用；
- ``compute_intent_preimage_hash`` 是跨进程幂等哈希，字段集与字节序是冲突判定
  的判据，不得改动；``_params_json`` 冻结 receipt 可见参数；

``read_catalog_revision`` 留在 ``queue_store.py`` 门面（其唯一调用方是
``executor.py`` 与 ``operations_service.py``），本模块只承载记录模型本身。

错误分类约定（沿用 ``session_catalog_store``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义冲突/
外部改动 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    AutoSessionTitleCommand,
    NavigationDeleteSettlement,
    NavigationEventDTO,
    NavigationMutationIntentDTO,
    NavigationQueueCommand,
    validate_operation_id,
)

__all__ = [
    "NAVIGATION_BACKPRESSURE_PENDING_LIMIT",
    "NavigationBackpressureError",
    "NavigationEventRecord",
    "NavigationMutationConflictError",
    "NavigationMutationRecord",
    "NavigationQueueOwner",
    "NavigationQueueOwnerError",
    "compute_intent_preimage_hash",
]

# 单 workspace 未终态 operation 上限：超过即返回明确可重试的背压，绝不静默丢命令。
NAVIGATION_BACKPRESSURE_PENDING_LIMIT = 1000

_RECORD_COLUMNS = (
    "gateway_id, workspace_id, actor, operation_id, client_sequence, queue_seq, "
    "kind, state, params_json, preimage_hash, depends_on_json, "
    "created_by_operation_id, target_node_id, reserved_node_id, result_node_id, "
    "result_node_revision, committed_catalog_revision, error_code, error_detail, "
    "pending_settlement, "
    "holder_id, fencing_token, receipt_revision, created_at, updated_at"
)

_EVENT_COLUMNS = (
    "workspace_id, event_seq, operation_id, gateway_id, actor, queue_seq, kind, "
    "result_state, committed_catalog_revision, affected_node_ids_json, "
    "error_code, error_detail, created_at"
)

_OWNER_COLUMNS = "workspace_id, owner_id, owner_generation, updated_at"


class NavigationBackpressureError(RuntimeError):
    """workspace 未终态 operation 超限：明确可重试，客户端须保留 outbox。"""


class NavigationMutationConflictError(RuntimeError):
    """同 ``(gateway, workspace, actor, operation_id)`` 但 preimage 不同。"""


class NavigationQueueOwnerError(RuntimeError):
    """queue owner generation 失效或缺少必需的 owner。"""


@dataclass(frozen=True, slots=True)
class NavigationMutationRecord:
    """``navigation_mutation_records`` 行的不可变投影。"""

    gateway_id: str
    workspace_id: str
    actor: str
    operation_id: str
    client_sequence: int
    queue_seq: int
    kind: str
    state: str
    params: dict[str, object]
    preimage_hash: str
    depends_on: tuple[str, ...]
    created_by_operation_id: str | None
    target_node_id: str | None
    reserved_node_id: str | None
    result_node_id: str | None
    result_node_revision: int | None
    committed_catalog_revision: int | None
    error_code: str | None
    error_detail: str | None
    pending_settlement: bool
    holder_id: str | None
    fencing_token: int
    receipt_revision: int
    created_at: str
    updated_at: str

    @property
    def settlement(self) -> NavigationDeleteSettlement | None:
        """既有 receipt 行的唯一结算投影，不创建第二份删除状态。"""
        if self.kind not in ("delete_folder", "delete_session") or self.state != "committed":
            return None
        if not self.pending_settlement:
            return "settled"
        return "repair_required" if self.error_code == "delete_settlement_repair_required" else "pending"

    @property
    def is_terminal(self) -> bool:
        return self.state in NAVIGATION_MUTATION_TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class NavigationEventRecord:
    """``navigation_events`` 行的不可变投影。"""

    workspace_id: str
    event_seq: int
    operation_id: str
    gateway_id: str
    actor: str
    queue_seq: int
    kind: str
    result_state: str
    committed_catalog_revision: int | None
    affected_node_ids: tuple[str, ...]
    error_code: str | None
    error_detail: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class NavigationQueueOwner:
    """同一 workspace queue worker 的 durable generation 身份。"""

    workspace_id: str
    owner_id: str
    generation: int

    @property
    def holder_id(self) -> str:
        """嵌入 operation receipt 的 holder identity，随 generation 单调变化。"""
        return f"{self.owner_id}:{self.generation}"


def compute_intent_preimage_hash(intent: NavigationQueueCommand) -> str:
    """规范化 intent 参数并计算 preimage hash（同 key 异 preimage 冲突的判据）。

    只纳入会影响业务结果的规范化字段；``client_operation_id`` 本身与
    ``client_sequence``、``base_catalog_revision`` 不参与 preimage（重试时
    客户端可能给出新的快照修订，但意图未变）。
    """
    if isinstance(intent, AutoSessionTitleCommand):
        payload = {
            "kind": intent.kind,
            "target_node_id": intent.target_node_id,
            "name": intent.name,
            "expected_revision": intent.expected_revision,
            "expected_title": intent.expected_title,
            "expected_title_source": intent.expected_title_source,
        }
    else:
        payload = {
            "kind": intent.kind,
            "target_node_id": intent.target_node_id,
            "created_by_operation_id": intent.created_by_operation_id,
            "name": intent.name,
            "parent_node_id": intent.parent_node_id,
            "recursive": intent.recursive,
            "depends_on": sorted(intent.depends_on),
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _params_json(intent: NavigationQueueCommand) -> str:
    params: dict[str, object] = {
        "expected_revision": intent.expected_revision,
        "base_catalog_revision": intent.base_catalog_revision,
        "target_node_id": intent.target_node_id,
        "created_by_operation_id": intent.created_by_operation_id,
        "name": intent.name,
        "parent_node_id": intent.parent_node_id,
        "recursive": intent.recursive,
    }
    if isinstance(intent, AutoSessionTitleCommand):
        params["expected_title"] = intent.expected_title
        params["expected_title_source"] = intent.expected_title_source
    return json.dumps(
        params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_record_metadata(row: sqlite3.Row) -> None:
    """恢复与正常读取共用原始控制字段合同，不截断序号或把任意值转成真。"""
    for name, minimum, nullable in (
        ("queue_seq", 1, False), ("fencing_token", 0, False),
        ("receipt_revision", 1, False), ("result_node_revision", 1, True),
        ("committed_catalog_revision", 0, True),
    ):
        value = row[name]
        if value is None and nullable:
            continue
        if type(value) is not int or value < minimum:
            raise ValueError(f"navigation operation {name}必须是至少{minimum}的整数")
    if type(row["pending_settlement"]) is not int or row["pending_settlement"] not in (0, 1):
        raise ValueError("navigation operation pending_settlement必须是0或1")
    if row["state"] not in ("queued", "running", *NAVIGATION_MUTATION_TERMINAL_STATES):
        raise ValueError("navigation operation state不在状态闭集中")
    for name in ("reserved_node_id", "result_node_id", "holder_id"):
        value = row[name]
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"navigation operation {name}必须为null或非空字符串")
    for name in ("error_code", "error_detail"):
        if row[name] is not None and not isinstance(row[name], str):
            raise TypeError(f"navigation operation {name}必须为null或字符串")
    for name in ("created_at", "updated_at"):
        if datetime.fromisoformat(row[name]).tzinfo is None:
            raise ValueError(f"navigation operation {name}必须带时区")
    if row["pending_settlement"] and (
        row["kind"] not in ("delete_folder", "delete_session") or row["state"] != "committed"
    ):
        raise ValueError("navigation operation只有committed删除可以等待settlement")
    if row["state"] == "running" and (row["holder_id"] is None or row["fencing_token"] < 1):
        raise ValueError("navigation operation running缺少holder或fencing token")


def _record_from_row(row: sqlite3.Row) -> NavigationMutationRecord:
    # 读取、幂等重放与启动使用同一 typed 合同，不从当前节点或配置重建历史意图。
    params = json.loads(row["params_json"])
    if not isinstance(params, dict):
        raise TypeError("navigation intent params 必须是对象")
    for name in ("gateway_id", "workspace_id", "actor"):
        if not isinstance(row[name], str) or not row[name]:
            raise ValueError(f"navigation intent {name} 必须是非空字符串")
    for name in ("target_node_id", "created_by_operation_id"):
        if name not in params or params[name] != row[name]:
            raise ValueError(f"navigation intent 冻结 {name} 与记录身份不符")
    command_type = AutoSessionTitleCommand if row["kind"] == "auto_title_session" else NavigationMutationIntentDTO
    intent = command_type.model_validate({
        **params,
        "client_operation_id": row["operation_id"],
        "client_sequence": row["client_sequence"],
        "kind": row["kind"],
        "depends_on": json.loads(row["depends_on_json"]),
    }, strict=True)
    if params != json.loads(_params_json(intent)):
        raise ValueError("navigation intent 冻结参数字段不完整或与 typed intent 不符")
    if compute_intent_preimage_hash(intent) != row["preimage_hash"]:
        raise ValueError(f"navigation intent preimage 不符: operation_id={row['operation_id']!r}")
    _require_record_metadata(row)
    return NavigationMutationRecord(
        gateway_id=row["gateway_id"],
        workspace_id=row["workspace_id"],
        actor=row["actor"],
        operation_id=row["operation_id"],
        client_sequence=intent.client_sequence,
        queue_seq=row["queue_seq"],
        kind=intent.kind,
        state=row["state"],
        params=params,
        preimage_hash=row["preimage_hash"],
        depends_on=tuple(intent.depends_on),
        created_by_operation_id=row["created_by_operation_id"],
        target_node_id=row["target_node_id"],
        reserved_node_id=row["reserved_node_id"],
        result_node_id=row["result_node_id"],
        result_node_revision=row["result_node_revision"],
        committed_catalog_revision=row["committed_catalog_revision"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        pending_settlement=row["pending_settlement"] == 1,
        holder_id=row["holder_id"],
        fencing_token=row["fencing_token"],
        receipt_revision=row["receipt_revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> NavigationEventRecord:
    # 先按原始类型验证；不能把对象的key当node列表或把浮点序号截断为整数。
    payload = dict(row)
    payload["affected_node_ids"] = json.loads(payload.pop("affected_node_ids_json"))
    payload["created_at"] = datetime.fromisoformat(row["created_at"])
    event = NavigationEventDTO.model_validate(payload, strict=True)
    validate_operation_id(event.operation_id)
    for name in ("workspace_id", "gateway_id", "actor"):
        if not isinstance(row[name], str) or not row[name]:
            raise ValueError(f"navigation event {name} 必须是非空字符串")
    if event.queue_seq < 1 or (
        event.committed_catalog_revision is not None and event.committed_catalog_revision < 0
    ):
        raise ValueError("navigation event queue_seq或catalog revision越界")
    if event.result_state not in NAVIGATION_MUTATION_TERMINAL_STATES:
        raise ValueError("navigation event必须引用终态operation")
    if event.created_at.tzinfo is None:
        raise ValueError("navigation event created_at必须带时区")
    if event.affected_node_ids != sorted(set(event.affected_node_ids)):
        raise ValueError("navigation event affected_node_ids必须是有序唯一列表")
    return NavigationEventRecord(
        workspace_id=event.workspace_id,
        event_seq=event.event_seq,
        operation_id=event.operation_id,
        gateway_id=row["gateway_id"],
        actor=row["actor"],
        queue_seq=event.queue_seq,
        kind=event.kind,
        result_state=event.result_state,
        committed_catalog_revision=event.committed_catalog_revision,
        affected_node_ids=tuple(event.affected_node_ids),
        error_code=event.error_code,
        error_detail=event.error_detail,
        created_at=row["created_at"],
    )


def _owner_from_row(row: sqlite3.Row) -> NavigationQueueOwner:
    """读取原始owner元数据；generation不能由浮点或文本隐式转换。"""
    for name in ("workspace_id", "owner_id"):
        if not isinstance(row[name], str) or not row[name]:
            raise ValueError(f"navigation queue owner {name}必须是非空字符串")
    generation = row["owner_generation"]
    if type(generation) is not int or generation < 1:
        raise ValueError(f"navigation queue owner generation必须是正整数: {generation!r}")
    if datetime.fromisoformat(row["updated_at"]).tzinfo is None:
        raise ValueError("navigation queue owner updated_at必须带时区")
    return NavigationQueueOwner(
        workspace_id=row["workspace_id"], owner_id=row["owner_id"], generation=generation,
    )
