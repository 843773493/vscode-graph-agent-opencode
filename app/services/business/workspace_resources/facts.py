"""外部运行回执校验与领域记录的纯投影；不执行外部操作或持久写入。"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from app.abstractions.workspace_resources import ResourceKind, ResourceSnapshot
from app.services.business.workspace_resources.records import OwnedResourceRecord

ORPHAN_GRACE_SECONDS = 30 * 60
STOPPED_STATUSES = frozenset({"completed", "exited", "terminated", "closed", "deleted"})
_REAL_PATH = re.compile(r"""(?:^|[\s'"=(])(?:/[^\s'"]+|[A-Za-z]:[\\/][^\s'"]+)""")


def validate_runtime(runtime: ResourceSnapshot, workspace_id: str) -> None:
    if runtime.get("workspace_id") != workspace_id:
        raise ValueError("Node 运行时 工作区身份与 外部资源 owner 冲突")
    if any(key in runtime for key in ("cwd", "workspace_root", "session_id", "source_thread", "retention_scope", "thread_associations", "attach_url")):
        raise ValueError("Node 运行时 回执包含禁止上浮的真实路径")
    relative = runtime.get("cwd_relative")
    if relative is not None and (
        not isinstance(relative, str) or Path(relative).is_absolute()
        or re.match(r"^[A-Za-z]:", relative)
    ):
        raise ValueError("Node 运行时 工作目录不是工作区内相对目录")
    revision = runtime.get("transport_owner_revision")
    if not isinstance(revision, int) or revision < 1:
        raise ValueError("Node 运行时 回执缺少有效传输版本")


def index_resources(
    items: list[ResourceSnapshot], workspace_id: str, owned_ids: Collection[str], kind: ResourceKind,
) -> dict[str, ResourceSnapshot]:
    result: dict[str, ResourceSnapshot] = {}
    for item in items:
        validate_runtime(item, workspace_id)
        rid = str(item[f"{kind}_id"])
        if rid in result or rid not in owned_ids:
            raise RuntimeError(f"Node 运行时 存在重复或未经 Python owner 登记的身份: {rid}")
        result[rid] = item
    return result


def observe_resource(
    record: OwnedResourceRecord, runtime: ResourceSnapshot | None, recovered: bool, kind: ResourceKind,
) -> None:
    if runtime is None:
        if record.status != "deleted":
            record.reconciliation_status = "unknown"
            record.error = "外部运行清单缺少对应资源；没有停止/删除回执"
        return
    validate_runtime(runtime, record.workspace_id)
    if runtime.get(f"{kind}_id") != record.resource_id:
        raise ValueError(f"Node 运行时 身份与 外部资源 owner 冲突: {record.resource_id}")
    was_creating = record.status == "creating"
    record.status = str(runtime["status"])
    recovery = str(runtime.get("reconciliation_status", "unknown"))
    # 同进程未知结果不得被一次列表查询无条件抹去。
    if not recovered or record.reconciliation_status == "verified" or was_creating:
        record.reconciliation_status = recovery
        record.error = None if recovery == "verified" else "Node 运行时 身份或连接尚待核实"


def lease_status(record: OwnedResourceRecord) -> str:
    if record.reconciliation_status != "verified":
        return "unknown"
    return "stopped" if record.status in STOPPED_STATUSES else "running"


def public_error(error: BaseException) -> str:
    message = str(error)
    if _REAL_PATH.search(message):
        message = "操作回执不可核实；请查看工作区 外部资源 服务日志中的详细错误"
    return f"{type(error).__name__}: {message}"


def resource_projection(
    record: OwnedResourceRecord, runtime: ResourceSnapshot | None, kind: ResourceKind,
) -> ResourceSnapshot:
    actions: list[str] = []
    if record.reconciliation_status == "verified" and record.status not in {
        "deleted", "deleting", "stopping", "creating",
    }:
        actions = ["attach", "detach", "stop", "delete"]
        if record.retention_scope == "thread":
            actions.insert(0, "promote")
    reclaim_at = (
        (datetime.fromisoformat(record.unattached_since)
         + timedelta(seconds=ORPHAN_GRACE_SECONDS)).isoformat()
        if record.unattached_since is not None else None
    )
    return {
        **(runtime or {}), f"{kind}_id": record.resource_id, "resource_id": record.resource_id,
        "workspace_id": record.workspace_id, "kind": kind, "title": record.title,
        "session_id": record.source_thread.session_id, "source_thread_id": record.source_thread.thread_id,
        "source_thread": asdict(record.source_thread), "retention_scope": record.retention_scope,
        "thread_associations": [asdict(ref) for ref in record.thread_associations],
        "owner_revision": record.owner_revision, "status": record.status,
        "reconciliation_status": record.reconciliation_status, "unattached_since": record.unattached_since,
        "reclaim_at": reclaim_at, "available_actions": actions, "error": record.error,
        "created_at": record.created_at, "updated_at": record.updated_at,
    }
