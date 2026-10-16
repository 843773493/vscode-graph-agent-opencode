"""assembly detail 与 request contribution registry owner。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.request_plan import (
    ContextContribution,
    parse_contribution_binding_manifest,
)
from app.domain.itemized.schema import validate_extensions
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    DetailRegistryMixin,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
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
        session_id: str,
        thread_id: str,
        contribution: ContextContribution,
        *,
        checkpoint_ns: str = "",
        expected_source_revision: str | None = None,
    ) -> int:
        """保存 request-only source 的 compact provenance，不保存其正文。"""
        contribution_id, expected = self._context_contribution_values(
            session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
        )
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            with self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection:
                connection.execute("BEGIN IMMEDIATE")
                source_ordinal = self._write_context_contribution(
                    connection, contribution_id, expected,
                    expected_source_revision=expected_source_revision,
                )
                connection.commit()
                return source_ordinal

    def _register_context_contribution(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        contribution: ContextContribution, checkpoint_ns: str,
    ) -> None:
        """fork 复用同一 registry writer；不另开连接、不提交调用方事务。"""
        if connection.execute("SELECT session_id,thread_id FROM database_meta WHERE singleton_id=1").fetchone() != (session_id, thread_id):
            raise ValueError("source-mismatch: contribution registry 与 fork target owner 不一致")
        contribution_id, expected = self._context_contribution_values(
            session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
        )
        self._write_context_contribution(connection, contribution_id, expected, expected_source_revision=None)

    def _context_contribution_values(
        self, session_id: str, thread_id: str, contribution: ContextContribution,
        *, checkpoint_ns: str,
    ) -> tuple[str, tuple[object, ...]]:
        session_id = strict_text(session_id, field="context_contribution.session_id")
        thread_id = strict_text(thread_id, field="context_contribution.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("context_contribution.checkpoint_ns 必须是字符串")
        if not isinstance(contribution, ContextContribution):
            raise TypeError("context registry 必须接收 typed ContextContribution")
        values = {
            name: getattr(contribution, name, None)
            for name in (
                "contribution_id",
                "source_kind",
                "source_revision",
                "content_hash",
                "request_only",
                "extensions",
                "content_length",
                "contribution_kind",
                "visibility",
                "protection",
                "selection_role",
                "replacement_policy",
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
            or not isinstance(values["extensions"], Mapping)
            or values["selection_role"] not in {"direct", "backing_only"}
            or values["replacement_policy"] not in {"immutable", "replaceable"}
        ):
            raise ValueError("context contribution 字段不完整")
        if not isinstance(values["content_hash"], str) or not values["content_hash"]:
            raise ValueError("context contribution 必须携带 content_hash")
        if values["request_only"] is not True:
            raise ValueError("context contribution 必须是 request-only")
        if values["source_kind"] == "tool_set":
            raise ValueError("tool_set 必须通过 ToolSetSnapshot/ToolSetRef 保存")
        if values["contribution_kind"] not in {
            "prompt",
            "overlay_base",
            "overlay_delta",
            "notice",
            "model_message",
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
        selection_role = strict_text(
            values["selection_role"], field="context_contribution.selection_role"
        )
        replacement_policy = strict_text(
            values["replacement_policy"],
            field="context_contribution.replacement_policy",
        )
        validate_extensions(values["extensions"], field="ContextContribution.extensions")
        binding_manifest_json = _json(contribution.binding_manifest())
        expected = (
            source_kind,
            source_revision,
            content_hash,
            content_length,
            int(values["request_only"]),
            contribution_kind,
            visibility,
            protection,
            selection_role,
            replacement_policy,
            binding_manifest_json,
        )
        return contribution_id, expected

    def _write_context_contribution(
        self, connection: sqlite3.Connection, contribution_id: str, expected: tuple[object, ...],
        *, expected_source_revision: str | None,
    ) -> int:
        """唯一 contribution manifest writer；调用方持有活动事务。"""
        if not connection.in_transaction:
            raise RuntimeError("contribution registry 写入需要调用方活动事务")
        self._storage._require_v2_runtime(connection)
        source_revision, replacement_policy = expected[1], expected[9]
        existing = connection.execute(
            'SELECT source_kind, source_revision, content_hash, content_length, request_only, contribution_kind, visibility, protection, selection_role, replacement_policy, binding_manifest_json, source_ordinal FROM context_contributions WHERE contribution_id = ?',
            (contribution_id,),
        ).fetchone()
        if existing is not None:
            if len(existing) != 12:
                raise RuntimeError("context contribution 行字段数非法")
            source_ordinal = strict_non_negative_int(
                existing[11], field="context_contributions.source_ordinal",
            )
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
                int(
                    _strict_bool(
                        existing[4], field="context_contributions.request_only"
                    )
                ),
                strict_text(
                    existing[5], field="context_contributions.contribution_kind"
                ),
                strict_text(
                    existing[6], field="context_contributions.visibility"
                ),
                strict_text(
                    existing[7], field="context_contributions.protection"
                ),
                strict_text(
                    existing[8],
                    field="context_contributions.selection_role",
                ),
                strict_text(
                    existing[9],
                    field="context_contributions.replacement_policy",
                ),
                _canonical_json_object(
                    existing[10], field="context_contributions.binding_manifest_json"
                ),
            )
            if expected_source_revision is not None and expected_source_revision != stored[1]:
                raise ValueError("context contribution revision CAS 冲突")
            if stored == expected:
                return source_ordinal
            if stored[9] != "replaceable" or replacement_policy != "replaceable":
                raise ValueError("context contribution immutable identity/hash 冲突")
            if expected_source_revision is None or expected_source_revision != stored[1]:
                raise ValueError("context contribution replacement 缺少匹配的 expected source revision CAS")
            if source_revision == stored[1]:
                raise ValueError("context contribution 同 revision 正文/核心字段冲突")
            sealed = connection.execute(
                "SELECT 1 FROM context_assembly_contributions WHERE contribution_id = ? AND source_revision = ? LIMIT 1",
                (contribution_id, stored[1]),
            ).fetchone()
            if sealed is not None:
                raise ValueError("context contribution 已封存，禁止更新原 registry slot")
            cursor = connection.execute(
                "UPDATE context_contributions SET source_kind = ?, source_revision = ?, content_hash = ?, content_length = ?, request_only = ?, contribution_kind = ?, visibility = ?, protection = ?, selection_role = ?, replacement_policy = ?, binding_manifest_json = ? WHERE contribution_id = ? AND source_revision = ? AND replacement_policy = 'replaceable' AND assembly_id IS NULL",
                (*expected, contribution_id, expected_source_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("context contribution revision CAS 冲突或 registry 已封存")
            return source_ordinal
        if expected_source_revision is not None:
            raise ValueError("context contribution revision CAS 冲突：slot 不存在")
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
            'INSERT INTO context_contributions(contribution_id, source_kind, source_revision, content_hash, content_length, request_only, contribution_kind, visibility, protection, selection_role, replacement_policy, assembly_id, contribution_ordinal, source_ordinal, binding_manifest_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)',
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
        return source_ordinal

    def list_context_contributions(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> tuple[dict[str, object], ...]:
        session_id = strict_text(session_id, field="list_context_contributions.session_id")
        thread_id = strict_text(thread_id, field="list_context_contributions.thread_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("list_context_contributions.checkpoint_ns 必须是字符串")
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("context contribution snapshot 与目标 rollout 不一致")
        try:
            with self._storage.read_connection(
                session_id, thread_id, checkpoint_ns, snapshot=snapshot,
            ) as connection:
                self._storage._require_v2_runtime(connection)
                rows = connection.execute(
                    'SELECT contribution_id, source_kind, source_revision, content_hash, content_length, request_only, contribution_kind, visibility, protection, selection_role, replacement_policy, assembly_id, contribution_ordinal, source_ordinal, binding_manifest_json, created_at FROM context_contributions ORDER BY source_ordinal, contribution_id'
                ).fetchall()
        except RolloutNotMaterializedError:
            return ()
        result: list[dict[str, object]] = []
        for row in rows:
            if len(row) != 16:
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
                    "request_only": int(
                        _strict_bool(row[5], field="context_contributions.request_only")
                    ),
                    "contribution_kind": strict_text(
                        row[6], field="context_contributions.contribution_kind"
                    ),
                    "visibility": strict_text(
                        row[7], field="context_contributions.visibility"
                    ),
                    "protection": strict_text(
                        row[8], field="context_contributions.protection"
                    ),
                    "assembly_id": strict_optional_text(
                        row[11], field="context_contributions.assembly_id"
                    ),
                    "contribution_ordinal": strict_optional_non_negative_int(
                        row[12], field="context_contributions.contribution_ordinal"
                    ),
                    "source_ordinal": strict_non_negative_int(
                        row[13], field="context_contributions.source_ordinal"
                    ),
                    "selection_role": strict_text(
                        row[9], field="context_contributions.selection_role"
                    ),
                    "replacement_policy": strict_text(
                        row[10], field="context_contributions.replacement_policy"
                    ),
                    **parse_contribution_binding_manifest(json.loads(_canonical_json_object(
                        row[14], field="context_contributions.binding_manifest_json"
                    ))),
                    "created_at": strict_text(
                        row[15], field="context_contributions.created_at"
                    ),
                }
            )
        return tuple(result)




__all__ = ["AssemblyRegistryMixin"]
