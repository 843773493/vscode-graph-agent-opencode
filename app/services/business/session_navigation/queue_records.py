"""会话目录队列的不可变行投影、跨进程幂等 preimage 与 catalog revision 读取。

本模块是「记录模型」这一条垂直链的唯一承载点：

- ``NavigationMutationRecord`` / ``NavigationEventRecord`` 精确对应两张旁挂表的行形态；
- ``_record_from_row`` / ``_event_from_row`` 是两个投影的唯一构造点（逐字段
  ``str()``/``int()`` 归一，可空列还原为 None），调用方统一经由
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

from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
    NavigationMutationIntentDTO,
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


def compute_intent_preimage_hash(intent: NavigationMutationIntentDTO) -> str:
    """规范化 intent 参数并计算 preimage hash（同 key 异 preimage 冲突的判据）。

    只纳入会影响业务结果的规范化字段；``client_operation_id`` 本身与
    ``client_sequence``、``base_catalog_revision`` 不参与 preimage（重试时
    客户端可能给出新的快照修订，但意图未变）。
    """
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


def _params_json(intent: NavigationMutationIntentDTO) -> str:
    return json.dumps(
        {
            "expected_revision": intent.expected_revision,
            "base_catalog_revision": intent.base_catalog_revision,
            "target_node_id": intent.target_node_id,
            "created_by_operation_id": intent.created_by_operation_id,
            "name": intent.name,
            "parent_node_id": intent.parent_node_id,
            "recursive": intent.recursive,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_from_row(row: sqlite3.Row) -> NavigationMutationRecord:
    return NavigationMutationRecord(
        gateway_id=str(row["gateway_id"]),
        workspace_id=str(row["workspace_id"]),
        actor=str(row["actor"]),
        operation_id=str(row["operation_id"]),
        client_sequence=int(row["client_sequence"]),
        queue_seq=int(row["queue_seq"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        params=json.loads(str(row["params_json"])),
        preimage_hash=str(row["preimage_hash"]),
        depends_on=tuple(json.loads(str(row["depends_on_json"]))),
        created_by_operation_id=row["created_by_operation_id"],
        target_node_id=row["target_node_id"],
        reserved_node_id=row["reserved_node_id"],
        result_node_id=row["result_node_id"],
        result_node_revision=row["result_node_revision"],
        committed_catalog_revision=row["committed_catalog_revision"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        pending_settlement=bool(row["pending_settlement"]),
        holder_id=row["holder_id"],
        fencing_token=int(row["fencing_token"]),
        receipt_revision=int(row["receipt_revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> NavigationEventRecord:
    return NavigationEventRecord(
        workspace_id=str(row["workspace_id"]),
        event_seq=int(row["event_seq"]),
        operation_id=str(row["operation_id"]),
        gateway_id=str(row["gateway_id"]),
        actor=str(row["actor"]),
        queue_seq=int(row["queue_seq"]),
        kind=str(row["kind"]),
        result_state=str(row["result_state"]),
        committed_catalog_revision=row["committed_catalog_revision"],
        affected_node_ids=tuple(json.loads(str(row["affected_node_ids_json"]))),
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        created_at=str(row["created_at"]),
    )
