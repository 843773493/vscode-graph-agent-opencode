"""tool_result canonical identity 归一与等价比较（由 item catalog 写入复用）。

函数逐字平移，仅为让 items.py 只保留 canonical item 的写入与定位读取。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)


def _strict_db_text(value: object, *, field: str) -> str:
    """读取 SQLite 文本列，不把损坏值强转成可比较的字符串。"""
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    return value


def _strict_db_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _strict_db_text(value, field=field)


def _comparable_tool_result_payload(
    item: CanonicalItemRecord,
) -> object:
    """提取工具结果正文，排除每个 producer 独有的生命周期 ID。"""

    value = item.payload
    if not isinstance(value, Mapping):
        return value
    # on_tool_end 与下一次 model call 的 ToolMessage 可能分别生成 canonical
    # result。result_id、tool invocation/attempt ID 是 producer 生命周期身份，
    # 不是用户可见结果正文；同一 tool_call_id 的这些字段不同不应制造冲突。
    comparable = {
        key: item
        for key, item in value.items()
        if key not in {"result_id", "tool_invocation_id", "tool_attempt_id"}
    }
    # 固定信封和信封内部目标工具可能各自产生一个 checkpoint shadow。
    # 两者共享一次 provider tool call，信封层 name 与内部目标 name 不同，
    # 但只要正文相同就必须复用同一个 canonical tool_result，不能制造重复
    # item；非信封工具仍保留 name 参与正文一致性校验。
    names = {
        str(name)
        for name in (value.get("name"), comparable.get("name"))
        if isinstance(name, str) and name
    }
    if "invoke_extension_tool" in names:
        comparable.pop("name", None)
    tool_call_id = comparable.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        comparable["tool_call_id"] = provider_tool_call_id(
            item.projection_identity, tool_call_id
        )
    return comparable


def _tool_result_identity(
    item: CanonicalItemRecord,
) -> tuple[str, str] | None:
    """返回 tool result 的原始与 provider 规范化调用身份。"""

    payload = item.payload if isinstance(item.payload, Mapping) else {}
    raw_tool_call_id = payload.get("tool_call_id")
    if not isinstance(raw_tool_call_id, str) or not raw_tool_call_id:
        raw_tool_call_id = (item.tool_result_evidence.tool_call_id if item.tool_result_evidence is not None else None)
    if not isinstance(raw_tool_call_id, str) or not raw_tool_call_id:
        return None
    return (
        raw_tool_call_id,
        provider_tool_call_id(item.projection_identity, raw_tool_call_id),
    )


def _tool_result_scope(identity: ItemProjectionIdentity | None) -> str | None:
    """只读取已封存 typed model-call scope。"""
    return f"model-call:{identity.model_call_id}" if identity is not None and identity.model_call_id is not None else None


def _select_existing_tool_result(
    connection: sqlite3.Connection,
    item: CanonicalItemRecord,
) -> sqlite3.Row | None:
    """按 turn 与规范化 provider call identity 选择既有结果。

    checkpoint carrier 通常只保存原始 call ID，而 stream carrier 可能保存
    ``{model_call_id}:tool-call:{call_id}``。两者是同一次工具结果；但同一
    Turn 允许 provider 重用原始 ID，因此多候选时必须用显式 model provenance
    消歧，无法消歧就失败。
    """

    identity = _tool_result_identity(item)
    if identity is None or item.projection_identity is None or item.projection_identity.model_call_id is None:
        return None
    raw_tool_call_id, normalized_tool_call_id = identity
    rows = connection.execute(
        "SELECT item_id, content_hash, commit_id, jsonl_offset, jsonl_length, "
        "projection_manifest_json, tool_call_id FROM item_catalog "
        "WHERE semantic_kind = 'tool_result' AND turn_id IS ? "
        "ORDER BY item_sequence",
        (item.turn_id,),
    ).fetchall()
    incoming_scope = _tool_result_scope(item.projection_identity)
    scoped_candidates: list[sqlite3.Row] = []
    for row in rows:
        manifest = parse_item_projection_manifest(json.loads(row[5]))
        projection = manifest["projection_identity"]
        identity = ItemProjectionIdentity.from_dict(projection) if projection is not None else None
        existing_raw_tool_call_id = row[6]
        if not isinstance(existing_raw_tool_call_id, str) or not existing_raw_tool_call_id:
            raise RuntimeError(f"既有 tool_result 缺少 typed call identity: {row[0]}")
        if (
            provider_tool_call_id(identity, existing_raw_tool_call_id)
            != normalized_tool_call_id
        ):
            continue
        if incoming_scope is not None and _tool_result_scope(identity) == incoming_scope:
            scoped_candidates.append(row)
    preferred_candidates = scoped_candidates
    for preferred in (preferred_candidates,):
        if len(preferred) == 1:
            return preferred[0]
        if len(preferred) > 1:
            raise RuntimeError(
                "tool_result canonical identity 歧义，拒绝猜测复用: "
                f"turn_id={item.turn_id}, tool_call_id={raw_tool_call_id}"
            )
    return None
