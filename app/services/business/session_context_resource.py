from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Literal

from app.abstractions.session_context import SessionContextRevisionChangedError


_SESSION_RESOURCE = re.compile(r"^boxteam://session/([^/]+)$")
_WORKSPACE_SESSION_RESOURCE = re.compile(
    r"^boxteam://workspace/([^/]+)/session/([^/]+)$"
)
_WORKSPACE_SESSIONS_RESOURCE = re.compile(
    r"^boxteam://workspace/([^/]+)/sessions$"
)


@dataclass(frozen=True, slots=True)
class ParsedSessionContextResource:
    """经过校验、可安全用于路由和 cursor 绑定的上下文资源。"""

    canonical: str
    base: str
    kind: Literal["session", "workspace_sessions"]
    session_id: str | None
    workspace_id: str | None
    selector: str | None = None


def parse_session_context_resource(resource: str) -> ParsedSessionContextResource:
    value = resource.strip()
    base, separator, selector = value.partition("#")
    if separator and not (
        selector == "information"
        or (
            selector.startswith("record=")
            and selector.removeprefix("record=").isdigit()
        )
    ):
        raise ValueError("resource locator 只支持 #record={index} 或 #information")
    match = _SESSION_RESOURCE.fullmatch(base)
    if match is not None:
        return ParsedSessionContextResource(
            value,
            base,
            "session",
            match.group(1),
            None,
            selector or None,
        )
    match = _WORKSPACE_SESSION_RESOURCE.fullmatch(base)
    if match is not None:
        workspace_id, session_id = match.groups()
        return ParsedSessionContextResource(
            value,
            base,
            "session",
            session_id,
            workspace_id,
            selector or None,
        )
    match = _WORKSPACE_SESSIONS_RESOURCE.fullmatch(base)
    if match is not None:
        if selector:
            raise ValueError("workspace sessions inventory 不支持 locator fragment")
        return ParsedSessionContextResource(
            value,
            base,
            "workspace_sessions",
            None,
            match.group(1),
        )
    raise ValueError(
        "resource 必须是 boxteam://session/{session_id}、"
        "boxteam://workspace/{workspace_id}/session/{session_id} 或 "
        "boxteam://workspace/{workspace_id}/sessions"
    )


def validate_session_context_read_view(
    resource: ParsedSessionContextResource,
    view: str,
) -> None:
    if resource.kind == "workspace_sessions" and view != "inventory":
        raise ValueError("workspace sessions 资源只支持 inventory view")
    if resource.kind == "session" and view == "inventory":
        raise ValueError("session 资源不支持 inventory view")


def require_session_context_revision(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise SessionContextRevisionChangedError(
            expected_revision=expected,
            actual_revision=actual,
        )


class SessionContextCursorCodec:
    """编码并校验绑定资源、操作和 revision 的不透明分页游标。"""

    @staticmethod
    def encode(
        *,
        resource: str,
        revision: str,
        operation: str,
        offset: int,
        char_offset: int = 0,
    ) -> str:
        payload = json.dumps(
            {
                "resource": resource,
                "revision": revision,
                "operation": operation,
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
        resource: str,
        revision: str,
        operation: str,
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
            raise ValueError("cursor payload 必须是对象")
        cursor_revision = payload.get("revision")
        if cursor_revision != revision:
            raise SessionContextRevisionChangedError(
                expected_revision=str(cursor_revision),
                actual_revision=revision,
            )
        if payload.get("resource") != resource or payload.get("operation") != operation:
            raise ValueError("cursor 与当前 resource 或查询参数不匹配")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("cursor offset 无效")
        char_offset = payload.get("char_offset", 0)
        if not isinstance(char_offset, int) or char_offset < 0:
            raise ValueError("cursor char_offset 无效")
        return offset, char_offset
