"""外部运行回执校验、失败说明与领域记录投影；不执行资源操作或持久写入。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict
from datetime import datetime, timedelta
from urllib.error import URLError

from app.abstractions.browser_resources import BrowserRequestError
from app.abstractions.terminal.resources import TerminalRequestError
from app.abstractions.workspace_resources import ResourceKind, ResourceSnapshot
from app.core.failures import (
    failure_detail,
    mark_public_failure,
    public_failure_message,
    report_failure,
)
from app.schemas.internal_v2.workspace_resource import WorkspaceResourceDTO
from app.schemas.public_metadata import is_native_relative_path
from app.services.business.workspace_resources.records import OwnedResourceRecord
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetFacts,
    RuntimeResourceTargetFacts,
)

ORPHAN_GRACE_SECONDS = 30 * 60
STOPPED_STATUSES = frozenset({"completed", "exited", "terminated", "closed", "deleted"})


def resource_target_facts(snapshot: ResourceSnapshot) -> ResourceTargetFacts:
    """统一目标检查只消费原 owner 已发布的完整投影。"""
    resource = WorkspaceResourceDTO.from_owner(snapshot)
    return ResourceTargetFacts(
        resource_uri=None, storage_domain_id=resource.workspace_id, lifecycle="runtime",
        available=resource.reconciliation_status == "verified" and "attach" in resource.available_actions,
        runtime=RuntimeResourceTargetFacts(
            kind=resource.kind, resource_id=resource.resource_id, workspace_id=resource.workspace_id,
            retention_scope=resource.retention_scope,
            source_thread=(resource.source_thread.session_id, resource.source_thread.thread_id),
            thread_associations=tuple((ref.session_id, ref.thread_id) for ref in resource.thread_associations),
            owner_revision=resource.owner_revision, status=resource.status,
            reconciliation_status=resource.reconciliation_status, available_actions=tuple(resource.available_actions),
        ),
    )


def validate_runtime(runtime: ResourceSnapshot, workspace_id: str) -> None:
    if runtime.get("workspace_id") != workspace_id:
        raise mark_public_failure(ValueError("Node 运行时 工作区身份与 外部资源 owner 冲突"))
    if any(key in runtime for key in ("cwd", "workspace_root", "session_id", "source_thread", "retention_scope", "thread_associations", "attach_url")):
        raise mark_public_failure(ValueError("Node 运行时 回执包含禁止上浮的真实路径"))
    relative = runtime.get("cwd_relative")
    if relative is not None and not is_native_relative_path(relative):
        error = mark_public_failure(ValueError("Node 运行时 工作目录不是工作区内相对目录"))
        error.add_note(f"Node 运行时 工作目录拒绝: cwd_relative={relative!r}")
        report_failure(error, detail="Node 运行时 工作目录拒绝")
        raise error
    revision = runtime.get("transport_owner_revision")
    if type(revision) is not int or revision < 1:
        raise mark_public_failure(ValueError("Node 运行时 回执缺少有效传输版本"))


def index_resources(
    items: list[ResourceSnapshot], workspace_id: str, owned_ids: Collection[str], kind: ResourceKind,
) -> dict[str, ResourceSnapshot]:
    result: dict[str, ResourceSnapshot] = {}
    for index, item in enumerate(items):
        validate_runtime(item, workspace_id)
        rid = str(item[f"{kind}_id"])
        if rid in result or rid not in owned_ids:
            reason = "duplicate_identity" if rid in result else "unregistered_identity"
            # 外部身份可能夹带原生路径；原值只进诊断，普通错误发布可核验的拒绝事实。
            error = mark_public_failure(RuntimeError(f"Node 运行清单身份拒绝: kind={kind}, reason={reason}, index={index}"))
            error.add_note(f"被拒绝的 Node 运行清单身份: identity={rid!r}")
            report_failure(error, detail=str(error))
            raise error
        result[rid] = item
    return result


def observe_resource(
    record: OwnedResourceRecord, runtime: ResourceSnapshot | None, kind: ResourceKind,
    *, creation_receipt: bool = False,
) -> None:
    if runtime is None:
        if record.status != "deleted":
            record.reconciliation_status = "unknown"
            record.error = "外部运行清单缺少对应资源；没有停止/删除回执"
        return
    validate_runtime(runtime, record.workspace_id)
    if runtime.get(f"{kind}_id") != record.resource_id:
        raise mark_public_failure(ValueError(f"Node 运行时 身份与 外部资源 owner 冲突: {record.resource_id}"))
    status = str(runtime["status"])
    recovery = str(runtime.get("reconciliation_status", "unknown"))
    # 原生created是尚在启动；未核实的存活观察不能抹掉原创建阶段，否则管理会跳过取消原执行。
    pending_creation = status == "created" or (
        record.status == "creating" and not creation_receipt and recovery != "verified"
    )
    record.status = "creating" if pending_creation else status
    # 已持久化的未知动作结果跨重启保留，存活快照不能证明旧动作是否执行。
    # 只有原创建回执能首次核实创建；普通清单不能替代丢失的回执。
    if record.reconciliation_status == "verified" or creation_receipt:
        record.reconciliation_status = recovery
        record.error = None if recovery == "verified" else "Node 运行时 身份或连接尚待核实"


def lease_status(record: OwnedResourceRecord) -> str:
    if record.reconciliation_status != "verified":
        return "unknown"
    return "stopped" if record.status in STOPPED_STATUSES else "running"


def public_error(
    error: BaseException, *, stage: str = "resource_operation_unknown",
    label: str = "外部资源操作结果未核实",
) -> str:
    """API 与持久记录共用来源已核定的说明，未知异常只发布诊断事实。"""
    diagnostic = failure_detail(error, stage=stage, label=label)
    if (message := public_failure_message(error)) is not None:
        return message
    if isinstance(error, (BrowserRequestError, TerminalRequestError)):
        # 两个具名协议类型自行构造说明；Browser 保留已登记的本地页面 URL 例外。
        message = f"{type(error).__name__}: {error}"
    elif isinstance(error, OSError):
        # 原生异常的正文及 filename 都可能携带路径，只发布系统错误事实，原异常由调用方记录。
        native = error.reason if isinstance(error, URLError) and isinstance(error.reason, OSError) else error
        details = [f"errno={native.errno}"]
        winerror = getattr(native, "winerror", None)
        if winerror is not None:
            details.append(f"winerror={winerror}")
        message = (
            f"{type(error).__name__}: 操作回执不可核实 ({', '.join(details)})；"
            "请查看工作区外部资源服务日志中的详细错误"
        )
    else:
        message = diagnostic
    mark_public_failure(error, message)
    return message


def resource_projection(
    record: OwnedResourceRecord, runtime: ResourceSnapshot | None, kind: ResourceKind,
) -> ResourceSnapshot:
    actions: list[str] = []
    if record.reconciliation_status == "verified" and record.status not in {
        "deleted", "deleting", "stopping", "creating",
    }:
        actions = ["attach", "detach", "stop"]
        if not record.thread_associations:
            actions.append("delete")
        if record.retention_scope == "thread":
            actions.insert(0, "promote")
    elif record.reconciliation_status != "verified" and runtime is not None and runtime.get("transport_owner_revision") == record.owner_revision:
        # 候选动作仍须核版本与原 lease；未知状态不允许重新使用或关联资源。
        if record.status not in {"deleted", "deleting"}:
            actions.append("stop")
        if not record.thread_associations:
            actions.append("delete")
    elif record.status == "creating" and record.reconciliation_status != "verified":
        # 仅显式管理stop可向原生owner申请持久取消证明，清单缺失自身不是证明。
        actions.append("stop")
    reclaim_at = (
        (datetime.fromisoformat(record.unattached_since)
         + timedelta(seconds=ORPHAN_GRACE_SECONDS)).isoformat()
        if record.unattached_since is not None else None
    )
    return {
        **(runtime or {}), f"{kind}_id": record.resource_id, "resource_id": record.resource_id,
        "workspace_id": record.workspace_id, "kind": kind,
        "title": runtime["title"] if kind == "browser" and runtime is not None else record.title,
        "session_id": record.source_thread.session_id, "source_thread_id": record.source_thread.thread_id,
        "source_thread": asdict(record.source_thread), "retention_scope": record.retention_scope,
        "thread_associations": [asdict(ref) for ref in record.thread_associations],
        "owner_revision": record.owner_revision, "status": record.status,
        "reconciliation_status": record.reconciliation_status, "unattached_since": record.unattached_since,
        "reclaim_at": reclaim_at, "available_actions": actions, "error": record.error,
        "created_at": record.created_at, "updated_at": record.updated_at,
    }
