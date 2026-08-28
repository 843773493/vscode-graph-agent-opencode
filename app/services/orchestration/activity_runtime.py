from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from app.services.infrastructure.message_stream_store import MessageStreamWriter

logger = logging.getLogger(__name__)
ActivityResult = TypeVar("ActivityResult")


class ActivityHandler(Protocol):
    """Activity 语义 Handler，只负责扩展投影和清理判断。"""

    def normalize(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def snapshot_detail(self, payload: Mapping[str, object]) -> Mapping[str, object] | None: ...

    def can_confirm_stop(self, payload: Mapping[str, object]) -> bool: ...


@dataclass(frozen=True, slots=True)
class ActivityCapabilities:
    detail: bool = False
    progress: bool = False
    cancellation: bool = False
    recovery: bool = False


@dataclass(frozen=True, slots=True)
class RegisteredActivityHandler:
    handler: ActivityHandler
    capabilities: ActivityCapabilities


class DefaultActivityHandler:
    """未注册 kind 的保守 Handler，丢弃 provider 私有细节。"""

    def normalize(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        allowed = {
            key: payload[key]
            for key in (
                "activity_id",
                "kind",
                "parent_activity_id",
                "scope_ref",
                "status",
                "outcome",
                "summary",
                "cancellable",
                "resumable",
                "side_effect_policy",
                "resource_refs",
                "detail_ref",
            )
            if key in payload
        }
        allowed["detail_available"] = False
        return allowed

    def snapshot_detail(self, payload: Mapping[str, object]) -> None:
        return None

    def can_confirm_stop(self, payload: Mapping[str, object]) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SafeDetailActivityHandler(DefaultActivityHandler):
    """为已知 Activity kind 保留白名单 detail，不把业务正文透传到前端。"""

    allowed_detail_keys: frozenset[str]

    def normalize(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        normalized = dict(DefaultActivityHandler.normalize(self, payload))
        raw_detail = payload.get("detail")
        if isinstance(raw_detail, Mapping):
            detail = {
                key: raw_detail[key]
                for key in self.allowed_detail_keys
                if key in raw_detail
            }
            if detail:
                normalized["detail"] = detail
                normalized["detail_available"] = True
        return normalized

    def snapshot_detail(self, payload: Mapping[str, object]) -> Mapping[str, object] | None:
        detail = self.normalize(payload).get("detail")
        return dict(detail) if isinstance(detail, Mapping) else None


class ActivityHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, RegisteredActivityHandler] = {}
        self._default = RegisteredActivityHandler(
            handler=DefaultActivityHandler(),
            capabilities=ActivityCapabilities(),
        )
        self._register_builtin_handlers()

    def _register_builtin_handlers(self) -> None:
        self.register(
            kind="context.compaction",
            handler=SafeDetailActivityHandler(
                frozenset({"phase", "summarized_message_count", "retained_message_count"})
            ),
            capabilities=ActivityCapabilities(detail=True, progress=True, recovery=True),
        )
        self.register(
            kind="approval.wait",
            handler=SafeDetailActivityHandler(
                frozenset({"approval_id", "expires_at", "required_action"})
            ),
            capabilities=ActivityCapabilities(detail=True, progress=True, cancellation=True),
        )
        self.register(
            kind="subagent.run",
            handler=SafeDetailActivityHandler(
                frozenset({"child_turn_id", "child_stream_id", "agent_id", "phase"})
            ),
            capabilities=ActivityCapabilities(detail=True, progress=True, recovery=True),
        )
        self.register(
            kind="resource.operation",
            handler=SafeDetailActivityHandler(
                frozenset({"resource_id", "operation", "phase"})
            ),
            capabilities=ActivityCapabilities(
                detail=True,
                progress=True,
                cancellation=True,
                recovery=True,
            ),
        )

    def register(
        self,
        *,
        kind: str,
        handler: ActivityHandler,
        capabilities: ActivityCapabilities,
    ) -> None:
        if not kind:
            raise ValueError("Activity Handler 注册缺少 kind")
        if kind in self._handlers:
            raise ValueError(f"Activity Handler 重复注册: kind={kind}")
        self._handlers[kind] = RegisteredActivityHandler(handler, capabilities)

    def resolve(self, kind: str) -> RegisteredActivityHandler:
        return self._handlers.get(kind, self._default)


@dataclass(slots=True)
class ActivityRuntime:
    writer: MessageStreamWriter
    registry: ActivityHandlerRegistry

    async def started(
        self,
        *,
        activity_id: str,
        kind: str,
        scope_ref: str = "turn",
        summary: str | None = None,
        parent_activity_id: str | None = None,
        cancellable: bool = False,
        resumable: bool = False,
        side_effect_policy: str = "unknown",
        resource_refs: tuple[str, ...] = (),
        detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "activity_id": activity_id,
            "kind": kind,
            "scope_ref": scope_ref,
            "status": "running",
            "cancellable": cancellable,
            "resumable": resumable,
            "side_effect_policy": side_effect_policy,
            "resource_refs": list(resource_refs),
            "updated_at": self._now(),
        }
        if summary is not None:
            payload["summary"] = summary
        if parent_activity_id is not None:
            payload["parent_activity_id"] = parent_activity_id
        if detail is not None:
            payload["detail"] = dict(detail)
        payload = self._normalize(kind, payload)
        return await self.writer.commit("activity.started", payload)

    async def updated(
        self,
        *,
        activity_id: str,
        kind: str,
        status: str = "running",
        summary: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if status not in {"running", "waiting", "stopping"}:
            raise ValueError(f"Activity updated 不接受终态: status={status}")
        payload: dict[str, object] = {
            "activity_id": activity_id,
            "kind": kind,
            "status": status,
            "updated_at": self._now(),
        }
        if summary is not None:
            payload["summary"] = summary
        if detail is not None:
            payload["detail"] = dict(detail)
        payload = self._normalize(kind, payload)
        return await self.writer.commit("activity.updated", payload)

    async def completed(
        self,
        *,
        activity_id: str,
        kind: str,
        outcome: str = "success",
        summary: str | None = None,
    ) -> dict[str, object]:
        if outcome not in {
            "success",
            "user_interrupt",
            "outcome_unknown",
        }:
            raise ValueError(f"Activity completed outcome 不合法: {outcome}")
        payload: dict[str, object] = {
            "activity_id": activity_id,
            "kind": kind,
            "status": "completed",
            "outcome": outcome,
            "updated_at": self._now(),
        }
        if summary is not None:
            payload["summary"] = summary
        return await self.writer.commit("activity.completed", self._normalize(kind, payload))

    async def failed(
        self,
        *,
        activity_id: str,
        kind: str,
        outcome: str = "execution_lost",
        summary: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if outcome not in {
            "provider_error",
            "execution_lost",
            "outcome_unknown",
            "user_interrupt",
        }:
            raise ValueError(f"Activity failed outcome 不合法: {outcome}")
        payload: dict[str, object] = {
            "activity_id": activity_id,
            "kind": kind,
            "status": "failed",
            "outcome": outcome,
            "updated_at": self._now(),
        }
        if summary is not None:
            payload["summary"] = summary
        if detail is not None:
            payload["detail"] = dict(detail)
        return await self.writer.commit("activity.failed", self._normalize(kind, payload))

    async def run(
        self,
        *,
        activity_id: str,
        kind: str,
        operation: Callable[[], Awaitable[ActivityResult]],
        summary: str | None = None,
        resource_refs: tuple[str, ...] = (),
        resumable: bool = False,
    ) -> ActivityResult:
        """统一包装可细化路径；失败事实先写入消息流再向调用方抛错。"""
        await self.started(
            activity_id=activity_id,
            kind=kind,
            summary=summary,
            resource_refs=resource_refs,
            resumable=resumable,
        )
        try:
            result = await operation()
        except Exception as error:
            await self.failed(
                activity_id=activity_id,
                kind=kind,
                outcome="outcome_unknown",
                summary=str(error),
            )
            raise
        await self.completed(
            activity_id=activity_id,
            kind=kind,
            summary=summary,
        )
        return result

    def _normalize(self, kind: str, payload: Mapping[str, object]) -> dict[str, object]:
        registered = self.registry.resolve(kind)
        try:
            normalized = dict(registered.handler.normalize(payload))
        except Exception as error:
            logger.exception("Activity Handler normalize 失败，降级通用投影: kind=%s", kind)
            normalized = dict(DefaultActivityHandler().normalize(payload))
            normalized["detail_available"] = False
            normalized["detail_error"] = str(error)
        normalized.setdefault("kind", kind)
        normalized.setdefault("updated_at", self._now())
        return normalized

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
