"""领域 owner 的保留与关联记录；不复制 PTY 或 Playwright 运行事实。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app.abstractions.workspace_resources import ResourceKind, RetentionScope
from app.core.session_catalog_store import validate_session_id, validate_thread_id
from app.core.workspace_identity import validate_workspace_id


@dataclass(frozen=True, slots=True)
class ThreadRef:
    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        validate_thread_id(self.thread_id)


@dataclass(slots=True)
class OwnedResourceRecord:
    resource_id: str
    workspace_id: str
    source_thread: ThreadRef
    retention_scope: RetentionScope
    title: str
    created_at: str
    updated_at: str
    thread_associations: list[ThreadRef] = field(default_factory=list)
    owner_revision: int = 1
    status: str = "creating"
    reconciliation_status: str = "reconcile_required"
    unattached_since: str | None = None
    error: str | None = None

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
