"""冻结扩展目录的确定性搜索与有界分页，不发现网络或执行候选。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict

from app.agents.policy.tool_capability import ToolCapabilityPolicy
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.mcp.catalog_activation import (
    EXTENSION_CATALOG_SOURCE_ID,
)
from app.services.infrastructure.mcp.tool_definitions import (
    FrozenToolCatalog,
    FrozenToolDefinition,
)
from app.services.infrastructure.tool_output_store import ToolOutputReference

logger = logging.getLogger(__name__)


class ToolSearchConflict(ValueError):
    code = "tool-search-snapshot-conflict"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ToolSearchStorageError(RuntimeError):
    """完整定义物化失败；不得伪造可读取引用。"""


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _rank(definition: FrozenToolDefinition, query: str) -> tuple[int, list[str]]:
    name = _normalized(definition.metadata.tool_id)
    description = _normalized(str(definition.payload["description"]))
    phrase = _normalized(query)
    terms = set(re.findall(r"[^\W_]+", phrase))
    # 连续中文任务句按双字片段匹配，不额外请求模型或 embedding。
    for segment in re.findall(r"[\u3400-\u9fff]+", phrase):
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    name_matches = sorted(term for term in terms if term in name)
    description_matches = sorted(term for term in terms if term in description)
    score = (100 if phrase == name else 30 if phrase in name else 0)
    score += 15 if phrase in description else 0
    score += 5 * len(name_matches) + len(description_matches)
    reasons = []
    if name_matches:
        reasons.append("名称匹配：" + "、".join(name_matches))
    if description_matches:
        reasons.append("描述匹配：" + "、".join(description_matches))
    return score, reasons


def _cursor(snapshot_key: str, offset: int) -> str:
    return base64.urlsafe_b64encode(canonical_json_bytes({"snapshot": snapshot_key, "offset": offset})).decode().rstrip("=")


def _offset(cursor: str | None, snapshot_key: str, count: int) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > 1024:
            raise ValueError("cursor 过长")
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise ToolSearchConflict("分页 cursor 无效，请重新搜索") from error
    if (not isinstance(value, dict) or set(value) != {"snapshot", "offset"}
            or value["snapshot"] != snapshot_key or type(value["offset"]) is not int
            or not 0 < value["offset"] < count):
        raise ToolSearchConflict("目录、有效策略或查询已变化，或 cursor 无效；请重新搜索")
    return value["offset"]


def _definition_result(
    definition: FrozenToolDefinition, policy: ToolCapabilityPolicy,
    catalog: FrozenToolCatalog, reasons: list[str], *, control_discoverable: bool,
) -> dict[str, object]:
    metadata = definition.metadata
    return {
        "tool_id": metadata.tool_id, "status": "available", "schema_state": "inline_complete", "origin": metadata.origin,
        "server_id": definition.server_id,
        "group_id": metadata.group_id, **definition.payload,
        "schema_hash": definition.schema_hash, "catalog_revision": catalog.catalog_revision,
        "match_reasons": reasons, "effective_policy": asdict(policy),
        "readiness": definition.readiness,
        "missing_connections": [] if definition.readiness == "ready" else [definition.server_id],
        "execution_ready": definition.readiness == "ready" and any(
            identity == EXTENSION_CATALOG_SOURCE_ID for identity, _ in definition.provenance.source_lineage_ref.sources
        ),
        **({"next_control": {"entry_point": EXTENSION_TOOL_INVOKER_NAME,
                              "tool_name": "connections.manage", "operation": "connect",
                              "server_id": definition.server_id}} if definition.readiness != "ready" and control_discoverable else {}),
        **({"next_step": "先在工作区连接管理入口连接目标 Server，再在允许的激活边界重新发现。"}
           if definition.readiness != "ready" else {}),
        "invocation": {"entry_point": EXTENSION_TOOL_INVOKER_NAME, "tool_name": metadata.tool_id},
        "provenance": {
            "activation_snapshot_id": catalog.activation_snapshot_id,
            "resource_id": definition.provenance.resource_id,
            "revision": definition.provenance.revision,
        },
    }


def _error(tool_id: str, code: str, **facts: object) -> dict[str, object]:
    return {"tool_id": tool_id, "status": "error", "error": {"code": code, **facts}}


async def search_tool_catalog(
    catalog: FrozenToolCatalog, policies: Mapping[str, ToolCapabilityPolicy], *,
    query: str | None, tool_ids: list[str] | None, limit: int,
    max_response_bytes: int, cursor: str | None,
    materialize: Callable[[FrozenToolDefinition, dict[str, object]], Awaitable[ToolOutputReference]],
) -> str:
    """返回的字节上限包括元数据和 cursor；完整定义从不裁剪。"""
    definitions = {entry.metadata.tool_id: entry for entry in catalog.definitions}
    if set(policies) != set(definitions):
        raise ValueError("工具搜索需要同一次原子选择快照解析的完整策略")
    policy_revision = sha256_jcs({name: asdict(policy) for name, policy in policies.items()})
    control_policy = policies.get("connections.manage")
    control_discoverable = (control_policy is not None and control_policy.execution_enabled
                           and not control_policy.model_visibility_locked)
    snapshot_key = sha256_jcs({
        "schema": "tool-search-cursor:v1", "catalog": catalog.catalog_revision,
        "definitions": [[entry.metadata.tool_id, entry.provenance.revision, entry.readiness] for entry in catalog.definitions],
        "policy": policy_revision, "query": query, "tool_ids": tool_ids,
        "limit": limit, "max_response_bytes": max_response_bytes,
    })
    if tool_ids is not None:
        candidates = [(name, ["精确工具 ID"]) for name in tool_ids]
    else:
        ranked = [(entry, *_rank(entry, query or "")) for entry in catalog.definitions
                  if policies[entry.metadata.tool_id].execution_enabled and not policies[entry.metadata.tool_id].model_visibility_locked]
        ranked.sort(key=lambda item: (-item[1], item[0].metadata.tool_id))
        candidates = [(entry.metadata.tool_id, reasons) for entry, score, reasons in ranked if score > 0]
    offset = _offset(cursor, snapshot_key, len(candidates))
    result: dict[str, object] = {
        "catalog_revision": catalog.catalog_revision, "policy_revision": policy_revision,
        "results": [], "next_cursor": None,
    }
    results: list[dict[str, object]] = []
    result["results"] = results
    for index in range(offset, min(offset + limit, len(candidates))):
        tool_id, reasons = candidates[index]
        definition = definitions.get(tool_id)
        if definition is None:
            entry = _error(tool_id, "tool-not-found", message="本次冻结目录无此工具；尚未发现的 MCP 目录须先通过工作区连接管理入口连接，再在允许的激活边界重新搜索。")
        elif not policies[tool_id].execution_enabled:
            entry = _error(tool_id, "tool-disabled")
        elif policies[tool_id].model_visibility_locked:
            entry = _error(tool_id, "tool-model-hidden")
        else:
            entry = _definition_result(definition, policies[tool_id], catalog, reasons,
                                       control_discoverable=control_discoverable)
        next_cursor = _cursor(snapshot_key, index + 1) if index + 1 < len(candidates) else None
        trial = {**result, "results": [*results, entry], "next_cursor": next_cursor}
        if len(canonical_json_bytes(trial)) > max_response_bytes:
            if results:
                break
            required_bytes = len(canonical_json_bytes(trial))
            if definition is not None and entry["status"] == "available":
                try:
                    reference = await materialize(definition, entry)
                except (OSError, RuntimeError) as error:
                    logger.exception("工具定义物化失败: tool_id=%s", tool_id)
                    raise ToolSearchStorageError(
                        f"definition-materialization-failed: error_type={type(error).__name__}"
                    ) from error
                entry = {key: value for key, value in entry.items() if key != "input_schema"}
                entry.update({
                    "schema_state": "needs_read", "schema_ref": reference.path,
                    "read_path": reference.read_path, "schema_pointer": "/input_schema",
                    "content_sha256": reference.content_sha256, "byte_count": reference.byte_count,
                })
            if len(canonical_json_bytes({**result, "results": [entry], "next_cursor": next_cursor})) > max_response_bytes:
                entry = _error(tool_id, "definition-too-large", required_response_bytes=required_bytes,
                               max_response_bytes=max_response_bytes)
        results.append(entry)
        result["next_cursor"] = next_cursor
    encoded = canonical_json_bytes(result)
    if len(encoded) > max_response_bytes:
        raise ValueError("工具搜索元数据超过响应上限")
    return encoded.decode("utf-8")
