"""资源 URI 的纯 grammar 与构造：boxteam:// 的唯一实现。

规范 URI 只表达逻辑 scope/kind/name，不携带 revision、物理路径、endpoint
或 credential。结构与身份段遵守严格 ASCII profile；只有已公布根的文件
child 段按 UTF-8 解码一次并规范重编码。URI 是安全展示/引用层，不是
identity、capability、dedupe 或幂等 key。
"""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import quote, unquote

from app.core.session_catalog_store.validators import validate_session_id

_SCHEME: Final = "boxteam://"
_SCOPE_KEYWORDS: Final = frozenset({"workspace", "user", "gateway", "inline"})
# 语法 kind 闭集（parse_resource_uri 侧）；与 values.py 的描述符闭集 _DESCRIPTOR_KINDS 是两个
# 独立闭集，不可混用。「config」承载配置来源文件本身，「session」承载会话上下文资源
# （工作区集合根、Session 定位和 Thread 定位）。
_RESOURCE_KINDS: Final = frozenset({"agent-spec", "skills", "config", "session", "workspace", "folder", "files"})
_NAME_CHARSET: Final = frozenset(string.ascii_letters + string.digits + "_-")
_INVALID_PERCENT_ESCAPE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
# 产物末段保留原 owner 的业务 profile；工具结果的固定 .txt 不代表开放文件 child。
_SESSION_ARTIFACT_ID_PATTERNS: Final = {
    "attachments": re.compile(r"att_[0-9a-f]{32}"),
    "tool-results": re.compile(r"tool_[0-9a-f]{24}\.txt"),
}

_GRAMMAR_REASON_CODES: Final = frozenset(
    {
        "empty_uri",
        "unknown_scheme",
        "scheme_case_error",
        "userinfo_rejected",
        "query_rejected",
        "fragment_rejected",
        "backslash_rejected",
        "control_char_rejected",
        "invalid_percent_encoding",
        "invalid_path_encoding",
        "non_ascii_rejected",
        "empty_segment",
        "dot_segment",
        "invalid_character",
        "case_error",
        "unknown_scope",
        "unknown_resource_kind",
        "malformed_path",
    }
)


class ResourceUriGrammarError(ValueError):
    """URI grammar 拒绝；reason_code 是闭合集合。"""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in _GRAMMAR_REASON_CODES:
            raise ValueError(f"未知 ResourceUriGrammarError reason_code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ParsedResourceUri:
    """一次严格解析后的逻辑地址；不含任何 locator/credential 语义。

    `scope_id` 对 workspace/gateway/inline/user 分别是对应 identity（user 为单用户
    本地程序约定值 `local`）；`logical_name` 对 skills 是 skill name，对 agent-spec
    是固定 `root/AGENTS.md`。
    """

    scope: str
    scope_id: str
    kind: str
    logical_name: str
    display_uri: str
    gateway_authority: str | None
    # resources/{kind}/ 之后的身份与解码后的文件 segment；skills 的 logical_name
    # 只取包名，文件名逐字保留。config/session 的 logical_name 即身份尾段逐段拼接。
    tail_segments: tuple[str, ...]
    session_artifact_kind: Literal["attachments", "tool-results"] | None = None


def _has_control_char(value: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)


def _reject_identity_escape(segment: str) -> None:
    if "%" in segment:
        raise ResourceUriGrammarError(
            "invalid_percent_encoding", f"URI 结构或身份段不得使用百分号编码: {segment!r}"
        )


def _validate_dynamic_segment(segment: str, *, field: str) -> None:
    _reject_identity_escape(segment)
    if not segment:
        raise ResourceUriGrammarError("empty_segment", f"URI {field} 不得为空")
    if segment in (".", ".."):
        raise ResourceUriGrammarError(
            "dot_segment", f"URI {field} 不得是 '.' 或 '..': {segment!r}"
        )
    if not _NAME_CHARSET.issuperset(segment):
        raise ResourceUriGrammarError(
            "invalid_character", f"URI {field} 含未登记字符: {segment!r}"
        )


def _encode_file_segment(segment: str) -> str:
    if not segment:
        raise ResourceUriGrammarError("empty_segment", "URI 文件段不得为空")
    if segment in (".", ".."):
        raise ResourceUriGrammarError("dot_segment", f"URI 文件段不得是 '.' 或 '..': {segment!r}")
    if "/" in segment:
        raise ResourceUriGrammarError("invalid_character", f"URI 文件段不得包含分隔符: {segment!r}")
    if "\\" in segment:
        raise ResourceUriGrammarError("backslash_rejected", f"URI 文件段不得包含反斜杠: {segment!r}")
    if _has_control_char(segment):
        raise ResourceUriGrammarError("control_char_rejected", f"URI 文件段含控制字符: {segment!r}")
    try:
        return quote(segment, safe="", encoding="utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ResourceUriGrammarError(
            "invalid_path_encoding", f"URI 文件段无法编码为 UTF-8: {segment!r}"
        ) from error


def _decode_file_segment(segment: str) -> str:
    if _INVALID_PERCENT_ESCAPE.search(segment):
        raise ResourceUriGrammarError(
            "invalid_percent_encoding", f"URI 文件段含非法百分号编码: {segment!r}"
        )
    try:
        return unquote(segment, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ResourceUriGrammarError(
            "invalid_path_encoding", f"URI 文件段不是有效 UTF-8: {segment!r}"
        ) from error


def _parse_scope(segment: str) -> str:
    _reject_identity_escape(segment)
    if segment in _SCOPE_KEYWORDS:
        return segment
    if segment.lower() in _SCOPE_KEYWORDS:
        raise ResourceUriGrammarError(
            "case_error", f"URI scope 大小写错误，必须全小写: {segment!r}"
        )
    raise ResourceUriGrammarError("unknown_scope", f"URI scope 未登记: {segment!r}")


def _parse_kind(segment: str) -> str:
    _reject_identity_escape(segment)
    if segment in _RESOURCE_KINDS:
        return segment
    if segment.lower() in _RESOURCE_KINDS:
        raise ResourceUriGrammarError(
            "case_error", f"URI 资源 kind 大小写错误: {segment!r}"
        )
    raise ResourceUriGrammarError(
        "unknown_resource_kind", f"URI 资源 kind 未登记: {segment!r}"
    )


def _expect_fixed_segment(segment: str, expected: str) -> None:
    _reject_identity_escape(segment)
    if segment == expected:
        return
    if segment.lower() == expected.lower():
        raise ResourceUriGrammarError(
            "case_error", f"URI 固定 segment 大小写错误，期望 {expected!r}: {segment!r}"
        )
    raise ResourceUriGrammarError(
        "malformed_path", f"URI 固定 segment 不匹配，期望 {expected!r}: {segment!r}"
    )


def _parsed(
    scope: str,
    scope_id: str,
    kind: str,
    tail_segments: Sequence[str],
    logical_name: str,
    display_uri: str,
    gateway_authority: str | None,
    session_artifact_kind: Literal["attachments", "tool-results"] | None = None,
) -> ParsedResourceUri:
    return ParsedResourceUri(
        scope=scope,
        scope_id=scope_id,
        kind=kind,
        logical_name=logical_name,
        display_uri=display_uri,
        gateway_authority=gateway_authority,
        tail_segments=tuple(tail_segments),
        session_artifact_kind=session_artifact_kind,
    )


def parse_resource_uri(uri: str, *, local_gateway_id: str | None = None) -> ParsedResourceUri:
    """严格解析 boxteam:// 资源 URI；任何拒绝都先于一切资源访问。"""
    if not isinstance(uri, str) or not uri:
        raise ResourceUriGrammarError("empty_uri", "URI 必须是非空字符串")
    if "\\" in uri:
        raise ResourceUriGrammarError("backslash_rejected", f"URI 不得包含反斜杠: {uri!r}")
    if "?" in uri:
        raise ResourceUriGrammarError("query_rejected", f"URI 不得携带 query: {uri!r}")
    if "#" in uri:
        raise ResourceUriGrammarError("fragment_rejected", f"URI 不得携带 fragment: {uri!r}")
    if _has_control_char(uri):
        raise ResourceUriGrammarError("control_char_rejected", f"URI 含控制字符: {uri!r}")
    if not uri.isascii():
        raise ResourceUriGrammarError("non_ascii_rejected", f"URI 必须是纯 ASCII: {uri!r}")
    if " " in uri:
        raise ResourceUriGrammarError("invalid_character", f"URI 空格必须在文件段编码为 %20: {uri!r}")
    if not uri.startswith(_SCHEME):
        if uri.lower().startswith(_SCHEME):
            raise ResourceUriGrammarError(
                "scheme_case_error", f"URI scheme 大小写错误，必须全小写: {uri!r}"
            )
        raise ResourceUriGrammarError("unknown_scheme", f"URI 只接受 {_SCHEME} scheme: {uri!r}")

    rest = uri[len(_SCHEME) :]
    if "@" in rest.split("/", 1)[0]:
        raise ResourceUriGrammarError("userinfo_rejected", f"URI 不得携带 userinfo: {uri!r}")

    segments = rest.split("/")
    if any(segment == "" for segment in segments):
        raise ResourceUriGrammarError("empty_segment", f"URI 不得有空 segment: {uri!r}")

    authority = None
    if len(segments) > 1 and segments[0].lower() not in _SCOPE_KEYWORDS and segments[1] in _SCOPE_KEYWORDS:
        authority = segments.pop(0)
        _validate_dynamic_segment(authority, field="gateway authority")
        if authority == local_gateway_id:
            authority = None
            uri = _SCHEME + "/".join(segments)
    scope = _parse_scope(segments[0])
    body = segments[1:]

    if scope == "workspace" and len(body) == 1:
        _validate_dynamic_segment(body[0], field="workspace id")
        return _parsed(scope, body[0], "workspace", (), body[0], uri, authority)

    if len(body) < 3:
        raise ResourceUriGrammarError(
            "malformed_path",
            f"资源 URI 必须是 boxteam://{scope}/{{id}}/resources/{{kind}}/...: {uri!r}",
        )
    _validate_dynamic_segment(body[0], field="scope id")
    _expect_fixed_segment(body[1], "resources")
    kind = _parse_kind(body[2])
    tails = body[3:]

    if kind == "workspace":
        raise ResourceUriGrammarError("malformed_path", "Workspace 容器必须使用 boxteam://workspace/{id}")
    if kind == "folder":
        if scope != "workspace" or len(tails) != 1:
            raise ResourceUriGrammarError("malformed_path", "Folder 容器必须使用 Workspace 内的单个稳定 Folder ID")
        _validate_dynamic_segment(tails[0], field="folder id")
        return _parsed(scope, body[0], kind, tails, tails[0], uri, authority)

    if kind in {"skills", "files"}:
        if not tails:
            raise ResourceUriGrammarError(
                "malformed_path",
                f"{kind} URI 必须携带已登记的目录根: {uri!r}",
            )
        if kind == "files":
            if scope != "workspace":
                raise ResourceUriGrammarError("malformed_path", "files/root 只属于 workspace scope")
            _expect_fixed_segment(tails[0], "root")
        else:
            _validate_dynamic_segment(tails[0], field="skill name")
        children = [_decode_file_segment(segment) for segment in tails[1:]]
        body[3:] = [tails[0], *(_encode_file_segment(child) for child in children)]
        prefix = () if authority is None else (authority,)
        uri = _SCHEME + "/".join((*prefix, scope, *body))
        tails = [tails[0], *children]
        return _parsed(scope, body[0], kind, tails, tails[0], uri, authority)

    if kind == "agent-spec":
        if len(tails) != 2:
            raise ResourceUriGrammarError(
                "malformed_path",
                f"agent-spec URI 必须是 boxteam://{scope}/{{id}}/resources/agent-spec/"
                f"root/AGENTS.md: {uri!r}",
            )
        _expect_fixed_segment(tails[0], "root")
        _expect_fixed_segment(tails[1], "AGENTS.md")
        return _parsed(scope, body[0], kind, tails, "root/AGENTS.md", uri, authority)

    if kind == "session" and len(tails) >= 2 and tails[1].lower() in _SESSION_ARTIFACT_ID_PATTERNS:
        # 所有段都是业务身份：先拒绝 escape，再校验结构与原 owner 的 ID profile。
        for tail in tails:
            _reject_identity_escape(tail)
        if scope != "workspace" or len(tails) != 3:
            raise ResourceUriGrammarError(
                "malformed_path", "Session 产物必须精确指向 Workspace 内的单个附件或工具结果",
            )
        artifact_kind = tails[1].lower()
        _expect_fixed_segment(tails[1], artifact_kind)
        _validate_dynamic_segment(tails[0], field="session id")
        try:
            validate_session_id(tails[0])
        except ValueError as error:
            raise ResourceUriGrammarError(
                "invalid_character", f"Session 产物的 session_id 非法: {tails[0]!r}",
            ) from error
        if tails[2] in {".", ".."}:
            raise ResourceUriGrammarError("dot_segment", "Session 产物 ID 不得是点段")
        if _SESSION_ARTIFACT_ID_PATTERNS[artifact_kind].fullmatch(tails[2]) is None:
            raise ResourceUriGrammarError(
                "invalid_character", f"Session 产物 ID 不符合原 owner profile: {tails[2]!r}",
            )
        return _parsed(
            scope, body[0], kind, tails, "/".join(tails), uri, authority,
            "attachments" if artifact_kind == "attachments" else "tool-results",
        )

    # config 需要逻辑身份尾段；只有 workspace session 可以用空尾段表示集合根。
    if not tails and (
        kind == "config" or (kind == "session" and scope != "workspace")
    ):
        raise ResourceUriGrammarError(
            "malformed_path",
            f"{kind} URI 必须携带规范化尾段: boxteam://{scope}/{{id}}/resources/"
            f"{kind}/{{...canonical path segments}}: {uri!r}",
        )
    for tail in tails:
        _validate_dynamic_segment(tail, field=f"{kind} path")
    return _parsed(scope, body[0], kind, tails, "/".join(tails), uri, authority)


def parse_session_thread_uri(
    uri: str, *, local_gateway_id: str | None = None,
) -> tuple[ParsedResourceUri, str, str | None]:
    """统一 Session/Thread 模板；Session 的 main 身份仍由 catalog 显式解析。"""
    parsed = parse_resource_uri(uri, local_gateway_id=local_gateway_id)
    if parsed.scope != "workspace" or parsed.kind != "session":
        raise ResourceUriGrammarError("malformed_path", "Session/Thread URI 必须属于 Workspace Session 资源")
    tails = parsed.tail_segments
    if len(tails) == 1:
        return parsed, tails[0], None
    if len(tails) == 3 and tails[1] == "threads":
        return parsed, tails[0], tails[2]
    raise ResourceUriGrammarError("malformed_path", "Session/Thread URI 必须精确指向 session_id 或 session_id/threads/thread_id")


def thread_resource_uri(
    workspace_id: str, session_id: str, thread_id: str, *, gateway_authority: str | None = None,
) -> str:
    return resource_display_uri(
        scope="workspace", scope_id=workspace_id, kind="session",
        tail_segments=(session_id, "threads", thread_id), gateway_authority=gateway_authority,
    )


def resource_display_uri(
    *, scope: str, scope_id: str, kind: str, tail_segments: Sequence[str],
    gateway_authority: str | None = None,
) -> str:
    """按 kind 构造规范 display URI；URI 构造的唯一通用入口。

    scope/scope_id/kind 取自同一套闭集，文件 child 接收未编码文件名，逐段编码后
    立即走 `parse_resource_uri` 校验；构造与解析使用相同的身份和文件规则。
    """
    if scope not in _SCOPE_KEYWORDS:
        raise ResourceUriGrammarError("unknown_scope", f"URI scope 未登记: {scope!r}")
    if kind not in _RESOURCE_KINDS:
        raise ResourceUriGrammarError(
            "unknown_resource_kind", f"URI 资源 kind 未登记: {kind!r}"
        )
    _validate_dynamic_segment(scope_id, field="scope id")
    if gateway_authority is not None:
        _validate_dynamic_segment(gateway_authority, field="gateway authority")
    if any("/" in segment for segment in tail_segments):
        raise ResourceUriGrammarError("invalid_character", "URI 构造的单个尾段不得包含分隔符")
    if kind in {"skills", "files"} and tail_segments:
        if kind == "skills":
            _validate_dynamic_segment(tail_segments[0], field="skill name")
        else:
            _expect_fixed_segment(tail_segments[0], "root")
        tail_segments = (
            tail_segments[0], *(_encode_file_segment(segment) for segment in tail_segments[1:])
        )
    prefix = () if gateway_authority is None else (gateway_authority,)
    if kind == "workspace":
        if scope != "workspace" or tail_segments:
            raise ResourceUriGrammarError("malformed_path", "Workspace 容器不接受尾段")
        uri = _SCHEME + "/".join((*prefix, scope, scope_id))
    else:
        uri = _SCHEME + "/".join((*prefix, scope, scope_id, "resources", kind, *tail_segments))
    return parse_resource_uri(uri).display_uri


def workspace_agent_spec_display_uri(workspace_id: str) -> str:
    """构造并校验 workspace AGENTS 规范 display URI。"""
    return resource_display_uri(
        scope="workspace",
        scope_id=workspace_id,
        kind="agent-spec",
        tail_segments=("root", "AGENTS.md"),
    )


def skill_display_uri(*, scope: str, scope_id: str, skill_name: str) -> str:
    """构造并校验 workspace/Gateway/inline Skill 规范 display URI。"""
    _validate_dynamic_segment(skill_name, field="skill name")
    return resource_display_uri(
        scope=scope,
        scope_id=scope_id,
        kind="skills",
        tail_segments=(skill_name, "SKILL.md"),
    )
