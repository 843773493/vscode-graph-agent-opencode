from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.core.resource_uri import (
    parse_resource_uri,
    resource_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    CONTEXT_ASSEMBLY_IDENTITY_NAMESPACE,
    SESSION_RESOURCE_IDENTITY_NAMESPACE,
    ResourceIdentity,
    derive_resource_identity,
    validate_resource_identity,
)


@dataclass(frozen=True, slots=True)
class ParsedSessionContextResource:
    """从中央 URI grammar 得到的会话上下文引用。"""

    resource_identity: ResourceIdentity
    uri: str
    scope: str
    kind: Literal["session", "workspace_sessions", "other"]
    session_id: str | None
    scope_id: str
    gateway_authority: str | None


def parse_session_context_ref(
    *,
    resource_identity: str,
    uri: str,
    scope: str,
    local_gateway_id: str | None = None,
) -> ParsedSessionContextResource:
    """用唯一 URI grammar 解析会话上下文引用，不解析 fragment 或私有语法。"""
    parsed = parse_resource_uri(uri, local_gateway_id=local_gateway_id)
    if scope != parsed.scope:
        raise ResourceUriResolveError(
            "scope_mismatch",
            f"上下文 scope 与 URI 不一致: scope={scope!r}, uri_scope={parsed.scope!r}",
        )
    identity = validate_resource_identity(resource_identity)
    if parsed.kind != "session":
        kind: Literal["session", "workspace_sessions", "other"] = "other"
        session_id = None
    elif (
        (parsed.scope == "workspace" and not parsed.tail_segments)
        or (parsed.scope == "gateway" and parsed.tail_segments == ("inventory",))
    ):
        kind = "workspace_sessions"
        session_id = None
    elif len(parsed.tail_segments) == 1:
        kind = "session"
        session_id = parsed.tail_segments[0]
    else:
        raise ValueError(
            "session URI 必须指向 Workspace Session 集合根、单个 session_id 或 Gateway inventory"
        )
    return ParsedSessionContextResource(
        resource_identity=identity,
        uri=parsed.display_uri,
        scope=parsed.scope,
        kind=kind,
        session_id=session_id,
        scope_id=parsed.scope_id,
        gateway_authority=parsed.gateway_authority,
    )


def session_resource_uri(
    workspace_id: str, session_id: str, *, gateway_authority: str | None = None,
) -> str:
    return resource_display_uri(
        scope="workspace",
        scope_id=workspace_id,
        kind="session",
        tail_segments=(session_id,),
        gateway_authority=gateway_authority,
    )


def workspace_session_inventory_uri(
    workspace_id: str, *, gateway_authority: str | None = None,
) -> str:
    return resource_display_uri(
        scope="workspace",
        scope_id=workspace_id,
        kind="session",
        tail_segments=(),
        gateway_authority=gateway_authority,
    )


def gateway_session_inventory_uri(
    gateway_id: str, *, gateway_authority: str | None = None,
) -> str:
    return resource_display_uri(
        scope="gateway",
        scope_id=gateway_id,
        kind="session",
        tail_segments=("inventory",),
        gateway_authority=gateway_authority,
    )


def session_resource_identity(workspace_id: str, session_id: str) -> ResourceIdentity:
    return derive_resource_identity(
        SESSION_RESOURCE_IDENTITY_NAMESPACE,
        workspace_id,
        session_id,
    )


def workspace_session_inventory_identity(workspace_id: str) -> ResourceIdentity:
    return derive_resource_identity(
        SESSION_RESOURCE_IDENTITY_NAMESPACE,
        "workspace-inventory",
        workspace_id,
    )


def gateway_session_inventory_identity(gateway_id: str) -> ResourceIdentity:
    return derive_resource_identity(
        SESSION_RESOURCE_IDENTITY_NAMESPACE,
        "gateway-inventory",
        gateway_id,
    )


def context_assembly_identity(
    resource_identity: ResourceIdentity,
    assembly_id: str,
) -> ResourceIdentity:
    return derive_resource_identity(
        CONTEXT_ASSEMBLY_IDENTITY_NAMESPACE,
        str(resource_identity),
        assembly_id,
    )


def session_context_read_operation(
    *,
    view: str,
    include: Sequence[str],
    recent_rounds: int,
    include_initial_goal: bool,
) -> str:
    selection = json.dumps(
        {
            "include": sorted(set(include)),
            "recent_rounds": recent_rounds,
            "include_initial_goal": include_initial_goal,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"read:{view}:{hashlib.sha256(selection).hexdigest()}"


def validate_session_context_read_view(
    resource: ParsedSessionContextResource,
    view: str,
    *,
    record_index: int | None = None,
    assembly_ref: ResourceIdentity | None = None,
) -> None:
    views = {
        "overview",
        "messages",
        "records",
        "information",
        "inventory",
        "assembly",
        "assemblies",
    }
    if view not in views:
        raise ValueError(f"未知 session context view: {view!r}")
    if record_index is not None and view != "records":
        raise ValueError("record_index 只能与 records view 一起使用")
    if assembly_ref is not None and view != "assembly":
        raise ValueError("assembly_ref 只能与 assembly view 一起使用")
    if view == "assembly" and assembly_ref is None:
        raise ValueError("assembly view 必须提供 assembly_ref")
    if resource.kind == "other":
        raise ResourceUriResolveError(
            "unsupported_view",
            f"session context 不支持该 URI resource kind: {resource.uri!r}",
        )
    if resource.kind == "workspace_sessions" and view != "inventory":
        raise ResourceUriResolveError(
            "unsupported_view",
            "workspace session inventory 只支持 inventory view",
        )
    if resource.kind == "session" and view == "inventory":
        raise ResourceUriResolveError(
            "unsupported_view",
            "单个 session resource 不支持 inventory view",
        )
    if view in {"assembly", "assemblies"} and resource.kind != "session":
        raise ResourceUriResolveError(
            "unsupported_view",
            f"{view} view 只支持单个 session resource",
        )


def require_session_context_revision(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise SessionContextRevisionChangedError(
            expected_revision=expected,
            actual_revision=actual,
        )


def require_cursor_offset_in_range(offset: int, total: int, *, uri: str) -> None:
    """游标 offset 越过真实条目数时 fail-closed。"""
    if offset > total:
        raise ValueError(
            f"cursor offset 越界: uri={uri} offset={offset} total={total}"
        )


class _SerializedResult(Protocol):
    returned_chars: int

    def model_dump_json(self) -> str: ...


_SerializedResultT = TypeVar("_SerializedResultT", bound=_SerializedResult)


def set_exact_returned_chars(
    result: _SerializedResultT,
    *,
    operation: str,
) -> _SerializedResultT:
    """把 result.returned_chars 收敛为序列化后的真实字符数。"""
    for _ in range(8):
        length = len(result.model_dump_json())
        if result.returned_chars == length:
            return result
        result.returned_chars = length
    raise RuntimeError(f"无法稳定计算 {operation} 响应字符数")


def exact_returned_chars_with_appended_json(
    *,
    empty_result_returned_chars: int,
    appended_json_chars: int,
) -> int:
    """加上条目 JSON 与数组逗号，计算空集合响应的精确 returned_chars。"""
    base_returned_chars = empty_result_returned_chars + appended_json_chars
    candidate_returned_chars = base_returned_chars
    empty_returned_chars_digits = len(str(empty_result_returned_chars))
    for _ in range(8):
        exact_returned_chars = (
            base_returned_chars
            + len(str(candidate_returned_chars))
            - empty_returned_chars_digits
        )
        if exact_returned_chars == candidate_returned_chars:
            return exact_returned_chars
        candidate_returned_chars = exact_returned_chars
    raise RuntimeError("无法稳定计算 session context 响应字符数")


class SessionContextCursorCodec:
    """绑定身份、URI、scope、修订、视图与结构化选择的分页游标。"""

    @staticmethod
    def encode(
        *,
        resource_identity: ResourceIdentity,
        uri: str,
        scope: str,
        revision: str,
        operation: str,
        record_index: int | None = None,
        assembly_ref: ResourceIdentity | None = None,
        offset: int,
        char_offset: int = 0,
    ) -> str:
        payload = json.dumps(
            {
                "resource_identity": str(resource_identity),
                "uri": uri,
                "scope": scope,
                "revision": revision,
                "operation": operation,
                "record_index": record_index,
                "assembly_ref": str(assembly_ref) if assembly_ref is not None else None,
                "offset": offset,
                "char_offset": char_offset,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def decode(
        cursor: str | None,
        *,
        resource_identity: ResourceIdentity,
        uri: str,
        scope: str,
        revision: str,
        operation: str,
        record_index: int | None = None,
        assembly_ref: ResourceIdentity | None = None,
    ) -> tuple[int, int]:
        if cursor is None:
            return 0, 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as error:
            raise ValueError("cursor 格式无效") from error
        if not isinstance(payload, dict):
            raise TypeError("cursor payload 必须是对象")
        cursor_revision = payload.get("revision")
        if cursor_revision != revision:
            raise SessionContextRevisionChangedError(
                expected_revision=str(cursor_revision),
                actual_revision=revision,
            )
        expected = {
            "resource_identity": str(resource_identity),
            "uri": uri,
            "scope": scope,
            "operation": operation,
            "record_index": record_index,
            "assembly_ref": str(assembly_ref) if assembly_ref is not None else None,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("cursor 与当前资源或查询选择不匹配")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("cursor offset 无效")
        char_offset = payload.get("char_offset", 0)
        if not isinstance(char_offset, int) or char_offset < 0:
            raise ValueError("cursor char_offset 无效")
        return offset, char_offset
