from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ResourceStopper = Callable[[], Awaitable[None] | None]


def resource_refs_from_tool_payload(
    tool_name: object,
    raw_args: object,
) -> tuple[tuple[str, str], ...]:
    """从工具公开参数提取资源引用，供 lease 和 Activity 复用同一规则。"""
    args = raw_args if isinstance(raw_args, Mapping) else {}
    candidates: list[tuple[str, str]] = []
    for key, kind in (
        ("terminal_id", "terminal"),
        ("pageId", "browser_context"),
        ("browserId", "browser_context"),
        ("development_server_id", "development_server"),
        ("mcp_connection_id", "mcp_connection"),
    ):
        value = args.get(key)
        if isinstance(value, str) and value:
            candidates.append((value, kind))
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        server_id = tool_name.removeprefix("mcp__").split("__", 1)[0]
        if server_id:
            candidates.append((f"mcp:{server_id}", "mcp_connection"))
    return tuple(dict.fromkeys(candidates))


@dataclass(slots=True)
class ResourceRecord:
    resource_id: str
    kind: str
    lifetime_scope: str
    created_by_turn_id: str | None
    cleanup_policy: str
    status: str = "running"
    updated_at: str = ""


@dataclass(slots=True)
class ResourceLease:
    lease_id: str
    resource_id: str
    turn_stream_id: str
    operation_id: str
    status: str = "active"
    updated_at: str = ""


class ResourceManager:
    """跨 Turn 持久资源的生命周期与操作 lease 管理器。"""

    EXTERNAL_RESOURCE_KINDS = frozenset(
        {
            "terminal",
            "browser_context",
            "mcp_connection",
            "development_server",
        }
    )

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._state_path = state_path
        self._records: dict[str, ResourceRecord] = {}
        self._leases: dict[str, ResourceLease] = {}
        self._stoppers: dict[str, ResourceStopper] = {}
        self._load()

    def register(
        self,
        *,
        resource_id: str,
        kind: str,
        lifetime_scope: str,
        cleanup_policy: str = "retain",
        created_by_turn_id: str | None = None,
        stopper: ResourceStopper | None = None,
    ) -> ResourceRecord:
        if not resource_id or not kind:
            raise ValueError("ResourceManager.register 缺少 resource_id 或 kind")
        if lifetime_scope not in {"turn", "session", "workspace", "global"}:
            raise ValueError(f"未知资源生命周期范围: {lifetime_scope}")
        record = self._records.get(resource_id)
        if record is None:
            record = ResourceRecord(
                resource_id=resource_id,
                kind=kind,
                lifetime_scope=lifetime_scope,
                created_by_turn_id=created_by_turn_id,
                cleanup_policy=cleanup_policy,
                updated_at=self._now(),
            )
            self._records[resource_id] = record
        else:
            if record.kind != kind:
                raise RuntimeError(
                    f"资源类型发生冲突: resource_id={resource_id} "
                    f"existing={record.kind} incoming={kind}"
                )
            record.updated_at = self._now()
        if stopper is not None:
            self._stoppers[resource_id] = stopper
        self._persist()
        return self._copy_record(record)

    def acquire(
        self,
        *,
        resource_id: str,
        turn_stream_id: str,
        lease_id: str,
        operation_id: str,
    ) -> ResourceLease:
        record = self._require_record(resource_id)
        if record.status not in {"running", "recovered"}:
            raise RuntimeError(
                f"资源当前不可获取 lease: resource_id={resource_id} status={record.status}"
            )
        if not turn_stream_id or not lease_id or not operation_id:
            raise ValueError("资源 lease 缺少 turn_stream_id、lease_id 或 operation_id")
        existing = self._leases.get(lease_id)
        if existing is not None:
            if (
                existing.resource_id != resource_id
                or existing.turn_stream_id != turn_stream_id
                or existing.operation_id != operation_id
            ):
                raise RuntimeError(f"资源 lease 重复但关联键不一致: lease_id={lease_id}")
            return self._copy_lease(existing)
        lease = ResourceLease(
            lease_id=lease_id,
            resource_id=resource_id,
            turn_stream_id=turn_stream_id,
            operation_id=operation_id,
            updated_at=self._now(),
        )
        self._leases[lease_id] = lease
        self._persist()
        return self._copy_lease(lease)

    def register_external(
        self,
        *,
        resource_id: str,
        kind: str,
        lifetime_scope: str = "session",
        cleanup_policy: str = "retain",
        created_by_turn_id: str | None = None,
        stopper: ResourceStopper | None = None,
    ) -> ResourceRecord:
        """登记已有的 terminal/browser/MCP/dev-server，不代替外部资源创建。"""
        if kind not in self.EXTERNAL_RESOURCE_KINDS:
            raise ValueError(f"不支持的外部持久资源 kind: {kind}")
        return self.register(
            resource_id=resource_id,
            kind=kind,
            lifetime_scope=lifetime_scope,
            cleanup_policy=cleanup_policy,
            created_by_turn_id=created_by_turn_id,
            stopper=stopper,
        )

    def acquire_operation(
        self,
        *,
        resource_id: str,
        turn_stream_id: str,
        operation_id: str,
        lease_id: str | None = None,
    ) -> ResourceLease:
        """为一次 Tool/Activity 操作取得 lease；不自动重放操作。"""
        resolved_lease_id = lease_id or f"{turn_stream_id}:{operation_id}:{resource_id}"
        return self.acquire(
            resource_id=resource_id,
            turn_stream_id=turn_stream_id,
            lease_id=resolved_lease_id,
            operation_id=operation_id,
        )

    def release(self, lease_id: str, *, reason: str = "operation_finished") -> ResourceLease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise KeyError(f"资源 lease 不存在: lease_id={lease_id}")
        lease.status = "released"
        lease.updated_at = self._now()
        self._persist()
        return self._copy_lease(lease)

    async def cancel_turn(self, turn_stream_id: str) -> list[ResourceLease]:
        released: list[ResourceLease] = []
        for lease in tuple(self._leases.values()):
            if lease.turn_stream_id != turn_stream_id or lease.status != "active":
                continue
            record = self._require_record(lease.resource_id)
            if record.cleanup_policy == "destroy_on_turn_end":
                await self.stop(
                    resource_id=record.resource_id,
                    lease_id=lease.lease_id,
                    reason="turn_cancelled",
                )
            else:
                released.append(self.release(lease.lease_id, reason="turn_cancelled"))
        return released

    async def stop(
        self,
        *,
        resource_id: str,
        lease_id: str | None = None,
        reason: str = "explicit_stop",
    ) -> ResourceRecord:
        record = self._require_record(resource_id)
        if lease_id is not None:
            lease = self._leases.get(lease_id)
            if lease is None or lease.resource_id != resource_id:
                raise RuntimeError(
                    f"资源 stop 的 lease 不匹配: resource_id={resource_id} lease_id={lease_id}"
                )
            if lease.status != "active":
                raise RuntimeError(
                    f"资源 stop 的 lease 不处于 active 状态: lease_id={lease_id} status={lease.status}"
                )
        stopper = self._stoppers.get(resource_id)
        if stopper is not None:
            result = stopper()
            if inspect.isawaitable(result):
                await result
        record.status = "stopped"
        record.updated_at = self._now()
        for lease in self._leases.values():
            if lease.resource_id == resource_id and lease.status == "active":
                lease.status = "released"
                lease.updated_at = record.updated_at
        self._persist()
        return self._copy_record(record)

    def reconcile(
        self,
        observed_status: Mapping[str, str],
    ) -> list[ResourceRecord]:
        """用外部资源清单对崩溃后的 record/lease 做保守对账。"""
        reconciled: list[ResourceRecord] = []
        for record in self._records.values():
            observed = observed_status.get(record.resource_id)
            if observed in {"running", "stopped"}:
                record.status = "recovered" if observed == "running" else "stopped"
            elif observed is None:
                record.status = "orphaned"
            else:
                record.status = "unknown"
            record.updated_at = self._now()
            reconciled.append(self._copy_record(record))
        for lease in self._leases.values():
            if lease.status == "active":
                lease.status = "reconcile_required"
                lease.updated_at = self._now()
        self._persist()
        return reconciled

    def get(self, resource_id: str) -> ResourceRecord | None:
        record = self._records.get(resource_id)
        return self._copy_record(record) if record is not None else None

    def leases_for_turn(self, turn_stream_id: str) -> list[ResourceLease]:
        return [
            self._copy_lease(lease)
            for lease in self._leases.values()
            if lease.turn_stream_id == turn_stream_id
        ]

    def _require_record(self, resource_id: str) -> ResourceRecord:
        record = self._records.get(resource_id)
        if record is None:
            raise KeyError(f"持久资源不存在: resource_id={resource_id}")
        return record

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(f"资源状态文件必须是对象: path={self._state_path}")
        for value in raw.get("records", []):
            if isinstance(value, dict):
                record = ResourceRecord(**value)
                self._records[record.resource_id] = record
        for value in raw.get("leases", []):
            if isinstance(value, dict):
                lease = ResourceLease(**value)
                self._leases[lease.lease_id] = lease

    def _persist(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(".tmp")
        payload = {
            "records": [asdict(record) for record in self._records.values()],
            "leases": [asdict(lease) for lease in self._leases.values()],
        }
        with temp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(self._state_path)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _copy_record(record: ResourceRecord) -> ResourceRecord:
        return ResourceRecord(**asdict(record))

    @staticmethod
    def _copy_lease(lease: ResourceLease) -> ResourceLease:
        return ResourceLease(**asdict(lease))
