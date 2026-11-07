"""准备消息和 canonical group，写入动作仍由同一 checkpoint 事务完成。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.identity.item_projection import ItemProjectionManifest
from app.domain.itemized.records import (
    CanonicalItemRecord,
    RuntimeNoticePayload,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.revisions import (
    resolve_message_group,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    find_message_locator,
    read_message_group,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text

PreparedMessage = tuple[
    int,
    str,
    str | None,
    str,
    dict[str, object],
    str,
    tuple[CanonicalItemRecord, ...],
    str,
]


@dataclass
class MessageBatch:
    next_item_sequence: int
    last_message_sequence: int
    first_message_sequence: int | None = None
    prepared: list[PreparedMessage] = field(default_factory=list)
    visible_sequences: list[int] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    display_only_item_ids: list[str] = field(default_factory=list)
    message_item_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)


def inherit_message_selection(
    connection: sqlite3.Connection, *, sequences: Sequence[int], history_view_id: str,
    next_item_sequence: int, last_message_sequence: int, owner: tuple[str, str],
) -> MessageBatch:
    """继承冻结引用，只消费 catalog 归属和组成员，不重新还原旧正文。"""
    batch = MessageBatch(next_item_sequence, last_message_sequence, visible_sequences=list(sequences))
    # stream items 可以共享模型调用的语义组；只有显式 projection_group 才声明完整消息组。
    rows = connection.execute(
        "SELECT message.message_sequence,message.message_id,message.turn_id,message.execution_id,"
        "anchor.projection_manifest_json,member.item_id,member.turn_id,member.execution_id,"
        "execution.session_id,execution.thread_id,history.source_kind "
        "FROM json_each(?) selected JOIN messages message ON message.message_sequence=selected.value "
        "LEFT JOIN item_catalog anchor ON anchor.jsonl_offset=message.jsonl_offset AND anchor.jsonl_length=message.jsonl_length "
        "LEFT JOIN item_catalog member ON member.item_id=anchor.item_id OR ("
        "json_extract(anchor.projection_manifest_json,'$.projection_group') IS NOT NULL "
        "AND anchor.message_group_id IS NOT NULL AND member.message_group_id=anchor.message_group_id) "
        "LEFT JOIN executions execution ON execution.execution_id=member.execution_id "
        "LEFT JOIN context_view_items history ON history.view_id=? AND history.item_id=member.item_id AND history.visible=1 "
        "ORDER BY selected.key,member.item_sequence",
        (canonical_json_text(sequences), history_view_id),
    ).fetchall()
    groups: dict[int, list[str]] = {}
    group_sizes: dict[int, int] = {}
    message_ids: dict[int, str] = {}
    scopes: set[tuple[str, str]] = set()
    for sequence, message_id, turn_id, execution_id, manifest, item_id, item_turn, item_execution, session, thread, source_kind in rows:
        if item_id is None or item_turn != turn_id or item_execution != execution_id or not execution_id:
            raise RuntimeError(f"继承消息组的 catalog 归属不一致: {message_id}/{item_id}")
        scope = (strict_text(session, field="executions.session_id"), strict_text(thread, field="executions.thread_id"))
        if scope not in scopes:
            require_historical_scope(connection, *scope, current_owner=owner)
            scopes.add(scope)
        if sequence not in groups:
            group = ItemProjectionManifest.from_dict(json.loads(manifest)).projection_group
            group_sizes[sequence] = group.size if group is not None else 1
            groups[sequence] = []
            message_ids[sequence] = strict_text(message_id, field="messages.message_id")
        groups[sequence].append(strict_text(item_id, field="item_catalog.item_id"))
        # display_only 是否进入展示历史沿父 checkpoint 的冻结 membership，
        # 不能仅凭 Turn-less 消息把隐藏通知重新发布成展示项。
        if source_kind == "display_only":
            batch.display_only_item_ids.append(item_id)
    if set(groups) != set(sequences) or any(len(group) != group_sizes[sequence] for sequence, group in groups.items()):
        raise RuntimeError("继承消息 selection 缺少完整 canonical group")
    if len(set(message_ids.values())) != len(message_ids):
        raise ValueError("一个 checkpoint 不能重复引用同一个 canonical message_id")
    for sequence in sequences:
        batch.item_ids.extend(groups[sequence])
        batch.message_item_ids[message_ids[sequence]] = tuple(groups[sequence])
    return batch


def prepare_messages(
    *,
    connection: sqlite3.Connection,
    jsonl: Path,
    codec: MessageCodec,
    read_item: Callable[..., CanonicalItemRecord],
    thread_id: str,
    messages: Sequence[object],
    last_message_sequence: int,
    next_item_sequence: int,
    read_locator_sequences: Callable[[], Sequence[int]] | None = None,
    allow_revisions: bool = False,
) -> MessageBatch:
    batch = MessageBatch(next_item_sequence, last_message_sequence)
    locator_sequences: Sequence[int] | None = None
    canonical_message_ids = [
        codec.message_id(message, index) for index, message in enumerate(messages)
    ]
    if len(set(canonical_message_ids)) != len(canonical_message_ids):
        raise ValueError("一个 checkpoint 不能重复引用同一个 canonical message_id")
    resolve_model_call = codec.tool_message_model_call_resolver(messages)
    with jsonl.open("rb") as stream:
        for index, (canonical_message_id, message) in enumerate(
            zip(canonical_message_ids, messages, strict=True)
        ):
            message_value = codec.to_dict(message)
            explicit_anchor = canonical_message_anchor(message_value)
            # 有精确 anchor 时直接定位；否则找到第二个版本即足以判定需要 view 消歧。
            multiple_versions = explicit_anchor is None and connection.execute(
                "SELECT 1 FROM messages WHERE message_id=? LIMIT 1 OFFSET 1", (canonical_message_id,),
            ).fetchone() is not None
            # 只有裸 logical ID 的多版本定位才消费父 view；同批消歧共用这次读取。
            if multiple_versions and read_locator_sequences is not None:
                locator_sequences = read_locator_sequences()
                read_locator_sequences = None
            message_locator = find_message_locator(
                connection, canonical_message_id, item_id=explicit_anchor,
                sequences=locator_sequences if multiple_versions else None,
            )
            message_item_group = (
                read_message_group(
                    connection,
                    stream,
                    offset=message_locator[3],
                    length=message_locator[4],
                    read_item=read_item,
                )
                if message_locator is not None
                else ()
            )
            message_role = codec.message_role(message)
            if message_locator is not None:
                stored_turn_ids = {item.turn_id for item in message_item_group}
                if len(stored_turn_ids) != 1:
                    raise RuntimeError(
                        f"canonical message group Turn identity 不一致: {canonical_message_id}"
                    )
                stored_turn_id = next(iter(stored_turn_ids))
                stored_execution_id = message_locator[2]
                if not isinstance(stored_execution_id, str) or not stored_execution_id:
                    raise RuntimeError(
                        f"canonical item 缺少 execution attribution: {message_item_group[-1].item_id}"
                    )
                # 末尾 anchor 的归属已由 locator 核对；这里只查询其余成员，不重复读取 anchor。
                stored_execution_ids = {stored_execution_id}
                for item in message_item_group[:-1]:
                    canonical_item_id = item.item_id
                    execution_row = connection.execute(
                        "SELECT execution_id FROM item_catalog WHERE item_id = ?",
                        (canonical_item_id,),
                    ).fetchone()
                    if (
                        execution_row is None
                        or not isinstance(execution_row[0], str)
                        or not execution_row[0]
                    ):
                        raise RuntimeError(
                            f"canonical item 缺少 execution attribution: {canonical_item_id}"
                        )
                    stored_execution_ids.add(execution_row[0])
                if len(stored_execution_ids) != 1:
                    raise RuntimeError(
                        f"canonical message group execution identity 不一致: {canonical_message_id}"
                    )
                if message_locator[1] != stored_turn_id:
                    raise RuntimeError(
                        "canonical item 与 message-root catalog attribution 不一致: "
                        f"{canonical_message_id}"
                    )
                turn_id = stored_turn_id
                execution_id = stored_execution_id
            else:
                admission = require_current_execution_admission(
                    langgraph_thread_id=thread_id
                )
                execution_id = admission.execution_id
                if admission.execution_origin == "user_turn":
                    if admission.turn_id is None:
                        raise RuntimeError(
                            "user_turn execution admission 缺少 accepted turn_id"
                        )
                    if message_role == "user" and codec.is_internal(message):
                        raise RuntimeError(
                            "user_turn execution admission 不得写入 internal HumanMessage；"
                            "内部通知必须使用 internal admission"
                        )
                    turn_id = admission.turn_id
                elif admission.execution_origin == "internal":
                    if admission.turn_id is not None:
                        raise RuntimeError(
                            "internal execution admission 不得绑定 turn_id"
                        )
                    turn_id = None
                else:
                    raise RuntimeError(
                        "未知 execution admission origin: "
                        f"{admission.execution_origin!r}"
                    )
            model_call_id = (
                resolve_model_call(index)
                if message_role == "tool"
                else codec.model_call_id(message)
                if message_role == "assistant"
                else None
            )
            group, projected = resolve_message_group(
                connection, codec=codec, message=message, message_value=message_value,
                explicit_anchor=explicit_anchor, stored_group=message_item_group,
                message_id=canonical_message_id, turn_id=turn_id, execution_id=execution_id,
                model_call_id=model_call_id, next_item_sequence=batch.next_item_sequence,
                timestamp=datetime.now(UTC).isoformat(), allow_revisions=allow_revisions,
            )
            batch.item_ids.extend(item.item_id for item in group)
            batch.message_item_ids[canonical_message_id] = tuple(item.item_id for item in group)
            for item in group:
                if item.turn_id is not None:
                    continue
                if item.semantic_kind == "runtime_notice":
                    if (
                        item.payload_kind != PayloadKind.STRUCTURED_CONTENT
                        or not isinstance(item.payload, Mapping)
                        or not set(item.payload) & {"prompt", "display_part"}
                    ):
                        continue
                    if RuntimeNoticePayload.from_dict(item.payload).display_part is None:
                        continue
                if item.turn_scope is None or item.semantic_kind == "runtime_notice":
                    batch.display_only_item_ids.append(item.item_id)
            if projected is None:
                # 候选组只验证内容与身份；已存消息继续引用原 locator，不写候选坐标。
                batch.visible_sequences.append(message_locator[0])
                continue
            sequence = batch.last_message_sequence + 1
            batch.next_item_sequence += len(group)
            # 派生 message projection 也从唯一 canonical group 构造，不保存第二份正文。
            batch.prepared.append(
                (
                    sequence,
                    canonical_message_id,
                    turn_id,
                    codec.message_role(message),
                    projected,
                    "internal" if codec.is_internal(message) else "visible",
                    group,
                    execution_id,
                )
            )
            batch.last_message_sequence = sequence
            if batch.first_message_sequence is None:
                batch.first_message_sequence = sequence
            batch.visible_sequences.append(sequence)
    return batch
