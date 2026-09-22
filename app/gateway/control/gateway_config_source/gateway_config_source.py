"""Gateway config source layer、source journal 与 generation high water mark 的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``config_source_layers`` 权威 layer 行（去重同步、CAS、上一版快照与备份路径）；
- ``config_source_journal`` 事件账本与 ``config_source_owner`` generation 水位；
- ``source_generation_high_water_mark`` 只读水位查询。

``GatewayConfigSourceMixin`` 由 :class:`app.gateway.control.gateway_state.
GatewayStateStore` 继承装配；宿主负责 ``_GATEWAY_MIGRATIONS`` 中本族三张表的
DDL 与迁移序号，本模块只承载读写方法族，宿主提供 ``_database``。错误分类沿用
gateway_state 约定：``ValueError`` 输入形态非法、``ConfigConflictError``
revision/digest/generation CAS 冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigSourceJournalRecord,
    ConfigSourceLayerRecord,
    dump_json,
    load_json_object,
    prepare_config_for_persistence,
)

__all__ = [
    "GatewayConfigSourceMixin",
]


# config_source_journal 的完整行投影：按 event_id 反查、去重后回读与按
# (source_key, generation) 读取三处共用同一列清单，新增列时只需改这里。
_SOURCE_JOURNAL_SELECT = """
SELECT source_key, source_generation, source_event_id, source_path,
       presence, layer_revision, layer_digest, previous_digest,
       origin, fanout_id, created_at
FROM config_source_journal
"""


class GatewayConfigSourceMixin:
    def get_source_layer(self, config_key: str) -> ConfigSourceLayerRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, source_path, presence, config_version,
                       payload_json, layer_revision, layer_digest,
                       source_generation, previous_digest, updated_at,
                       previous_payload_json, backup_path
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ConfigSourceLayerRecord(
            config_key=str(row[0]),
            source_path=str(row[1]),
            presence=cast(str, row[2]),
            config_version=int(row[3]),
            payload=(
                load_json_object(str(row[4]), field="Gateway source layer payload")
                if row[4] is not None
                else None
            ),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            source_generation=int(row[7]),
            previous_digest=str(row[8]) if row[8] is not None else None,
            updated_at=datetime.fromisoformat(str(row[9])),
            previous_payload=(
                load_json_object(
                    str(row[10]), field="Gateway source layer previous payload"
                )
                if row[10] is not None
                else None
            ),
            backup_path=str(row[11]) if row[11] is not None else None,
        )

    def sync_config_source(
        self,
        *,
        config_key: str,
        source_path: Path,
        config_version: int,
        presence: str,
        payload: dict[str, object] | None,
        layer_digest: str | None,
        expected_layer_revision: int | None = None,
        expected_layer_digest: str | None = None,
        backup_path: Path | None = None,
        journal_origin: str | None = None,
        source_event_id: str | None = None,
        fanout_id: str | None = None,
    ) -> ConfigSourceLayerRecord:
        """写入/去重一条 source layer，并可选在同一事务后回读。

        ``backup_path`` 指向的备份文件由调用方在事务之前写好（见 config_service 的
        ``*.migrated.bak`` / ``*.deleted.bak``）；本方法只把路径记进 layer 行。备份名
        对每个配置路径是确定的、且写入端有 ``if not exists`` 守卫，因此崩溃最多留下
        一个可被下次同步复用的孤儿备份，不会无界累积，无需额外回收机制。
        """

        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 Gateway source layer presence: {presence}")
        if presence == "present" and payload is None:
            raise ValueError("Gateway present source layer 必须有 payload")
        if presence == "absent" and payload is not None:
            raise ValueError("Gateway absent source layer 的 payload 必须为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT source_path, presence, layer_revision, layer_digest,
                       source_generation, payload_json
                FROM config_source_layers WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if current is None:
                if (
                    expected_layer_revision is not None
                    or expected_layer_digest is not None
                ):
                    raise ConfigConflictError("Gateway source layer 初始 CAS 冲突")
                revision, generation, previous = 1, 1, None
                previous_payload_json = None
            else:
                current_path = str(current[0])
                current_presence = str(current[1])
                revision_now = int(current[2])
                digest_now = str(current[3]) if current[3] is not None else None
                if (
                    expected_layer_revision is not None
                    and revision_now != expected_layer_revision
                ) or (
                    expected_layer_digest is not None
                    and digest_now != expected_layer_digest
                ):
                    raise ConfigConflictError(
                        "Gateway source layer CAS 冲突: "
                        f"key={config_key}, revision={revision_now}, digest={digest_now}"
                    )
                if (
                    current_path == str(source_path.expanduser().resolve())
                    and current_presence == presence
                    and digest_now == layer_digest
                ):
                    sanitized_json = (
                        dump_json(prepare_config_for_persistence(payload))
                        if payload is not None
                        else None
                    )
                    now = utc_now_text()
                    connection.execute(
                        """
                        UPDATE config_source_layers
                        SET config_version = ?, payload_json = ?, updated_at = ?
                        WHERE config_key = ?
                        """,
                        (config_version, sanitized_json, now, config_key),
                    )
                    if presence == "present":
                        connection.execute(
                            """
                            INSERT INTO gateway_config(
                                config_key, config_version, payload_json, updated_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(config_key) DO UPDATE SET
                                config_version=excluded.config_version,
                                payload_json=excluded.payload_json,
                                updated_at=excluded.updated_at
                            """,
                            (config_key, config_version, sanitized_json, now),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM gateway_config WHERE config_key = ?",
                            (config_key,),
                        )
                    if journal_origin is not None:
                        self._append_config_source_journal_in_connection(
                            connection,
                            source_key=config_key,
                            source_event_id=(
                                source_event_id or f"{config_key}:layer:{revision_now}"
                            ),
                            source_path=source_path,
                            presence=presence,
                            layer_revision=revision_now,
                            layer_digest=layer_digest,
                            previous_digest=digest_now,
                            origin=journal_origin,
                            fanout_id=(
                                fanout_id
                                or f"fanout:{config_key}:event:{config_key}:layer:{revision_now}"
                            ),
                        )
                    connection.execute("COMMIT")
                    record = self.get_source_layer(config_key)
                    if record is None:
                        raise RuntimeError("Gateway source layer 去重后无法读取")
                    return record
                revision, generation, previous = (
                    revision_now + 1,
                    int(current[4]) + 1,
                    digest_now,
                )
                previous_payload_json = current[5]
            now = utc_now_text()
            payload_json = (
                dump_json(prepare_config_for_persistence(payload))
                if payload is not None
                else None
            )
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, source_path, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json, backup_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    source_path=excluded.source_path,
                    presence=excluded.presence,
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    source_generation=excluded.source_generation,
                    previous_digest=excluded.previous_digest,
                    updated_at=excluded.updated_at,
                    previous_payload_json=excluded.previous_payload_json,
                    backup_path=excluded.backup_path
                """,
                (
                    config_key,
                    str(source_path.expanduser().resolve()),
                    presence,
                    config_version,
                    payload_json,
                    revision,
                    layer_digest,
                    generation,
                    previous,
                    now,
                    previous_payload_json,
                    str(backup_path.expanduser().resolve())
                    if backup_path is not None
                    else None,
                ),
            )
            if journal_origin is not None:
                self._append_config_source_journal_in_connection(
                    connection,
                    source_key=config_key,
                    source_event_id=(
                        source_event_id or f"{config_key}:layer:{revision}"
                    ),
                    source_path=source_path,
                    presence=presence,
                    layer_revision=revision,
                    layer_digest=layer_digest,
                    previous_digest=previous,
                    origin=journal_origin,
                    fanout_id=(
                        fanout_id
                        or f"fanout:{config_key}:event:{config_key}:layer:{revision}"
                    ),
                )
            if presence == "present":
                connection.execute(
                    """
                    INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        config_key,
                        config_version,
                        dump_json(prepare_config_for_persistence(payload)),
                        now,
                    ),
                )
            else:
                connection.execute(
                    "DELETE FROM gateway_config WHERE config_key = ?",
                    (config_key,),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError("Gateway source layer 提交后无法读取")
        return record

    @staticmethod
    def _advance_source_owner_generation(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        generation: int,
    ) -> None:
        """同一事务内把 owner 水位推进到下一 generation；越级即 fail closed。"""

        owner = connection.execute(
            "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if owner is not None and int(owner[0]) != generation:
            raise ConfigConflictError("Gateway source owner generation CAS 冲突")
        if owner is None:
            connection.execute(
                "INSERT INTO config_source_owner(source_key, next_generation) VALUES (?, ?)",
                (source_key, generation + 1),
            )
        else:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = ? WHERE source_key = ?",
                (generation + 1, source_key),
            )

    @staticmethod
    def _append_config_source_journal_in_connection(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> int:
        """在调用方事务内追加 source journal，避免 source/journal 裂脑。"""
        if presence not in {"present", "absent"}:
            raise ValueError("Gateway source journal presence 无效")
        existing = connection.execute(
            """
            SELECT source_key, source_generation, layer_revision, layer_digest,
                   presence
            FROM config_source_journal
            WHERE source_event_id = ?
            """,
            (source_event_id,),
        ).fetchone()
        if existing is not None:
            existing_digest = str(existing[3]) if existing[3] is not None else None
            if (
                str(existing[0]) != source_key
                or int(existing[2]) != layer_revision
                or existing_digest != layer_digest
                or str(existing[4]) != presence
            ):
                raise ConfigConflictError(
                    "Gateway source journal event_id 已绑定不同 source 记录"
                )
            return int(existing[1])
        latest = connection.execute(
            """
            SELECT source_generation, presence, layer_digest
            FROM config_source_journal
            WHERE source_key = ?
            ORDER BY source_generation DESC
            LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        current_generation = int(latest[0]) if latest is not None else 0
        if (
            expected_source_generation is not None
            and current_generation != expected_source_generation
        ):
            raise ConfigConflictError("Gateway source journal generation CAS 冲突")
        if (
            latest is not None
            and str(latest[1]) == presence
            and (str(latest[2]) if latest[2] is not None else None) == layer_digest
        ):
            return current_generation
        generation = current_generation + 1
        GatewayConfigSourceMixin._advance_source_owner_generation(
            connection, source_key=source_key, generation=generation
        )
        connection.execute(
            """
            INSERT INTO config_source_journal(
                source_key, source_generation, source_event_id, source_path,
                presence, layer_revision, layer_digest, previous_digest,
                origin, fanout_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                generation,
                source_event_id,
                str(source_path.expanduser().resolve()),
                presence,
                layer_revision,
                layer_digest,
                previous_digest,
                origin,
                fanout_id,
                utc_now_text(),
            ),
        )
        return generation

    def append_config_source_journal(
        self,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> ConfigSourceJournalRecord:
        if presence not in {"present", "absent"}:
            raise ValueError("Gateway source journal presence 无效")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                _SOURCE_JOURNAL_SELECT + "WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._source_journal_from_row(existing)
            latest = connection.execute(
                """
                SELECT source_generation, presence, layer_digest
                FROM config_source_journal WHERE source_key = ?
                ORDER BY source_generation DESC LIMIT 1
                """,
                (source_key,),
            ).fetchone()
            current_generation = int(latest[0]) if latest is not None else 0
            if (
                expected_source_generation is not None
                and current_generation != expected_source_generation
            ):
                raise ConfigConflictError("Gateway source journal generation CAS 冲突")
            if (
                latest is not None
                and str(latest[1]) == presence
                and (str(latest[2]) if latest[2] is not None else None) == layer_digest
            ):
                existing = connection.execute(
                    _SOURCE_JOURNAL_SELECT
                    + "WHERE source_key = ? AND source_generation = ?",
                    (source_key, current_generation),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Gateway source journal 去重后无法读取")
                connection.execute("COMMIT")
                return self._source_journal_from_row(existing)
            generation = current_generation + 1
            self._advance_source_owner_generation(
                connection, source_key=source_key, generation=generation
            )
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO config_source_journal(
                    source_key, source_generation, source_event_id, source_path,
                    presence, layer_revision, layer_digest, previous_digest,
                    origin, fanout_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    generation,
                    source_event_id,
                    str(source_path.expanduser().resolve()),
                    presence,
                    layer_revision,
                    layer_digest,
                    previous_digest,
                    origin,
                    fanout_id,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        row = self._latest_source_journal_row(source_key, generation)
        if row is None:
            raise RuntimeError("Gateway source journal 提交后无法读取")
        return self._source_journal_from_row(row)

    @staticmethod
    def _source_journal_from_row(row: sqlite3.Row) -> ConfigSourceJournalRecord:
        return ConfigSourceJournalRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            source_event_id=str(row[2]),
            source_path=str(row[3]),
            presence=cast(str, row[4]),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            previous_digest=str(row[7]) if row[7] is not None else None,
            origin=str(row[8]),
            fanout_id=str(row[9]),
            created_at=datetime.fromisoformat(str(row[10])),
        )

    def _latest_source_journal_row(self, source_key: str, generation: int):
        connection = self._database.connection()
        try:
            return connection.execute(
                _SOURCE_JOURNAL_SELECT
                + "WHERE source_key = ? AND source_generation = ?",
                (source_key, generation),
            ).fetchone()
        finally:
            connection.close()

    def source_generation_high_water_mark(self, *, source_key: str) -> int:
        """返回本 source 已提交的最大 journal generation；从未写入时为 0。

        ``config_source_owner.next_generation`` 与 journal 在同一事务推进，合法写入
        至少为 ``generation + 1``（首个事件后为 2，因此高水位至少为 1）。这里对两种
        不可能由软件产生的形态响亮报错，绝不返回虚假默认值：

        - owner 行缺失但 journal 已有 generation：有人绕过软件清空了水位行；
        - owner 行存在但 ``next_generation < 1``：水位本身已损坏（会算出负值高水位，
          让 CAS 永远失配）。
        """

        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                journal_row = connection.execute(
                    "SELECT MAX(source_generation) FROM config_source_journal "
                    "WHERE source_key = ?",
                    (source_key,),
                ).fetchone()
                if journal_row is not None and journal_row[0] is not None:
                    raise RuntimeError(
                        "Gateway source owner 水位缺失但 journal 已有 generation；"
                        "检测到绕过软件直接修改 Gateway 配置来源状态: "
                        f"source_key={source_key}"
                    )
                return 0
            next_generation = int(row[0])
            if next_generation < 1:
                raise RuntimeError(
                    "Gateway source owner next_generation 非法: "
                    f"source_key={source_key}, next_generation={next_generation}"
                )
        finally:
            connection.close()
        return next_generation - 1
