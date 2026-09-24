"""McpToolGuidanceProducer：从已验证 MCP 目录派生有界确定性工具指引。

OpenSpec add-context-injection-lifecycle E4：指引只从 validated catalog 派生；
排序、清洗与参数摘要全部确定性，guidance_revision 可重算核对。外部
description 是不可信数据，经控制字符清洗与长度截断后只能成为 tail_only
user-role 内容；指引文本不得更改授权、工具 schema、root 资格或信封。
原始 MCP prompts/resources/server instructions 不进入本 producer。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from app.services.infrastructure.mcp.catalog_owner import McpToolDescriptor
from app.services.infrastructure.mcp.extension_catalog import payload_digest

MAX_GUIDANCE_DESCRIPTION_LENGTH = 200
MAX_GUIDANCE_ARGS_FIELDS = 8
MAX_GUIDANCE_ARGS_SUMMARY_LENGTH = 120

_GUIDANCE_HASH_DOMAIN = "mcp-tool-guidance:v1"


class McpToolGuidanceError(RuntimeError):
    """MCP 工具指引派生的显式错误；dirty 输入必须 fail closed。"""


def _sanitize_untrusted_text(value: str, *, max_length: int) -> str:
    """不可信外部文本：控制字符清洗 + 确定性长度截断。"""
    cleaned = "".join(
        " " if (ord(char) < 0x20 or ord(char) == 0x7F) else char for char in value
    )
    return cleaned.strip()[:max_length]


def _args_summary(args: Mapping[str, object]) -> str:
    """参数 schema 的有界确定性摘要；只描述最小调用形式。

    tool.args 是扁平的「字段名 -> schema」映射（无 properties 包装）。
    """
    if not args:
        return "{}"
    parts: list[str] = []
    for name in sorted(args)[:MAX_GUIDANCE_ARGS_FIELDS]:
        schema = args[name]
        field_type = "any"
        if isinstance(schema, dict) and isinstance(schema.get("type"), str):
            field_type = schema["type"]
        parts.append(f"{name}:{field_type}")
    summary = ", ".join(parts)
    if len(args) > MAX_GUIDANCE_ARGS_FIELDS:
        summary += ", ..."
    return summary[:MAX_GUIDANCE_ARGS_SUMMARY_LENGTH]


@dataclass(frozen=True, slots=True)
class McpToolGuidanceEntry:
    """一个 target 的模型可见指引条目；不含路径/credential/server locator。"""

    tool_id: str
    server_id: str
    remote_name: str
    description: str
    args_summary: str


@dataclass(frozen=True, slots=True)
class McpToolGuidanceSnapshot:
    """一次派生的不可变指引；entries 按 tool_id 确定性排序。

    added/modified/tombstones 是相对 previous snapshot 的确定性 delta；
    tombstones 携带被删除 target 的 tool_id，跨冻结保持稳定。
    """

    catalog_revision: str
    guidance_revision: str
    entries: tuple[McpToolGuidanceEntry, ...]
    added_tool_ids: tuple[str, ...]
    modified_tool_ids: tuple[str, ...]
    tombstones: tuple[str, ...]


class McpToolGuidanceProducer:
    """无状态派生器；delta 相对调用方传入的 previous snapshot。"""

    def produce(
        self,
        *,
        catalog_revision: str,
        descriptors: Iterable[McpToolDescriptor],
        tools_by_id: Mapping[str, BaseTool],
        previous: McpToolGuidanceSnapshot | None = None,
    ) -> McpToolGuidanceSnapshot:
        entries: list[McpToolGuidanceEntry] = []
        for descriptor in descriptors:
            tool = tools_by_id.get(descriptor.tool_id)
            if tool is None:
                raise McpToolGuidanceError(
                    "MCP 指引派生输入 dirty：descriptor 引用的工具不在已验证目录: "
                    f"tool_id={descriptor.tool_id} catalog_revision={catalog_revision}"
                )
            entries.append(
                McpToolGuidanceEntry(
                    tool_id=descriptor.tool_id,
                    server_id=descriptor.server_id,
                    remote_name=descriptor.remote_name,
                    description=_sanitize_untrusted_text(
                        descriptor.description,
                        max_length=MAX_GUIDANCE_DESCRIPTION_LENGTH,
                    ),
                    args_summary=_args_summary(tool.args),
                )
            )
        entries.sort(key=lambda item: item.tool_id)
        payload = [
            [
                entry.tool_id,
                entry.server_id,
                entry.remote_name,
                entry.description,
                entry.args_summary,
            ]
            for entry in entries
        ]
        guidance_revision = payload_digest(
            payload,
            context="MCP 工具指引载荷",
            error_type=McpToolGuidanceError,
        )
        previous_entries = (
            {entry.tool_id: entry for entry in previous.entries}
            if previous is not None
            else {}
        )
        current = {entry.tool_id: entry for entry in entries}
        added = tuple(sorted(set(current) - set(previous_entries)))
        removed = tuple(sorted(set(previous_entries) - set(current)))
        modified = tuple(
            sorted(
                tool_id
                for tool_id in set(current) & set(previous_entries)
                if current[tool_id] != previous_entries[tool_id]
            )
        )
        return McpToolGuidanceSnapshot(
            catalog_revision=catalog_revision,
            guidance_revision=guidance_revision,
            entries=tuple(entries),
            added_tool_ids=added,
            modified_tool_ids=modified,
            tombstones=removed,
        )
