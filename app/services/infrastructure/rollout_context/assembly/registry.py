"""assembly detail 与 request contribution registry owner。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    DetailRegistryMixin,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _strict_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 1}:
        raise RuntimeError(f"{field} 必须是 SQLite 0/1")
    return value == 1


def _canonical_json_object(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} 必须是非空 JSON object 文本")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{field} JSON 非法") from error
    if not isinstance(decoded, Mapping) or _json(decoded) != value:
        raise RuntimeError(f"{field} 必须是 canonical JSON object")
    return value


class AssemblyRegistryMixin(DetailRegistryMixin):
    """只维护 assembly detail locator 与 request contribution manifest。"""


    def register_context_contribution(
        self,
        thread_id: str,
        contribution: object,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        """保存 request-only source 的 compact provenance，不保存其正文。"""
        thread_id = strict_text(thread_id, field="context_contribution.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("context_contribution.checkpoint_ns 必须是字符串")
        values = {
            name: getattr(contribution, name, None)
            for name in (
                "contribution_id",
                "source_kind",
                "source_revision",
                "content_hash",
                "request_only",
                "metadata",
                "content_length",
                "redacted_stable_digest",
                "contribution_kind",
                "visibility",
                "protection",
            )
        }
        if (
            not all(
                isinstance(values[name], str) and values[name]
                for name in (
                    "contribution_id",
                    "source_kind",
                    "source_revision",
                )
            )
            or not isinstance(values["request_only"], bool)
            or not isinstance(values["metadata"], Mapping)
        ):
            raise ValueError("context contribution 字段不完整")
        if (
            (
                values["content_hash"] is not None
                and (
                    not isinstance(values["content_hash"], str)
                    or not values["content_hash"]
                )
            )
            or (
                values["redacted_stable_digest"] is not None
                and (
                    not isinstance(values["redacted_stable_digest"], str)
                    or not values["redacted_stable_digest"]
                )
            )
            or (
                (values["content_hash"] is None)
                == (values["redacted_stable_digest"] is None)
            )
        ):
            raise ValueError("context contribution 必须恰好包含一个 hash token")
        if values["request_only"] is not True:
            raise ValueError("context contribution 必须是 request-only")
        if values["source_kind"] == "tool_set":
            raise ValueError("tool_set 必须通过 ToolSetSnapshot/ToolSetRef 保存")
        if values["contribution_kind"] not in {
            "prompt",
            "overlay_base",
            "overlay_delta",
            "notice",
        }:
            raise ValueError(
                f"不支持的 context contribution kind: {values['contribution_kind']}"
            )
        if values["visibility"] not in {"public", "internal", "private"}:
            raise ValueError(
                f"未知 context contribution visibility: {values['visibility']}"
            )
        if values["protection"] not in {"public", "redacted", "protected"}:
            raise ValueError(
                f"未知 context contribution protection: {values['protection']}"
            )
        if values["content_length"] is None:
            raise ValueError("context contribution 必须提供 content_length")
        if values["content_length"] is not None and (
            not isinstance(values["content_length"], int)
            or isinstance(values["content_length"], bool)
            or values["content_length"] < 0
        ):
            raise ValueError("context contribution content_length 非法")
        contribution_id = strict_text(
            values["contribution_id"], field="context_contribution.contribution_id"
        )
        source_kind = strict_text(
            values["source_kind"], field="context_contribution.source_kind"
        )
        source_revision = strict_text(
            values["source_revision"], field="context_contribution.source_revision"
        )
        content_hash = strict_optional_text(
            values["content_hash"], field="context_contribution.content_hash"
        )
        redacted_stable_digest = strict_optional_text(
            values["redacted_stable_digest"],
            field="context_contribution.redacted_stable_digest",
        )
        contribution_kind = strict_text(
            values["contribution_kind"], field="context_contribution.contribution_kind"
        )
        visibility = strict_text(
            values["visibility"], field="context_contribution.visibility"
        )
        protection = strict_text(
            values["protection"], field="context_contribution.protection"
        )
        content_length = strict_non_negative_int(
            values["content_length"], field="context_contribution.content_length"
        )
        metadata_json = _json(dict(values["metadata"]))
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, metadata_json FROM context_contributions WHERE contribution_id = ?",
                    (contribution_id,),
                ).fetchone()
                expected = (
                    source_kind,
                    source_revision,
                    content_hash,
                    content_length,
                    redacted_stable_digest,
                    int(values["request_only"]),
                    contribution_kind,
                    visibility,
                    protection,
                    metadata_json,
                )
                if existing is not None:
                    if len(existing) != 10:
                        raise RuntimeError("context contribution 行字段数非法")
                    stored = (
                        strict_text(
                            existing[0], field="context_contributions.source_kind"
                        ),
                        strict_text(
                            existing[1], field="context_contributions.source_revision"
                        ),
                        strict_optional_text(
                            existing[2], field="context_contributions.content_hash"
                        ),
                        strict_non_negative_int(
                            existing[3], field="context_contributions.content_length"
                        ),
                        strict_optional_text(
                            existing[4],
                            field="context_contributions.redacted_stable_digest",
                        ),
                        int(
                            _strict_bool(
                                existing[5], field="context_contributions.request_only"
                            )
                        ),
                        strict_text(
                            existing[6], field="context_contributions.contribution_kind"
                        ),
                        strict_text(
                            existing[7], field="context_contributions.visibility"
                        ),
                        strict_text(
                            existing[8], field="context_contributions.protection"
                        ),
                        _canonical_json_object(
                            existing[9], field="context_contributions.metadata_json"
                        ),
                    )
                    if stored == expected:
                        return
                    previous_metadata = json.loads(stored[9])
                    if (
                        isinstance(previous_metadata, Mapping)
                        and previous_metadata.get("replaceable_source") is True
                        and dict(values["metadata"]).get("replaceable_source") is True
                    ):
                        cursor = connection.execute(
                            "UPDATE context_contributions SET source_kind = ?, source_revision = ?, content_hash = ?, content_length = ?, redacted_stable_digest = ?, request_only = ?, contribution_kind = ?, visibility = ?, protection = ?, metadata_json = ? WHERE contribution_id = ?",
                            (*expected, contribution_id),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(
                                f"context contribution 更新影响行数异常: {contribution_id}"
                            )
                        connection.commit()
                        return
                    raise ValueError("context contribution identity/hash 冲突")
                source_ordinal_row = connection.execute(
                    "SELECT COALESCE(MAX(source_ordinal), -1) + 1 FROM context_contributions"
                ).fetchone()
                if source_ordinal_row is None:
                    raise RuntimeError("context contribution source_ordinal 查询失败")
                source_ordinal = strict_non_negative_int(
                    source_ordinal_row[0],
                    field="context_contributions.source_ordinal",
                )
                cursor = connection.execute(
                    "INSERT INTO context_contributions(contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, assembly_id, contribution_ordinal, source_ordinal, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                    (
                        contribution_id,
                        *expected[:-1],
                        source_ordinal,
                        expected[-1],
                        _now(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"context contribution 未写入: {contribution_id}"
                    )
                connection.commit()

    def list_context_contributions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> tuple[dict[str, object], ...]:
        thread_id = strict_text(thread_id, field="list_context_contributions.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("list_context_contributions.checkpoint_ns 必须是字符串")
        if snapshot is not None and (
            snapshot.thread_id != thread_id or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("context contribution snapshot 与目标 rollout 不一致")
        owned_connection = None
        if snapshot is None:
            self.initialize(thread_id, checkpoint_ns)
            owned_connection = self._connect(thread_id, checkpoint_ns, read_only=True)
        connection = (
            self._snapshot_connection(snapshot)
            if snapshot is not None
            else owned_connection
        )
        assert connection is not None
        try:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, assembly_id, contribution_ordinal, source_ordinal, metadata_json, created_at FROM context_contributions ORDER BY source_ordinal, contribution_id"
            ).fetchall()
        finally:
            if owned_connection is not None:
                owned_connection.close()
        result: list[dict[str, object]] = []
        for row in rows:
            if len(row) != 15:
                raise RuntimeError("context_contributions 行字段数非法")
            result.append(
                {
                    "contribution_id": strict_text(
                        row[0], field="context_contributions.contribution_id"
                    ),
                    "source_kind": strict_text(
                        row[1], field="context_contributions.source_kind"
                    ),
                    "source_revision": strict_text(
                        row[2], field="context_contributions.source_revision"
                    ),
                    "content_hash": strict_optional_text(
                        row[3], field="context_contributions.content_hash"
                    ),
                    "content_length": strict_non_negative_int(
                        row[4], field="context_contributions.content_length"
                    ),
                    "redacted_stable_digest": strict_optional_text(
                        row[5], field="context_contributions.redacted_stable_digest"
                    ),
                    "request_only": int(
                        _strict_bool(row[6], field="context_contributions.request_only")
                    ),
                    "contribution_kind": strict_text(
                        row[7], field="context_contributions.contribution_kind"
                    ),
                    "visibility": strict_text(
                        row[8], field="context_contributions.visibility"
                    ),
                    "protection": strict_text(
                        row[9], field="context_contributions.protection"
                    ),
                    "assembly_id": strict_optional_text(
                        row[10], field="context_contributions.assembly_id"
                    ),
                    "contribution_ordinal": strict_optional_non_negative_int(
                        row[11], field="context_contributions.contribution_ordinal"
                    ),
                    "source_ordinal": strict_non_negative_int(
                        row[12], field="context_contributions.source_ordinal"
                    ),
                    "metadata_json": _canonical_json_object(
                        row[13], field="context_contributions.metadata_json"
                    ),
                    "created_at": strict_text(
                        row[14], field="context_contributions.created_at"
                    ),
                }
            )
        return tuple(result)




__all__ = ["AssemblyRegistryMixin"]
