from __future__ import annotations

from app.services.infrastructure.config.state import ConfigEventRecord
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class ConfigEventCursor:
    """承载 Workspace 配置事件的游标族读写（列表、消费者认领与投递确认）。"""

    def __init__(
        self,
        *,
        store: WorkspaceStateStore | None,
        config_domain: str,
    ) -> None:
        self._store = store
        self._config_domain = config_domain

    def list_config_events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[ConfigEventRecord, ...]:
        """按游标重放配置事件，游标越界时由状态库抛出 CursorGone。"""

        if self._store is None:
            return ()
        return self._store.list_config_events(
            config_domain=self._config_domain,
            after=after,
            limit=limit,
        )

    def claim_config_events_for_consumer(
        self,
        *,
        after: int,
        consumer_id: str,
        limit: int = 100,
    ) -> tuple[ConfigEventRecord, ...]:
        """按消费者独立认领事件，避免一个消费者的确认导致其他消费者丢事件。"""

        if self._store is None:
            return ()
        return self._store.claim_config_events_for_consumer(
            config_domain=self._config_domain,
            after=after,
            consumer_id=consumer_id,
            limit=limit,
        )

    def mark_config_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ) -> ConfigEventRecord:
        """确认单个事件对某消费者已投递；重复确认必须保持幂等。"""

        if self._store is None:
            raise RuntimeError("当前 Workspace 没有配置事件状态库")
        return self._store.mark_config_event_delivered_for_consumer(
            event_id=event_id,
            consumer_id=consumer_id,
        )

    def ensure_config_event_cursor(self, *, after: int) -> None:
        """校验游标仍在保留窗口内，越界时抛出 CursorGone。"""

        if self._store is None:
            return
        self._store.ensure_config_event_cursor(
            config_domain=self._config_domain,
            after=after,
        )
