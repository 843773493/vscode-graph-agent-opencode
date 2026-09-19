"""VRN 严格 grammar：boxteam:// 虚拟资源 URI 的唯一解析入口。

规范 URI 只表达逻辑 scope/kind/name，不携带 revision、物理路径、endpoint
或 credential。解析只接受已登记 grammar：百分号编码整体拒绝（因此不存在
二次解码歧义），userinfo、query/fragment、控制字符、反斜杠、空 segment、
`.`/`..`、非 ASCII 与大小写变体一律显式报错。URI 是安全展示/引用层，
不是 identity、capability、dedupe 或幂等 key。
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Final

_SCHEME: Final = "boxteam://"
_SCOPE_KEYWORDS: Final = frozenset({"workspace", "gateway", "builtin", "memory"})
_RESOURCE_KINDS: Final = frozenset({"agent-spec", "skills"})
_SKILL_SCOPES: Final = frozenset({"workspace", "gateway", "builtin"})
_NAME_CHARSET: Final = frozenset(string.ascii_letters + string.digits + "_-")

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
        "percent_encoding_rejected",
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


class VrnGrammarError(ValueError):
    """VRN grammar 拒绝；reason_code 是闭合集合。"""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in _GRAMMAR_REASON_CODES:
            raise ValueError(f"未知 VrnGrammarError reason_code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ParsedVrn:
    """一次严格解析后的逻辑地址；不含任何 locator/credential 语义。

    `scope_id` 对 workspace/gateway/builtin 是对应 identity，对 memory 是
    memory 逻辑 scope；`logical_name` 对 skills 是 skill name，对
    agent-spec 是固定 `root/AGENTS.md`，对 memory 是逻辑资源名。
    """

    scope: str
    scope_id: str
    kind: str | None
    logical_name: str
    display_uri: str


def _has_control_char(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_dynamic_segment(segment: str, *, field: str) -> None:
    if segment in (".", ".."):
        raise VrnGrammarError(
            "dot_segment", f"VRN {field} 不得是 '.' 或 '..': {segment!r}"
        )
    if not _NAME_CHARSET.issuperset(segment):
        raise VrnGrammarError(
            "invalid_character", f"VRN {field} 含未登记字符: {segment!r}"
        )


def _parse_scope(segment: str) -> str:
    if segment in _SCOPE_KEYWORDS:
        return segment
    if segment.lower() in _SCOPE_KEYWORDS:
        raise VrnGrammarError(
            "case_error", f"VRN scope 大小写错误，必须全小写: {segment!r}"
        )
    raise VrnGrammarError("unknown_scope", f"VRN scope 未登记: {segment!r}")


def _parse_kind(segment: str) -> str:
    if segment in _RESOURCE_KINDS:
        return segment
    if segment.lower() in _RESOURCE_KINDS:
        raise VrnGrammarError(
            "case_error", f"VRN 资源 kind 大小写错误: {segment!r}"
        )
    raise VrnGrammarError(
        "unknown_resource_kind", f"VRN 资源 kind 未登记: {segment!r}"
    )


def _expect_fixed_segment(segment: str, expected: str) -> None:
    if segment == expected:
        return
    if segment.lower() == expected.lower():
        raise VrnGrammarError(
            "case_error", f"VRN 固定 segment 大小写错误，期望 {expected!r}: {segment!r}"
        )
    raise VrnGrammarError(
        "malformed_path", f"VRN 固定 segment 不匹配，期望 {expected!r}: {segment!r}"
    )


def parse_vrn(uri: str) -> ParsedVrn:
    """严格解析 boxteam:// 虚拟资源 URI；任何拒绝都先于一切资源访问。"""
    if not isinstance(uri, str) or not uri:
        raise VrnGrammarError("empty_uri", "VRN 必须是非空字符串")
    if "\\" in uri:
        raise VrnGrammarError("backslash_rejected", f"VRN 不得包含反斜杠: {uri!r}")
    if "%" in uri:
        raise VrnGrammarError(
            "percent_encoding_rejected",
            f"VRN 拒绝百分号编码（含双重编码）: {uri!r}",
        )
    if "?" in uri:
        raise VrnGrammarError("query_rejected", f"VRN 不得携带 query: {uri!r}")
    if "#" in uri:
        raise VrnGrammarError("fragment_rejected", f"VRN 不得携带 fragment: {uri!r}")
    if _has_control_char(uri):
        raise VrnGrammarError("control_char_rejected", f"VRN 含控制字符: {uri!r}")
    if not uri.isascii():
        raise VrnGrammarError("non_ascii_rejected", f"VRN 必须是纯 ASCII: {uri!r}")
    if not uri.startswith(_SCHEME):
        if uri.lower().startswith(_SCHEME):
            raise VrnGrammarError(
                "scheme_case_error", f"VRN scheme 大小写错误，必须全小写: {uri!r}"
            )
        raise VrnGrammarError("unknown_scheme", f"VRN 只接受 {_SCHEME} scheme: {uri!r}")

    rest = uri[len(_SCHEME) :]
    if "@" in rest.split("/", 1)[0]:
        raise VrnGrammarError("userinfo_rejected", f"VRN 不得携带 userinfo: {uri!r}")

    segments = rest.split("/")
    if any(segment == "" for segment in segments):
        raise VrnGrammarError("empty_segment", f"VRN 不得有空 segment: {uri!r}")

    scope = _parse_scope(segments[0])
    body = segments[1:]

    if scope == "memory":
        if len(body) != 2:
            raise VrnGrammarError(
                "malformed_path",
                f"memory VRN 必须是 boxteam://memory/{{scope}}/{{name}}: {uri!r}",
            )
        _validate_dynamic_segment(body[0], field="memory scope")
        _validate_dynamic_segment(body[1], field="memory resource name")
        return ParsedVrn(
            scope="memory",
            scope_id=body[0],
            kind=None,
            logical_name=body[1],
            display_uri=uri,
        )

    if len(body) < 3:
        raise VrnGrammarError(
            "malformed_path",
            f"资源 VRN 必须是 boxteam://{scope}/{{id}}/resources/{{kind}}/...: {uri!r}",
        )
    _validate_dynamic_segment(body[0], field="scope id")
    _expect_fixed_segment(body[1], "resources")
    kind = _parse_kind(body[2])
    tails = body[3:]

    if kind == "skills":
        if len(tails) != 2:
            raise VrnGrammarError(
                "malformed_path",
                f"skills VRN 必须是 boxteam://{scope}/{{id}}/resources/skills/"
                f"{{skill_name}}/SKILL.md: {uri!r}",
            )
        _expect_fixed_segment(tails[1], "SKILL.md")
        _validate_dynamic_segment(tails[0], field="skill name")
        return ParsedVrn(
            scope=scope,
            scope_id=body[0],
            kind=kind,
            logical_name=tails[0],
            display_uri=uri,
        )

    if len(tails) != 2:
        raise VrnGrammarError(
            "malformed_path",
            f"agent-spec VRN 必须是 boxteam://{scope}/{{id}}/resources/agent-spec/"
            f"root/AGENTS.md: {uri!r}",
        )
    _expect_fixed_segment(tails[0], "root")
    _expect_fixed_segment(tails[1], "AGENTS.md")
    return ParsedVrn(
        scope=scope,
        scope_id=body[0],
        kind=kind,
        logical_name="root/AGENTS.md",
        display_uri=uri,
    )


def workspace_agent_spec_display_uri(workspace_id: str) -> str:
    """构造并校验 workspace AGENTS 规范 display URI。"""
    _validate_dynamic_segment(workspace_id, field="workspace id")
    return f"boxteam://workspace/{workspace_id}/resources/agent-spec/root/AGENTS.md"


def skill_display_uri(*, scope: str, scope_id: str, skill_name: str) -> str:
    """构造并校验 workspace/Gateway/builtin Skill 规范 display URI。"""
    if scope not in _SKILL_SCOPES:
        raise VrnGrammarError("unknown_scope", f"Skill VRN scope 未登记: {scope!r}")
    _validate_dynamic_segment(scope_id, field="scope id")
    _validate_dynamic_segment(skill_name, field="skill name")
    return f"boxteam://{scope}/{scope_id}/resources/skills/{skill_name}/SKILL.md"


def memory_display_uri(*, memory_scope: str, resource_name: str) -> str:
    """构造并校验 memory 规范 display URI。"""
    _validate_dynamic_segment(memory_scope, field="memory scope")
    _validate_dynamic_segment(resource_name, field="memory resource name")
    return f"boxteam://memory/{memory_scope}/{resource_name}"
