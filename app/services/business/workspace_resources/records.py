"""领域 owner 的保留与关联记录；不复制 PTY 或 Playwright 运行事实。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.abstractions.workspace_resources import ResourceKind, RetentionScope
from app.core.identifier import validate_session_id, validate_thread_id
from app.core.workspace_identity import validate_workspace_id


@dataclass(frozen=True, slots=True)
class ThreadRef:
    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        validate_thread_id(self.thread_id)


@dataclass(slots=True)
class ForkAttachReceipt:
    """原 owner 的一次 fork attach 事实；不复制当前生命周期或外部实例。"""

    creation_key: str
    target_thread: ThreadRef
    expected_revision: int
    state: Literal["dispatching", "committed", "rejected"]
    lease_id: str | None = None
    committed_revision: int | None = None
    error: str | None = None

    def validate(self, key: str, revision: int) -> None:
        if not self.creation_key or self.creation_key != key or self.expected_revision < 1:
            raise ValueError("fork attach 回执缺少冻结 operation 身份或版本")
        if self.state not in {"dispatching", "committed", "rejected"}:
            raise ValueError("fork attach 回执状态无效")
        if self.state == "committed" and (
            self.committed_revision != self.expected_revision + 1 or self.committed_revision > revision
        ):
            raise ValueError("fork attach 提交回执版本与原 owner 不一致")
        if self.state != "committed" and self.committed_revision is not None:
            raise ValueError("未提交的 fork attach 不能携带提交版本")
        if self.state != "rejected" and not self.lease_id:
            raise ValueError("已派发的 fork attach 缺少精确 operation lease")


@dataclass(slots=True)
class OwnedResourceRecord:
    resource_id: str
    workspace_id: str
    # 资源创建时记录的来源 Thread，是起源事实，不随显式关联的增删而改变。
    source_thread: ThreadRef
    retention_scope: RetentionScope
    title: str
    created_at: str
    updated_at: str
    # 当前显式关联列表，可变且可空；Thread 级资源为空时，查询仍可按 source_thread 筛选。
    # 该列表只记录关联，不代表操作租约持有者或完整准入结果。
    thread_associations: list[ThreadRef] = field(default_factory=list)
    # owner 持久化的传输授权版本，用于版本校验，并与外部 transport_owner_revision 对账。
    owner_revision: int = 1
    # 生命周期状态；观察时来自外部运行快照，创建、停止或删除期间也可处于过渡状态。
    status: str = "creating"
    # 外部状态及前次操作结果的核实状态，与生命周期状态分开。
    reconciliation_status: str = "reconcile_required"
    unattached_since: str | None = None
    error: str | None = None
    fork_attachments: dict[str, ForkAttachReceipt] = field(default_factory=dict)

    def validate(self, workspace_id: str, kind: ResourceKind) -> None:
        if self.workspace_id != workspace_id:
            raise ValueError(f"资源 记录工作区冲突: resource_id={self.resource_id}")
        if not self.resource_id.startswith("term_" if kind == "terminal" else "browser_") or self.owner_revision < 1:
            raise ValueError(f"资源 记录身份或版本无效: {self.resource_id}")
        if self.retention_scope not in {"thread", "workspace"}:
            raise ValueError(f"资源 保留范围无效: {self.retention_scope}")
        if self.reconciliation_status not in {"verified", "unknown", "reconcile_required"}:
            raise ValueError(f"资源 恢复事实无效: {self.resource_id}")
        if len(set(self.thread_associations)) != len(self.thread_associations):
            raise ValueError(f"资源 Thread 关联重复: {self.resource_id}")
        if self.retention_scope == "thread" and any(
            item != self.source_thread for item in self.thread_associations
        ):
            raise ValueError(f"临时 资源 关联超出来源 Thread: {self.resource_id}")
        for value in (self.created_at, self.updated_at, self.unattached_since):
            if value is not None:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is None:
                    raise ValueError(f"资源 时间必须包含时区: {self.resource_id}")
        if self.unattached_since is not None and (
            self.retention_scope != "thread" or self.thread_associations
        ):
            raise ValueError(f"资源 孤儿期限与关联冲突: {self.resource_id}")
        for key, receipt in self.fork_attachments.items():
            receipt.validate(key, self.owner_revision)


class ResourceRecordStore:
    """只读唯一 owner 格式，拒绝旧 Node 领域数据的隐式导入。"""

    def __init__(self, *, workspace_root: Path, workspace_id: str, kind: ResourceKind) -> None:
        self.kind = kind
        self.workspace_id = validate_workspace_id(workspace_id)
        self.path = workspace_root / ".boxteam" / f"{kind}-resources.json"
        self.records: dict[str, OwnedResourceRecord] = {}
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or payload.get("workspace_id") != workspace_id:
            raise ValueError(f"资源 owner 持久格式或工作区不匹配: {self.path}")
        if not isinstance(payload.get("records"), list):
            raise TypeError(f"资源 owner records 必须是数组: {self.path}")
        for raw in payload["records"]:
            record = OwnedResourceRecord(
                **{
                    **raw,
                    "source_thread": ThreadRef(**raw["source_thread"]),
                    "thread_associations": [ThreadRef(**item) for item in raw["thread_associations"]],
                    "fork_attachments": {
                        key: ForkAttachReceipt(**{**item, "target_thread": ThreadRef(**item["target_thread"])})
                        for key, item in raw.get("fork_attachments", {}).items()
                    },
                }
            )
            record.validate(workspace_id, kind)
            if record.resource_id in self.records:
                raise ValueError(f"资源 owner 记录重复: {record.resource_id}")
            self.records[record.resource_id] = record

    def persist(self) -> None:
        for record in self.records.values():
            record.validate(self.workspace_id, self.kind)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as stream:
            json.dump({
                "schema_version": 1,
                "workspace_id": self.workspace_id,
                "records": [asdict(record) for record in self.records.values()],
            }, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(self.path)
