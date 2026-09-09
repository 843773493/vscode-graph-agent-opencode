"""canonical catalog 的 content_part、anchor 与 provenance 索引。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.parts import ContentPart, ContentPartAnchor
from app.domain.itemized.runtime import ProvenanceEdge
from app.services.infrastructure.rollout_context.fork.validation import (
    json_mapping,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _view_in_active_branch_lineage(
    connection,
    checkpoint_ns: str,
    view_id: str,
) -> bool:
    """判断 view 是否位于当前 namespace active branch 的父链上。"""
    active = connection.execute(
        "SELECT b.head_view_id FROM branches AS b "
        "JOIN checkpoint_namespace_state AS ns "
        "ON ns.active_branch_id = b.branch_id "
        "WHERE ns.checkpoint_ns = ?",
        (checkpoint_ns,),
    ).fetchone()
    if active is None or active[0] is None:
        return False
    current_view_id = strict_text(active[0], field="branches.head_view_id")
    visited: set[str] = set()
    while current_view_id:
        if current_view_id in visited:
            raise RuntimeError(f"context view 父链成环: {current_view_id}")
        visited.add(current_view_id)
        if current_view_id == view_id:
            return True
        parent = connection.execute(
            "SELECT parent_view_id FROM context_views WHERE view_id = ?",
            (current_view_id,),
        ).fetchone()
        if parent is None:
            return False
        current_view_id = (
            strict_optional_text(parent[0], field="context_views.parent_view_id") or ""
        )
    return False


class RolloutPartsMixin:
    """只维护 canonical payload 的 parts locator 和操作边界。"""

    def resolve_content_part_anchor(
        self,
        thread_id: str,
        *,
        anchor_id: str,
        checkpoint_ns: str = "",
    ) -> ContentPartAnchor:
        """重启后恢复 anchor，并校验 view、part 与父 JSONL 行完整性。"""
        thread_id = strict_text(thread_id, field="thread_id")
        anchor_id = strict_text(anchor_id, field="operation_anchors.anchor_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        # anchor 自己还要用 item_parts.line_hash 给出精确的 stale 诊断；
        # 先跳过通用 item 内容校验，避免把可定位的 anchor 损坏误报成
        # 泛化的 rollout integrity error。offset/commit chain 仍然照常校验。
        self.initialize(
            thread_id,
            checkpoint_ns,
            validate_jsonl_items=False,
        )
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT anchor_id, item_id, part_id, mode, view_id, branch_id, "
                "recovery_capability, fragment_identity, "
                "fragment_length, fragment_hash, fragment_layout_json "
                "FROM operation_anchors WHERE anchor_id = ?",
                (anchor_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"content part anchor 不存在: {anchor_id}")
            if len(row) != 11:
                raise RuntimeError("content part anchor row 字段数量不一致")
            stored_anchor_id = strict_text(row[0], field="operation_anchors.anchor_id")
            if stored_anchor_id != anchor_id:
                raise RuntimeError("content part anchor identity 不一致")
            item_id = strict_text(row[1], field="operation_anchors.item_id")
            part_id = strict_text(row[2], field="operation_anchors.part_id")
            mode = strict_text(row[3], field="operation_anchors.mode")
            if mode not in {"before", "inclusive"}:
                raise RuntimeError(f"content part anchor mode 非法: {mode}")
            view_id = strict_text(row[4], field="operation_anchors.view_id")
            branch_id = strict_text(row[5], field="operation_anchors.branch_id")
            capability = strict_text(
                row[6], field="operation_anchors.recovery_capability"
            )
            if capability not in {"content_part", "fragment"}:
                raise RuntimeError(f"content part anchor capability 非法: {capability}")
            fragment_identity = strict_optional_text(
                row[7], field="operation_anchors.fragment_identity"
            )
            fragment_length = strict_optional_non_negative_int(
                row[8], field="operation_anchors.fragment_length"
            )
            fragment_hash = strict_optional_text(
                row[9], field="operation_anchors.fragment_hash"
            )
            fragment_layout = (
                json_mapping(row[10], field="operation_anchors.fragment_layout_json")
                if row[10] is not None
                else None
            )
            if capability == "fragment":
                if (
                    fragment_identity is None
                    or fragment_length is None
                    or fragment_hash is None
                    or fragment_layout is None
                ):
                    raise RuntimeError("fragment anchor 缺少可恢复字段")
            elif any(
                value is not None
                for value in (
                    fragment_identity,
                    fragment_length,
                    fragment_hash,
                    fragment_layout,
                )
            ):
                raise RuntimeError("content_part anchor 携带 fragment 专用字段")
            part = connection.execute(
                "SELECT content_hash, content_prefix_hash, line_hash FROM item_parts "
                "WHERE item_id = ? AND part_id = ?",
                (item_id, part_id),
            ).fetchone()
            if part is None:
                raise ValueError(
                    "content part anchor stale: locator 不存在或已被移除: "
                    f"{item_id}/{part_id}"
                )
            content_hash = strict_text(part[0], field="item_parts.content_hash")
            prefix_hash = strict_optional_text(
                part[1], field="item_parts.content_prefix_hash"
            )
            line_hash = strict_text(part[2], field="item_parts.line_hash")
            visible = connection.execute(
                "SELECT 1 FROM context_view_items WHERE view_id = ? AND item_id = ? "
                "AND visible = 1",
                (view_id, item_id),
            ).fetchone()
            if visible is None:
                raise ValueError(
                    "content part anchor stale/unreachable: item 不属于指定 view: "
                    f"{item_id}@{view_id}"
                )
            view_branch = connection.execute(
                "SELECT branch_id FROM context_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            if (
                view_branch is None
                or strict_text(view_branch[0], field="context_views.branch_id")
                != branch_id
            ):
                raise ValueError(
                    "content part anchor stale/unreachable: view/branch 不一致: "
                    f"{view_id}@{branch_id}"
                )
            if not _view_in_active_branch_lineage(
                connection,
                checkpoint_ns,
                view_id,
            ):
                raise ValueError(
                    "content part anchor stale/unreachable: view 不在 active branch lineage: "
                    f"{view_id}"
                )
            catalog = connection.execute(
                "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if catalog is None:
                raise ValueError(
                    f"content part anchor stale: parent item 不存在: {item_id}"
                )
            catalog_offset = strict_non_negative_int(
                catalog[0], field=f"item_catalog.jsonl_offset:{item_id}"
            )
            catalog_length = strict_non_negative_int(
                catalog[1], field=f"item_catalog.jsonl_length:{item_id}"
            )
            if catalog_length == 0:
                raise RuntimeError(f"content part parent JSONL length 为 0: {item_id}")
            with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
                stream.seek(catalog_offset)
                raw_line = stream.read(catalog_length)
            if len(raw_line) != catalog_length or _hash_bytes(raw_line) != line_hash:
                raise ValueError(
                    "content part anchor stale: parent JSONL line hash 不匹配: "
                    f"{item_id}"
                )
            return ContentPartAnchor(
                anchor_id=stored_anchor_id,
                item_id=item_id,
                part_id=part_id,
                mode=mode,
                view_id=view_id,
                branch_id=branch_id,
                capability=capability,
                content_hash=content_hash,
                prefix_hash=prefix_hash,
                fragment_identity=fragment_identity,
                fragment_length=fragment_length,
                fragment_hash=fragment_hash,
                fragment_layout=fragment_layout,
            )

    def register_content_part(
        self,
        thread_id: str,
        *,
        item_id: str,
        part: ContentPart,
        locator: Mapping[str, object],
        checkpoint_ns: str = "",
    ) -> None:
        """建立 part locator 派生索引，正文仍只从父 JSONL item 读取。"""
        thread_id = strict_text(thread_id, field="thread_id")
        item_id = strict_text(item_id, field="item_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if part.content_hash != sha256_jcs(part.content):
            raise ItemSchemaError("content part hash 与正文不一致")
        if part.part_semantic_kind not in {value.value for value in SemanticKind}:
            raise ItemSchemaError(
                f"未知 content part semantic kind: {part.part_semantic_kind}"
            )
        if not isinstance(locator, Mapping):
            raise ItemSchemaError("content part locator 必须是 object")
        pointer = locator.get("json_pointer")
        encoding = locator.get("encoding")
        offset = locator.get("offset")
        length = locator.get("length")
        if (
            not isinstance(pointer, str)
            or not pointer.startswith("/")
            or encoding != "jcs:v1"
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
        ):
            raise ItemSchemaError(
                "content part locator 必须包含 json_pointer、encoding=jcs:v1、"
                "非负 offset/length"
            )
        if pointer != "/payload" and not pointer.startswith("/payload/"):
            raise ItemSchemaError(
                "content part json_pointer 必须锚定父 CanonicalItemRecord.payload"
            )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                parent = connection.execute(
                    "SELECT jsonl_offset, jsonl_length, semantic_kind FROM item_catalog WHERE item_id = ?",
                    (item_id,),
                ).fetchone()
                if parent is None:
                    raise KeyError(f"content part parent item 不存在: {item_id}")
                parent_offset = strict_non_negative_int(
                    parent[0], field=f"item_catalog.jsonl_offset:{item_id}"
                )
                parent_length = strict_non_negative_int(
                    parent[1], field=f"item_catalog.jsonl_length:{item_id}"
                )
                if parent_length == 0:
                    raise RuntimeError(
                        f"content part parent JSONL length 为 0: {item_id}"
                    )
                with self.jsonl_path(thread_id, checkpoint_ns).open("rb") as stream:
                    stream.seek(parent_offset)
                    raw_line = stream.read(parent_length)
                if len(raw_line) != parent_length:
                    raise RuntimeError(
                        f"content part parent JSONL locator 越界: {item_id}"
                    )
                line_hash = _hash_bytes(raw_line)
                try:
                    envelope = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RuntimeError(
                        f"content part parent item JSONL 无法解码: {item_id}"
                    ) from error
                if not isinstance(envelope, Mapping):
                    raise TypeError(f"content part parent envelope 非法: {item_id}")
                selected: object = envelope
                for raw_token in pointer.split("/")[1:]:
                    token = raw_token.replace("~1", "/").replace("~0", "~")
                    if isinstance(selected, Mapping) and token in selected:
                        selected = selected[token]
                    elif (
                        isinstance(selected, list)
                        and token.isdigit()
                        and int(token) < len(selected)
                    ):
                        selected = selected[int(token)]
                    else:
                        raise ItemSchemaError(
                            f"content part json_pointer 不存在: {item_id}{pointer}"
                        )
                if sha256_jcs(selected) != part.content_hash:
                    raise ItemSchemaError(
                        f"content part 正文与父 JSONL payload 不一致: {item_id}/{part.part_id}"
                    )
                selected_length = len(canonical_json_bytes(selected))
                if length != selected_length:
                    raise ItemSchemaError(
                        "content part locator length 与 JCS fragment 长度不一致: "
                        f"expected={selected_length}, actual={length}"
                    )
                values = (
                    item_id,
                    part.part_id,
                    part.part_ordinal,
                    part.part_semantic_kind,
                    part.prefix_hash,
                    part.content_hash,
                    _json(dict(locator)),
                    line_hash,
                    _now(),
                )
                existing = connection.execute(
                    "SELECT part_ordinal, part_semantic_kind, content_prefix_hash, content_hash, locator_json, line_hash FROM item_parts WHERE item_id = ? AND part_id = ?",
                    (item_id, part.part_id),
                ).fetchone()
                if existing is not None:
                    existing_values = (
                        strict_non_negative_int(
                            existing[0],
                            field=f"item_parts.part_ordinal:{item_id}/{part.part_id}",
                        ),
                        strict_text(existing[1], field="item_parts.part_semantic_kind"),
                        strict_optional_text(
                            existing[2], field="item_parts.content_prefix_hash"
                        ),
                        strict_text(existing[3], field="item_parts.content_hash"),
                        strict_text(existing[4], field="item_parts.locator_json"),
                        strict_text(existing[5], field="item_parts.line_hash"),
                    )
                    if existing_values == (
                        part.part_ordinal,
                        part.part_semantic_kind,
                        part.prefix_hash,
                        part.content_hash,
                        _json(dict(locator)),
                        line_hash,
                    ):
                        return
                    raise ValueError("content part identity/hash 冲突")
                result = connection.execute(
                    "INSERT INTO item_parts(item_id, part_id, part_ordinal, part_semantic_kind, content_prefix_hash, content_hash, locator_json, line_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"content part locator 写入失败: {item_id}/{part.part_id}"
                    )
                connection.commit()

    def register_content_part_anchor(
        self,
        thread_id: str,
        anchor: ContentPartAnchor,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT item_id, part_id, mode, view_id, branch_id, recovery_capability, fragment_identity, fragment_length, fragment_hash, fragment_layout_json FROM operation_anchors WHERE anchor_id = ?",
                    (anchor.anchor_id,),
                ).fetchone()
                expected = (
                    anchor.item_id,
                    anchor.part_id,
                    anchor.mode,
                    anchor.view_id,
                    anchor.branch_id,
                    anchor.capability,
                    anchor.fragment_identity,
                    anchor.fragment_length,
                    anchor.fragment_hash,
                    _json(dict(anchor.fragment_layout))
                    if anchor.fragment_layout is not None
                    else None,
                )
                part_row = connection.execute(
                    "SELECT content_hash, content_prefix_hash FROM item_parts WHERE item_id = ? AND part_id = ?",
                    (anchor.item_id, anchor.part_id),
                ).fetchone()
                if existing is not None:
                    existing_values = (
                        strict_text(existing[0], field="operation_anchors.item_id"),
                        strict_text(existing[1], field="operation_anchors.part_id"),
                        strict_text(existing[2], field="operation_anchors.mode"),
                        strict_text(existing[3], field="operation_anchors.view_id"),
                        strict_text(existing[4], field="operation_anchors.branch_id"),
                        strict_text(
                            existing[5],
                            field="operation_anchors.recovery_capability",
                        ),
                        strict_optional_text(
                            existing[6], field="operation_anchors.fragment_identity"
                        ),
                        strict_optional_non_negative_int(
                            existing[7], field="operation_anchors.fragment_length"
                        ),
                        strict_optional_text(
                            existing[8], field="operation_anchors.fragment_hash"
                        ),
                        (
                            _json(
                                json_mapping(
                                    existing[9],
                                    field="operation_anchors.fragment_layout_json",
                                )
                            )
                            if existing[9] is not None
                            else None
                        ),
                    )
                    if existing_values == expected:
                        if part_row is None:
                            raise KeyError(
                                "content part anchor 的 parent part 已不可达"
                            )
                        existing_content_hash = strict_text(
                            part_row[0], field="item_parts.content_hash"
                        )
                        existing_prefix_hash = strict_optional_text(
                            part_row[1], field="item_parts.content_prefix_hash"
                        )
                        if existing_content_hash != anchor.content_hash or (
                            anchor.prefix_hash is not None
                            and existing_prefix_hash != anchor.prefix_hash
                        ):
                            raise ValueError(
                                "content part anchor 的既有 identity 与当前 part hash 不一致"
                            )
                        return
                    raise ValueError("operation anchor identity 冲突")
                if part_row is None:
                    raise KeyError(
                        f"content part anchor 指向不存在的 part: "
                        f"{anchor.item_id}/{anchor.part_id}"
                    )
                part_content_hash = strict_text(
                    part_row[0], field="item_parts.content_hash"
                )
                part_prefix_hash = strict_optional_text(
                    part_row[1], field="item_parts.content_prefix_hash"
                )
                if part_content_hash != anchor.content_hash or (
                    anchor.prefix_hash is not None
                    and part_prefix_hash != anchor.prefix_hash
                ):
                    raise ValueError("content part anchor hash 与 part 不一致")
                visible = connection.execute(
                    "SELECT 1 FROM context_view_items WHERE view_id = ? AND item_id = ? AND visible = 1",
                    (anchor.view_id, anchor.item_id),
                ).fetchone()
                if visible is None:
                    raise ValueError("content part anchor 的 item 不在指定 view 中")
                branch = connection.execute(
                    "SELECT branch_id FROM context_views WHERE view_id = ?",
                    (anchor.view_id,),
                ).fetchone()
                if (
                    branch is None
                    or strict_text(branch[0], field="context_views.branch_id")
                    != anchor.branch_id
                ):
                    raise ValueError("content part anchor 的 view/branch 不一致")
                if not _view_in_active_branch_lineage(
                    connection,
                    checkpoint_ns,
                    anchor.view_id,
                ):
                    raise ValueError(
                        "content part anchor 的 view 不在 active branch lineage: "
                        f"{anchor.view_id}"
                    )
                result = connection.execute(
                    "INSERT INTO operation_anchors(anchor_id, anchor_kind, item_id, part_id, mode, view_id, branch_id, recovery_capability, fragment_identity, fragment_length, fragment_hash, fragment_layout_json, created_at) VALUES (?, 'content_part', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        anchor.anchor_id,
                        anchor.item_id,
                        anchor.part_id,
                        anchor.mode,
                        anchor.view_id,
                        anchor.branch_id,
                        anchor.capability,
                        anchor.fragment_identity,
                        anchor.fragment_length,
                        anchor.fragment_hash,
                        _json(dict(anchor.fragment_layout))
                        if anchor.fragment_layout is not None
                        else None,
                        _now(),
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"content part anchor 写入失败: {anchor.anchor_id}"
                    )
                connection.commit()

    def register_provenance_edge(
        self,
        thread_id: str,
        edge: ProvenanceEdge,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT relation, source_ref, target_ref, attempt, supersedes_relation_id, replay_input FROM item_relations WHERE relation_id = ?",
                    (edge.edge_id,),
                ).fetchone()
                expected = (
                    edge.relation,
                    edge.source_ref,
                    edge.target_ref,
                    edge.attempt,
                    edge.supersedes_edge_id,
                    int(edge.replay_input),
                )
                if existing is not None:
                    if tuple(existing) == expected:
                        return
                    raise ValueError("provenance edge identity 冲突")
                result = connection.execute(
                    "INSERT INTO item_relations(relation_id, relation, source_ref, target_ref, attempt, supersedes_relation_id, replay_input, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (edge.edge_id, *expected, _now()),
                )
                if result.rowcount != 1:
                    raise RuntimeError(f"provenance edge 写入失败: {edge.edge_id}")
                connection.commit()


__all__ = ["RolloutPartsMixin"]
