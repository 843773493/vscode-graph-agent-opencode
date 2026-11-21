from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
