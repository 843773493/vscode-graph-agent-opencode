"""Gateway 全局 config event outbox 与其 consumer/relay 投递账本的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``config_events`` 权威 outbox（幂等 append、分页读取、cursor 边界与裁剪）；
- ``config_event_relay_delivery`` 每个 consumer 独立的投递状态（claim /
  delivered / failed，含租约与重试时间）。

``GatewayConfigEventMixin`` 由 :class:`app.gateway.control.gateway_state.
GatewayStateStore` 继承装配；宿主负责 ``_GATEWAY_MIGRATIONS`` 中本族两张表
的 DDL 与迁移序号，本模块只承载读写方法族，宿主提供 ``_database``。其它方法
族在各自事务内通过 ``self._insert_config_event(...)`` 复用本模块的唯一插入
实现（该私有辅助归本族所有）。错误分类沿用 gateway_state 约定：
``ValueError`` 输入形态非法、``KeyError`` 目标事件缺失、
``ConfigConflictError`` claim/幂等冲突、``RuntimeError`` 事务后读取失败、
``ConfigEventCursorGoneError`` 游标已被裁剪。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
    ConfigEventInput,
    ConfigEventRecord,
    ConfigEventRelayState,
    ConfigResult,
    dump_json,
)

__all__ = [
    "GatewayConfigEventMixin",
]


# config_events 的完整行投影：三处读取（单事件、relay 待投递、按域分页）
# 共用同一列清单，新增列时只需改这里。
_CONFIG_EVENT_SELECT = """
SELECT event_seq, event_id, config_domain, candidate_id, attempt_id,
       apply_id, idempotency_key, commit_revision, active_revision,
       pending_revision, source, result, activation_scope,
       changed_paths_json, applied_paths_json, deferred_paths_json,
       error, occurred_at, relay_state, relay_attempts,
       relay_last_error, relay_claimed_by, relay_claimed_until,
       relay_next_attempt_at
FROM config_events
"""


class GatewayConfigEventMixin:
    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ConfigEventRecord:
        def paths(value: object) -> tuple[str, ...]:
            parsed = json.loads(str(value))
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError("Gateway 配置事件路径无效")
            return tuple(parsed)

        def optional_datetime(value: object) -> datetime | None:
            return datetime.fromisoformat(str(value)) if value is not None else None

        return ConfigEventRecord(
            event_seq=int(row[0]),
            event_id=str(row[1]),
            config_domain=str(row[2]),
            candidate_id=str(row[3]) if row[3] is not None else None,
            attempt_id=str(row[4]) if row[4] is not None else None,
            apply_id=str(row[5]) if row[5] is not None else None,
            idempotency_key=str(row[6]) if row[6] is not None else None,
            commit_revision=int(row[7]) if row[7] is not None else None,
            active_revision=int(row[8]) if row[8] is not None else None,
            pending_revision=int(row[9]) if row[9] is not None else None,
            source=str(row[10]),
            result=cast(ConfigResult, str(row[11])),
            activation_scope=cast(str, str(row[12])),
            changed_paths=paths(row[13]),
            applied_paths=paths(row[14]),
            deferred_paths=paths(row[15]),
            error=str(row[16]) if row[16] is not None else None,
            occurred_at=datetime.fromisoformat(str(row[17])),
            relay_state=cast(ConfigEventRelayState, str(row[18])),
            relay_attempts=int(row[19]),
            relay_last_error=str(row[20]) if row[20] is not None else None,
            relay_claimed_by=str(row[21]) if row[21] is not None else None,
            relay_claimed_until=optional_datetime(row[22]),
            relay_next_attempt_at=optional_datetime(row[23]),
        )

    @staticmethod
    def _select_config_event(connection, event_id: str):
        return connection.execute(
            _CONFIG_EVENT_SELECT + "WHERE event_id = ?",
            (event_id,),
        ).fetchone()

    @classmethod
    def _insert_config_event(
        cls,
        connection: sqlite3.Connection,
        event: ConfigEventInput,
    ) -> ConfigEventRecord:
        existing = connection.execute(
            """
            SELECT event_id
            FROM config_events
            WHERE event_id = ?
               OR (
                    config_domain = ? AND idempotency_key = ? AND result = ?
                    AND ? IS NOT NULL
               )
            ORDER BY event_seq ASC
            LIMIT 1
            """,
            (
                event.event_id,
                event.config_domain,
                event.idempotency_key,
                event.result,
                event.idempotency_key,
            ),
        ).fetchone()
        if existing is not None:
            row = cls._select_config_event(connection, str(existing[0]))
            if row is None:
                raise RuntimeError("Gateway 配置事件幂等记录读取失败")
            return cls._event_from_row(row)
        connection.execute(
            """
            INSERT INTO config_events(
                event_id, config_domain, candidate_id, attempt_id, apply_id,
                idempotency_key, commit_revision, active_revision, pending_revision,
                source, result, activation_scope, changed_paths_json,
                applied_paths_json, deferred_paths_json, error, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.config_domain,
                event.candidate_id,
                event.attempt_id,
                event.apply_id,
                event.idempotency_key,
                event.commit_revision,
                event.active_revision,
                event.pending_revision,
                event.source,
                event.result,
                event.activation_scope,
                dump_json(list(event.changed_paths)),
                dump_json(list(event.applied_paths)),
                dump_json(list(event.deferred_paths)),
                event.error,
                utc_now_text(),
            ),
        )
        row = cls._select_config_event(connection, event.event_id)
        if row is None:
            raise RuntimeError("Gateway 配置事件提交后无法读取")
        return cls._event_from_row(row)

    def append_config_event(self, event: ConfigEventInput) -> ConfigEventRecord:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._insert_config_event(connection, event)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_config_events_for_relay(
        self,
        *,
        config_domain: str,
        limit: int = 100,
    ) -> tuple[ConfigEventRecord, ...]:
        """返回尚未确认或已到重试时间的 outbox 事件。"""
        if limit < 1 or limit > 2000:
            raise ValueError("配置 outbox relay 分页参数无效")
        now = utc_now_text()
        connection = self._database.connection()
        try:
            rows = connection.execute(
                _CONFIG_EVENT_SELECT
                + """WHERE config_domain = ? AND (
                    relay_state = 'pending'
                    OR (relay_state = 'failed' AND relay_next_attempt_at <= ?)
                    OR (relay_state = 'claimed' AND relay_claimed_until <= ?)
                )
                ORDER BY event_seq ASC
                LIMIT ?
                """,
                (config_domain, now, now, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def claim_config_events_for_consumer(
        self,
        *,
        config_domain: str,
        after: int,
        consumer_id: str,
        limit: int = 100,
        lease_seconds: float = 30.0,
    ) -> tuple[ConfigEventRecord, ...]:
        """按消费者独立 claim 事件，避免 SSE 消费者相互确认。"""
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Gateway 配置事件 relay 分页参数无效")
        if not consumer_id.strip():
            raise ValueError("Gateway 配置事件 consumer_id 不能为空")
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("Gateway 配置事件 relay lease 必须大于 0")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        claimed_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT e.event_seq, e.event_id, e.config_domain, e.candidate_id,
                       e.attempt_id, e.apply_id, e.idempotency_key,
                       e.commit_revision, e.active_revision, e.pending_revision,
                       e.source, e.result, e.activation_scope,
                       e.changed_paths_json, e.applied_paths_json,
                       e.deferred_paths_json, e.error, e.occurred_at,
                       e.relay_state, e.relay_attempts, e.relay_last_error,
                       e.relay_claimed_by, e.relay_claimed_until,
                       e.relay_next_attempt_at
                FROM config_events AS e
                LEFT JOIN config_event_relay_delivery AS d
                  ON d.event_id = e.event_id AND d.consumer_id = ?
                WHERE e.config_domain = ? AND e.event_seq > ? AND (
                    d.event_id IS NULL
                    OR (d.state = 'failed' AND d.next_attempt_at <= ?)
                    OR (d.state = 'claimed' AND d.claimed_until <= ?)
                )
                ORDER BY e.event_seq ASC
                LIMIT ?
                """,
                (consumer_id, config_domain, after, now_text, now_text, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO config_event_relay_delivery(
                        event_id, consumer_id, state, attempts, last_error,
                        claimed_until, next_attempt_at, updated_at
                    ) VALUES (?, ?, 'claimed', 1, NULL, ?, ?, ?)
                    ON CONFLICT(event_id, consumer_id) DO UPDATE SET
                        state = 'claimed', attempts = attempts + 1,
                        last_error = NULL, claimed_until = excluded.claimed_until,
                        next_attempt_at = excluded.next_attempt_at,
                        updated_at = excluded.updated_at
                    """,
                    (str(row[1]), consumer_id, claimed_until, now_text, now_text),
                )
            connection.execute("COMMIT")
            return tuple(self._event_from_row(row) for row in rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_config_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ) -> ConfigEventRecord:
        if not consumer_id.strip():
            raise ValueError("Gateway 配置事件 consumer_id 不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_event_relay_delivery
                SET state = 'delivered', last_error = NULL,
                    claimed_until = NULL, updated_at = ?
                WHERE event_id = ? AND consumer_id = ?
                  AND state IN ('claimed', 'delivered')
                """,
                (utc_now_text(), event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                raise ConfigConflictError("Gateway 配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"Gateway 配置事件不存在: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_config_event_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
        error: str,
        retry_after_seconds: float = 0.0,
    ) -> ConfigEventRecord:
        if not consumer_id.strip() or not error.strip():
            raise ValueError("Gateway 配置事件 relay consumer 和错误不能为空")
        if isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
            raise ValueError("Gateway 配置事件 relay 重试延迟不能为负数")
        next_attempt_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_event_relay_delivery
                SET state = 'failed', last_error = ?, claimed_until = NULL,
                    next_attempt_at = ?, updated_at = ?
                WHERE event_id = ? AND consumer_id = ? AND state = 'claimed'
                """,
                (error, next_attempt_at, utc_now_text(), event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                raise ConfigConflictError("Gateway 配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"Gateway 配置事件不存在: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_config_event_relay(
        self,
        *,
        event_id: str,
        consumer_id: str,
        lease_seconds: float = 30.0,
    ) -> ConfigEventRecord | None:
        """以租约 claim 单个 outbox 事件；过期 claim 可被恢复者接管。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("配置 outbox relay lease 必须大于 0")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        claimed_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'claimed',
                    relay_attempts = relay_attempts + 1,
                    relay_last_error = NULL,
                    relay_claimed_by = ?,
                    relay_claimed_until = ?
                WHERE event_id = ? AND (
                    relay_state = 'pending'
                    OR (relay_state = 'failed' AND relay_next_attempt_at <= ?)
                    OR (relay_state = 'claimed' AND relay_claimed_until <= ?)
                )
                """,
                (consumer_id, claimed_until, event_id, now_text, now_text),
            )
            if cursor.rowcount != 1:
                connection.execute("COMMIT")
                return None
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise RuntimeError(f"配置 outbox claim 后事件消失: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_config_event_relay_delivered(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ) -> ConfigEventRecord:
        """以 event_id 幂等确认 relay；重复确认不会生成第二条事件。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'delivered',
                    relay_last_error = NULL,
                    relay_claimed_by = NULL,
                    relay_claimed_until = NULL
                WHERE event_id = ? AND relay_state = 'claimed'
                  AND relay_claimed_by = ?
                """,
                (event_id, consumer_id),
            )
            relay_claimed = cursor.rowcount != 0
            row = self._select_config_event(connection, event_id)
            if row is None:
                if relay_claimed:
                    raise RuntimeError(f"配置 outbox 确认后事件消失: {event_id}")
                raise KeyError(f"配置 outbox 事件不存在: {event_id}")
            record = self._event_from_row(row)
            if not relay_claimed and record.relay_state != "delivered":
                raise ConfigConflictError("配置 outbox relay claim 不属于当前 consumer")
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_config_event_relay(
        self,
        *,
        event_id: str,
        consumer_id: str,
        error: str,
        retry_after_seconds: float = 0.0,
    ) -> ConfigEventRecord:
        """记录 relay 失败并安排重试，保持原 event_id/cursor 不变。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        if not error.strip():
            raise ValueError("配置 outbox relay 错误不能为空")
        if isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
            raise ValueError("配置 outbox relay 重试延迟不能为负数")
        next_attempt_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'failed',
                    relay_last_error = ?,
                    relay_claimed_by = NULL,
                    relay_claimed_until = NULL,
                    relay_next_attempt_at = ?
                WHERE event_id = ? AND relay_state = 'claimed'
                  AND relay_claimed_by = ?
                """,
                (error, next_attempt_at, event_id, consumer_id),
            )
            relay_claimed = cursor.rowcount != 0
            row = self._select_config_event(connection, event_id)
            if row is None:
                if relay_claimed:
                    raise RuntimeError(f"配置 outbox 失败记录后事件消失: {event_id}")
                raise KeyError(f"配置 outbox 事件不存在: {event_id}")
            record = self._event_from_row(row)
            if not relay_claimed and record.relay_state != "delivered":
                raise ConfigConflictError("配置 outbox relay claim 不属于当前 consumer")
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_config_events(
        self, *, config_domain: str, after: int = 0, limit: int = 100
    ):
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Gateway 配置事件分页参数无效")
        self.ensure_config_event_cursor(config_domain=config_domain, after=after)
        connection = self._database.connection()
        try:
            rows = connection.execute(
                _CONFIG_EVENT_SELECT
                + """WHERE config_domain = ? AND event_seq > ?
                ORDER BY event_seq ASC LIMIT ?
                """,
                (config_domain, after, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def config_event_bounds(self, *, config_domain: str) -> tuple[int | None, int]:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT MIN(event_seq), MAX(event_seq)
                FROM config_events WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'config_events'"
            ).fetchone()
            return (
                int(row[0]) if row[0] is not None else None,
                int(sequence_row[0]) if sequence_row is not None else 0,
            )
        finally:
            connection.close()

    def ensure_config_event_cursor(self, *, config_domain: str, after: int) -> None:
        if after < 0:
            raise ValueError("Gateway 配置事件游标不能为负数")
        first, _ = self.config_event_bounds(config_domain=config_domain)
        if after > 0 and first is not None and after < first - 1:
            raise ConfigEventCursorGoneError(
                config_domain=config_domain,
                after=after,
                first=first,
            )

    def prune_config_events(
        self, *, config_domain: str, retention_days: int = 30
    ) -> int:
        if retention_days < 1:
            raise ValueError("Gateway 配置事件保留天数必须大于 0")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        connection = self._database.connection()
        try:
            cursor = connection.execute(
                "DELETE FROM config_events WHERE config_domain = ? AND occurred_at < ?",
                (config_domain, cutoff),
            )
            return int(cursor.rowcount)
        finally:
            connection.close()
