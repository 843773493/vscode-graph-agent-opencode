"""config_source_layers 权威 layer 行的读写方法族。

``WorkspaceConfigSourceLayerMixin`` 只承载本族方法，由 ``WorkspaceConfigSourceMixin``
组合装配；依赖宿主提供的 ``_database``。
"""

from __future__ import annotations

from datetime import datetime

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigSourceLayerRecord,
    dump_json,
    load_json_object,
    prepare_config_for_persistence,
)

__all__ = ["WorkspaceConfigSourceLayerMixin"]


class WorkspaceConfigSourceLayerMixin:
    def get_source_layer(self, config_key: str) -> ConfigSourceLayerRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, vrn, presence, config_version,
                       payload_json, layer_revision, layer_digest,
                       source_generation, previous_digest, updated_at,
                       previous_payload_json
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = (
                load_json_object(str(row[4]), field="source layer payload")
                if row[4] is not None
                else None
            )
            return ConfigSourceLayerRecord(
                config_key=str(row[0]),
                vrn=str(row[1]) if row[1] is not None else None,
                presence=str(row[2]),  # type: ignore[arg-type]
                config_version=int(row[3]),
                payload=payload,
                layer_revision=int(row[5]),
                layer_digest=str(row[6]) if row[6] is not None else None,
                source_generation=int(row[7]),
                previous_digest=str(row[8]) if row[8] is not None else None,
                updated_at=datetime.fromisoformat(str(row[9])),
                previous_payload=(
                    load_json_object(
                        str(row[10]), field="source layer previous payload"
                    )
                    if row[10] is not None
                    else None
                ),
            )
        finally:
            connection.close()

    def sync_config_source(
        self,
        *,
        config_key: str,
        vrn: str | None,
        config_version: int,
        presence: str,
        payload: dict[str, object] | None,
        layer_digest: str | None,
        expected_layer_revision: int | None = None,
        expected_layer_digest: str | None = None,
        journal_origin: str | None = None,
        source_event_id: str | None = None,
        fanout_id: str | None = None,
        config_domain: str | None = None,
        expected_active_revision: int | None = None,
        expected_active_digest: str | None = None,
        enforce_layer_cas: bool = False,
        enforce_active_cas: bool = False,
    ) -> ConfigSourceLayerRecord:
        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 source layer presence: {presence}")
        if presence == "present" and payload is None:
            raise ValueError("present source layer 必须有 payload")
        if presence == "absent" and payload is not None:
            raise ValueError("absent source layer 的 payload 必须为空")
        if (expected_active_revision is None) != (expected_active_digest is None):
            raise ValueError("active CAS 必须同时提供 revision 和 digest")
        if (enforce_active_cas or expected_active_revision is not None) and not config_domain:
            raise ValueError("active CAS 必须声明 config_domain")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if enforce_active_cas or expected_active_revision is not None:
                active = connection.execute(
                    """
                    SELECT active_revision, effective_digest
                    FROM config_active_snapshot
                    WHERE config_domain = ?
                    """,
                    (config_domain,),
                ).fetchone()
                current_active_revision = int(active[0]) if active is not None else None
                current_active_digest = str(active[1]) if active is not None else None
                if (
                    current_active_revision != expected_active_revision
                    or current_active_digest != expected_active_digest
                ):
                    raise ConfigConflictError(
                        "active snapshot CAS 冲突: "
                        f"domain={config_domain}, "
                        f"current_revision={current_active_revision}, "
                        f"current_digest={current_active_digest}"
                    )
            row = connection.execute(
                """
                SELECT vrn, presence, layer_revision, layer_digest,
                       source_generation, payload_json
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                if expected_layer_revision is not None or expected_layer_digest is not None:
                    raise ConfigConflictError(
                        f"source layer 不存在但调用方声明了旧基线: key={config_key}"
                    )
                layer_revision = 1
                source_generation = 1
                previous_digest = None
                previous_payload_json = None
            else:
                current_vrn = str(row[0]) if row[0] is not None else None
                current_presence = str(row[1])
                current_revision = int(row[2])
                current_digest = str(row[3]) if row[3] is not None else None
                if (
                    (enforce_layer_cas or expected_layer_revision is not None)
                    and current_revision != expected_layer_revision
                ) or (
                    (enforce_layer_cas or expected_layer_digest is not None)
                    and current_digest != expected_layer_digest
                ):
                    raise ConfigConflictError(
                        "source layer CAS 冲突: "
                        f"key={config_key}, current_revision={current_revision}, "
                        f"current_digest={current_digest}"
                    )
                if (
                    current_vrn == vrn
                    and current_presence == presence
                    and current_digest == layer_digest
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
                    if journal_origin is not None:
                        self._append_config_source_journal_in_connection(
                            connection,
                            source_key=config_key,
                            source_event_id=(
                                source_event_id
                                or f"{config_key}:layer:{current_revision}"
                            ),
                            vrn=vrn,
                            presence=presence,
                            layer_revision=current_revision,
                            layer_digest=layer_digest,
                            previous_digest=current_digest,
                            origin=journal_origin,
                            fanout_id=(
                                fanout_id
                                or f"fanout:{config_key}:event:{config_key}:layer:{current_revision}"
                            ),
                        )
                    connection.execute("COMMIT")
                    record = self.get_source_layer(config_key)
                    if record is None:
                        raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
                    return record
                layer_revision = current_revision + 1
                source_generation = int(row[4]) + 1
                previous_digest = current_digest
                previous_payload_json = row[5]

            payload_json = (
                dump_json(prepare_config_for_persistence(payload))
                if payload is not None
                else None
            )
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, vrn, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    vrn=excluded.vrn,
                    presence=excluded.presence,
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    source_generation=excluded.source_generation,
                    previous_digest=excluded.previous_digest,
                    updated_at=excluded.updated_at,
                    previous_payload_json=excluded.previous_payload_json
                """,
                (
                    config_key,
                    vrn,
                    presence,
                    config_version,
                    payload_json,
                    layer_revision,
                    layer_digest,
                    source_generation,
                    previous_digest,
                    now,
                    previous_payload_json,
                ),
            )
            if journal_origin is not None:
                self._append_config_source_journal_in_connection(
                    connection,
                    source_key=config_key,
                    source_event_id=(
                        source_event_id or f"{config_key}:layer:{layer_revision}"
                    ),
                    vrn=vrn,
                    presence=presence,
                    layer_revision=layer_revision,
                    layer_digest=layer_digest,
                    previous_digest=previous_digest,
                    origin=journal_origin,
                    fanout_id=(
                        fanout_id
                        or f"fanout:{config_key}:event:{config_key}:layer:{layer_revision}"
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
        return record

    def update_source_generation(
        self,
        *,
        config_key: str,
        source_generation: int,
        expected_layer_revision: int,
        expected_layer_digest: str | None,
    ) -> ConfigSourceLayerRecord:
        """把本地 materialized layer 绑定到共享 source owner 的 generation。"""

        if source_generation < 1 or expected_layer_revision < 1:
            raise ValueError("source generation 或 layer revision 必须为正数")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT vrn, presence, config_version, payload_json,
                       layer_revision, layer_digest, source_generation,
                       previous_digest, updated_at, previous_payload_json
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"source layer 不存在，无法绑定共享 generation: {config_key}"
                )
            current_revision = int(row[4])
            current_digest = str(row[5]) if row[5] is not None else None
            if (
                current_revision != expected_layer_revision
                or current_digest != expected_layer_digest
            ):
                raise ConfigConflictError(
                    "source layer generation 绑定 CAS 冲突: "
                    f"key={config_key}, revision={current_revision}, digest={current_digest}"
                )
            if int(row[6]) > source_generation:
                raise ConfigConflictError(
                    "source layer generation 不能回退: "
                    f"key={config_key}, current={row[6]}, requested={source_generation}"
                )
            connection.execute(
                """
                UPDATE config_source_layers
                SET source_generation = ?, updated_at = ?
                WHERE config_key = ?
                """,
                (source_generation, utc_now_text(), config_key),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError(f"source layer generation 绑定后无法读取: {config_key}")
        return record
