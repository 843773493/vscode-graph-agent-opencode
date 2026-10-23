"""创建 journal 的纯 envelope/preimage 合同；创建、恢复与启动共用，不读取当前业务状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from app.core.session_catalog_store.contracts import (
    SessionCreationPurpose,
    SessionCreationRecord,
)
from app.core.session_control_store.contracts import ThreadGraphBindingRecord

# 创建意图六字段闭集；发布时初始化 nodes，session.json 只保留不可变身份。
_METADATA_CALLER_KEYS = frozenset(
    {
        "kind",
        "delegation",
        "generation_origin",
        "current_agent_id",
        "current_provider_id",
        "context_source_session_id",
    }
)

def validate_session_metadata_keys(session_metadata: dict[str, object]) -> None:
    """校验调用方 session_metadata 字段闭集（缺/多一律 fail closed）。

    剥离 manifest 的字段集是封闭的：调用方恰好提供六个非派生字段；禁止
    出现 title/title_source/parent_session_id（可变导航字段只存 catalog）。
    """
    if not isinstance(session_metadata, dict):
        raise TypeError(
            f"session_metadata 必须是 dict: {type(session_metadata).__name__}"
        )
    keys = set(session_metadata)
    missing = sorted(_METADATA_CALLER_KEYS - keys)
    unexpected = sorted(keys - _METADATA_CALLER_KEYS)
    if missing or unexpected:
        raise ValueError(
            "session_metadata 字段集非法（剥离 manifest 字段闭集）: "
            f"missing={missing}, unexpected={unexpected}, "
            f"allowed={sorted(_METADATA_CALLER_KEYS)}"
        )


def validate_creation_purpose_metadata(
    purpose: SessionCreationPurpose,
    session_metadata: dict[str, object],
) -> None:
    """校验创建 purpose 与 session kind/source 的唯一对应合同。"""
    kind = session_metadata["kind"]
    context_source_session_id = session_metadata["context_source_session_id"]
    if purpose == "normal":
        valid = kind == "normal" and context_source_session_id is None
    elif purpose == "context_fork":
        valid = (
            kind == "context_fork"
            and isinstance(context_source_session_id, str)
            and bool(context_source_session_id)
        )
    else:
        raise ValueError(f"session creation purpose 非法: {purpose!r}")
    if not valid:
        raise ValueError(
            "session creation purpose 与 session_metadata 不匹配: "
            f"purpose={purpose!r}, kind={kind!r}, "
            f"context_source_session_id={context_source_session_id!r}"
        )


def compute_session_creation_preimage_hash(
    *,
    workspace_id: str,
    parent_node_id: str | None,
    title: str,
    title_source: str = "user",
    purpose: SessionCreationPurpose,
    graph_binding: ThreadGraphBindingRecord,
    session_metadata: dict[str, object],
    request_intent: dict[str, object] | None = None,
) -> str:
    """计算 preimage_hash = sha256(canonical JSON of preimage 四元组)。

    canonical 形式：``sort_keys=True`` + 紧凑分隔符 + UTF-8（本模块与测试
    共用，保证同 preimage 判定跨重入稳定；不追求跨实现 JCS 兼容）。
    ``session_metadata`` 值必须 JSON 可序列化（``datetime`` 等对象须先转
    文本，否则 ``json.dumps`` 抛 ``TypeError`` fail fast）。
    """
    if not isinstance(graph_binding, ThreadGraphBindingRecord):
        raise TypeError("graph_binding 必须是 ThreadGraphBindingRecord")
    payload = {
        "workspace_id": workspace_id,
        "parent_node_id": parent_node_id,
        "title": title,
        "title_source": title_source,
        "purpose": purpose,
        "graph_binding": asdict(graph_binding),
        "session_metadata": session_metadata,
    }
    # 创建控制意图只归 creation ledger/preimage，不展开到业务 session.json。
    if request_intent is not None:
        if not isinstance(request_intent, dict):
            raise TypeError("session creation request_intent 必须是对象")
        payload["request_intent"] = request_intent
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialize_session_creation_metadata(
    session_metadata: dict[str, object],
) -> str:
    """将已校验 metadata 以稳定 canonical JSON 冻结到 creation journal。"""
    validate_session_metadata_keys(session_metadata)
    return json.dumps(
        session_metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_session_creation_envelope(
    record: SessionCreationRecord,
) -> dict[str, object]:
    """只从冻结记录核验 metadata、请求意图和 preimage，返回同一份原 metadata。"""
    try:
        metadata = json.loads(record.session_metadata_json)
    except ValueError as error:
        raise RuntimeError(
            "durable creation metadata 无法解析（record 被外部改动，"
            f"fail closed）: key={record.session_creation_idempotency_key!r}"
        ) from error
    if not isinstance(metadata, dict):
        raise RuntimeError(  # noqa: TRY004 - journal 数据损坏属于语义冲突。
            "durable creation metadata 必须是 JSON 对象（fail closed）: "
            f"key={record.session_creation_idempotency_key!r}"
        )
    validate_session_metadata_keys(metadata)
    try:
        validate_creation_purpose_metadata(record.purpose, metadata)
    except ValueError as error:
        raise RuntimeError(
            "durable creation purpose 与 session_metadata 不匹配 "
            "（fail closed）: "
            f"key={record.session_creation_idempotency_key!r}"
        ) from error
    if serialize_session_creation_metadata(metadata) != record.session_metadata_json:
        raise RuntimeError(
            "durable creation metadata 不是 canonical JSON（fail closed）: "
            f"key={record.session_creation_idempotency_key!r}"
        )
    request_intent = json.loads(record.request_intent_json) if record.request_intent_json is not None else None
    if record.request_intent_json is not None and (
        not isinstance(request_intent, dict)
        or json.dumps(request_intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != record.request_intent_json
    ):
        raise RuntimeError("durable creation request_intent 必须是 canonical JSON 对象（fail closed）")
    actual = compute_session_creation_preimage_hash(
        workspace_id=record.workspace_id,
        parent_node_id=record.parent_node_id,
        title=record.display_name,
        title_source=record.title_source,
        purpose=record.purpose,
        graph_binding=record.graph_binding,
        session_metadata=metadata,
        request_intent=request_intent,
    )
    if actual != record.preimage_hash:
        raise RuntimeError(
            "durable creation preimage 与 envelope 不一致（record 被外部改动，"
            f"fail closed）: key={record.session_creation_idempotency_key!r}"
        )

    return metadata
