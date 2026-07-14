from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from app.schemas.event import Event


DurableEventListener = Callable[[Event], Awaitable[None]]


class EventSubscriberOverflowError(RuntimeError):
    """临时订阅者消费过慢，事件总线已关闭该订阅。"""

    def __init__(
        self,
        *,
        subscription_id: str,
        subscriber_kind: str,
        job_id: str,
        event_type: str,
        max_queue_size: int,
    ) -> None:
        self.subscription_id = subscription_id
        self.subscriber_kind = subscriber_kind
        self.job_id = job_id
        self.event_type = event_type
        self.max_queue_size = max_queue_size
        super().__init__(
            "事件订阅者消费速度不足，订阅已关闭: "
            f"subscription_id={subscription_id} subscriber_kind={subscriber_kind} "
            f"job_id={job_id} event_type={event_type} max_queue_size={max_queue_size}"
        )


class EventSubscriptionProtocol(Protocol):
    """临时事件订阅；溢出后 get 会抛出 EventSubscriberOverflowError。"""

    @property
    def subscription_id(self) -> str: ...

    @property
    def subscriber_kind(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, str]: ...

    async def get(self) -> Event: ...


@runtime_checkable
class JobEventBusProtocol(Protocol):
    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        step_id: str | None = None,
        agent_id: str | None = None,
    ) -> Event: ...

    async def subscribe(
        self,
        job_id: str,
        *,
        subscriber_kind: str,
        metadata: Mapping[str, str] | None = None,
        event_types: frozenset[str] | None = None,
    ) -> EventSubscriptionProtocol: ...

    async def unsubscribe(
        self,
        job_id: str,
        subscription: EventSubscriptionProtocol,
        *,
        reason: str,
    ) -> None: ...

    async def register_durable_listener(self, listener: DurableEventListener) -> None: ...

    async def unregister_durable_listener(self, listener: DurableEventListener) -> None: ...

    async def list_events(self, job_id: str, after: str | None = None, limit: int = 20) -> list[Event]: ...

    async def get_event(self, event_id: str) -> Event | None: ...
