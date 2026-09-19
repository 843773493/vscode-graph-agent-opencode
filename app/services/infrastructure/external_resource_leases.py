"""跨 Turn 外部资源的持久 operation lease 账本。

本模块只回答「哪个 holder 以哪次 operation 占用了哪个已登记资源」，
并把占用状态持久化到工作区 .boxteam/resources.json，供进程重启后的
owner 恢复与对账。它不猜测工具参数、不保存 cleanup_policy、不持有任何
内存 stopper：资源是否真的停止、何时停止，只由实际 owner 验证身份与
状态后写入结清结果（settle），账本绝不代替 owner 宣称资源已停止。
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.services.infrastructure.events.channel_events import (
    RESOURCE_STATE_STATES,
    ResourceStateEventPublisher,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResourceRecord:
    resource_id: str
    kind: str
    lifetime_scope: str
    created_by_turn_id: str | None
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


class ExternalResourceLeaseLedger:
    """外部资源身份登记与 operation lease 的唯一持久账本。"""

    EXTERNAL_RESOURCE_KINDS = frozenset(
        {
            "terminal",
            "browser_context",
            "mcp_connection",
            "development_server",
            # Node 调试进程由 debug owner 以精确 (session_id, thread_id) 持有，
            # 每次启动的占用 identity 是唯一的 process_instance_id，而不是
            # 发起本次操作的 tool_call / Web request 或其短期 operation lease。
            "node_debug_process",
        }
    )

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        state_events: ResourceStateEventPublisher | None = None,
    ) -> None:
        self._state_path = state_path
        #: owner 已核实状态变化的轻量通知出口（resource.state/{owner_domain}）。
        #: 账本只在语义上等价于「owner 已核实资源终态」的转换点发布（settle、
        #: reconcile），lease 释放/获取不发布，避免把 Turn 收尾虚报为资源终态。
        self._state_events = state_events
        #: 通知失败时显式记录的错误摘要（有界）；durable 事实已先行落盘，不回滚。
        self._notification_errors: deque[str] = deque(maxlen=64)
        self._records: dict[str, ResourceRecord] = {}
        self._leases: dict[str, ResourceLease] = {}
        self._load()

    def register(
        self,
        *,
        resource_id: str,
        kind: str,
        lifetime_scope: str,
        created_by_turn_id: str | None = None,
    ) -> ResourceRecord:
        if not resource_id or not kind:
            raise ValueError("lease 账本登记缺少 resource_id 或 kind")
        if lifetime_scope not in {"turn", "session", "workspace", "global"}:
            raise ValueError(f"未知资源生命周期范围: {lifetime_scope}")
        record = self._records.get(resource_id)
        if record is None:
            record = ResourceRecord(
                resource_id=resource_id,
                kind=kind,
                lifetime_scope=lifetime_scope,
                created_by_turn_id=created_by_turn_id,
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
        created_by_turn_id: str | None = None,
    ) -> ResourceRecord:
        """登记外部 owner 已核实的资源身份，不创建或停止资源。"""
        if kind not in self.EXTERNAL_RESOURCE_KINDS:
            raise ValueError(f"不支持的外部持久资源 kind: {kind}")
        return self.register(
            resource_id=resource_id,
            kind=kind,
            lifetime_scope=lifetime_scope,
            created_by_turn_id=created_by_turn_id,
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

    def settle(self, lease_id: str) -> ResourceLease:
        """结清由资源 owner 核实终态后释放的跨 Turn 占用 lease。

        与 release 的区别：release 表达“某次 Turn/操作结束”，settle 表达
        “资源 owner 已核实资源终态并结清占用”。两者在账本里保留不同状态，
        后续消费方不能把一次 Turn 取消当成资源已终结，也不能把 owner 的结清
        当成普通操作完成。lease 不存在时显式抛错，绝不静默当作已结清。
        """
        lease = self._leases.get(lease_id)
        if lease is None:
            raise KeyError(f"资源 lease 不存在: lease_id={lease_id}")
        lease.status = "settled"
        lease.updated_at = self._now()
        self._persist()
        self._notify(resource_id=lease.resource_id, state="released")
        return self._copy_lease(lease)

    def get_lease(self, lease_id: str) -> ResourceLease | None:
        """按 lease_id 读取占用状态；不存在时返回 None。"""
        lease = self._leases.get(lease_id)
        return self._copy_lease(lease) if lease is not None else None

    def release_turn_leases(self, turn_stream_id: str) -> list[ResourceLease]:
        """释放 Turn 对资源的占用；不改变资源实际状态。"""
        released: list[ResourceLease] = []
        for lease in tuple(self._leases.values()):
            if lease.turn_stream_id != turn_stream_id or lease.status != "active":
                continue
            released.append(self.release(lease.lease_id, reason="turn_cancelled"))
        return released

    def reconcile(
        self,
        observed_status: Mapping[str, str],
    ) -> list[ResourceRecord]:
        """用外部资源清单对崩溃后的 record/lease 做保守对账。

        observed_status 只能来自实际 owner 核实过的身份/状态证据；缺失证据
        的资源标记为 orphaned，其 active lease 进入 reconcile_required，
        绝不虚报为已停止。
        """
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
        for record in reconciled:
            if record.status == "stopped":
                state = "released"
            elif record.status == "orphaned":
                # 登记存在但外部清单无证据：资源不可证实，绝不发布 released。
                state = "unavailable"
            else:
                state = "unknown"
            self._notify(resource_id=record.resource_id, state=state)
        return reconciled

    @property
    def notification_errors(self) -> tuple[str, ...]:
        """最近的有界通知失败摘要；空元组表示所有通知都成功。"""
        return tuple(self._notification_errors)

    def _notify(self, *, resource_id: str, state: str) -> None:
        """发布 owner 已核实的轻量状态；失败显式记录，绝不回滚 durable 事实。"""
        if self._state_events is None:
            return
        if state not in RESOURCE_STATE_STATES:
            raise ValueError(f"resource.state 状态越界: {state!r}")
        try:
            self._state_events.publish(resource_id=resource_id, state=state)
        except (RuntimeError, ValueError) as error:
            summary = f"resource_id={resource_id} state={state} error={error}"
            self._notification_errors.append(summary)
            logger.exception("resource.state 事件发布失败: %s", summary)

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


__all__ = [
    "ExternalResourceLeaseLedger",
    "ResourceLease",
    "ResourceRecord",
]
