"""VRN 严格 grammar：boxteam:// 虚拟资源 URI 的唯一解析入口。

规范 URI 只表达逻辑 scope/kind/name，不携带 revision、物理路径、endpoint
或 credential。解析只接受已登记 grammar：百分号编码整体拒绝（因此不存在
二次解码歧义），userinfo、query/fragment、控制字符、反斜杠、空 segment、
`.`/`..`、非 ASCII 与大小写变体一律显式报错。URI 是安全展示/引用层，
不是 identity、capability、dedupe 或幂等 key。
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

_SCHEME: Final = "boxteam://"
_SCOPE_KEYWORDS: Final = frozenset({"workspace", "user", "gateway", "inline"})
# 语法 kind 闭集（parse_vrn 侧）；与 values.py 的描述符闭集 _DESCRIPTOR_KINDS 是两个
# 独立闭集，不可混用。「config」承载配置来源文件本身，「session」承载会话上下文资源
# （会话定位，规范形态 .../resources/session/{...canonical path segments}）。
_RESOURCE_KINDS: Final = frozenset({"agent-spec", "skills", "config", "session"})
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

    `scope_id` 对 workspace/gateway/inline/user 分别是对应 identity（user 为单用户
    本地程序约定值 `local`）；`logical_name` 对 skills 是 skill name，对 agent-spec
    是固定 `root/AGENTS.md`。
    """

    scope: str
    scope_id: str
    kind: str | None
    logical_name: str
    display_uri: str
    # 规范化尾段（resources/{kind}/ 之后的全部 segment）。固定形态 kind（skills /
    # agent-spec）的 logical_name 是规范化后的权威值，可能与尾段字面不同（大小写、
    # skills 尾段丢弃文件名）；发现式 kind（config / session）的 logical_name 即尾段
    # 逐段拼接。
    tail_segments: tuple[str, ...]


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


def _parsed(
    scope: str,
    scope_id: str,
    kind: str,
    tail_segments: list[str],
    logical_name: str,
    display_uri: str,
) -> ParsedVrn:
    return ParsedVrn(
        scope=scope,
        scope_id=scope_id,
        kind=kind,
        logical_name=logical_name,
        display_uri=display_uri,
        tail_segments=tuple(tail_segments),
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
        return _parsed(scope, body[0], kind, tails, tails[0], uri)

    if kind == "agent-spec":
        if len(tails) != 2:
            raise VrnGrammarError(
                "malformed_path",
                f"agent-spec VRN 必须是 boxteam://{scope}/{{id}}/resources/agent-spec/"
                f"root/AGENTS.md: {uri!r}",
            )
        _expect_fixed_segment(tails[0], "root")
        _expect_fixed_segment(tails[1], "AGENTS.md")
        return _parsed(scope, body[0], kind, tails, "root/AGENTS.md", uri)

    # config / session 无固定尾段（规范形态 .../resources/{kind}/{...canonical path
    # segments}）；logical_name 即尾段逐段拼接的规范化结果。
    if not tails:
        raise VrnGrammarError(
            "malformed_path",
            f"{kind} VRN 必须携带规范化尾段: boxteam://{scope}/{{id}}/resources/"
            f"{kind}/{{...canonical path segments}}: {uri!r}",
        )
    for tail in tails:
        _validate_dynamic_segment(tail, field=f"{kind} path")
    return _parsed(scope, body[0], kind, tails, "/".join(tails), uri)


def resource_display_uri(
    *, scope: str, scope_id: str, kind: str, tail_segments: Sequence[str]
) -> str:
    """按 kind 构造规范 display URI；VRN 构造的唯一通用入口。

    scope/scope_id/kind 取自同一套闭集，尾段按 kind 校验固定段：构造后立即走
    `parse_vrn`（构造即校验），故禁止裸拼接，且闭集内任一 kind 都能解析回自身。
    """
    if scope not in _SCOPE_KEYWORDS:
        raise VrnGrammarError("unknown_scope", f"VRN scope 未登记: {scope!r}")
    if kind not in _RESOURCE_KINDS:
        raise VrnGrammarError(
            "unknown_resource_kind", f"VRN 资源 kind 未登记: {kind!r}"
        )
    _validate_dynamic_segment(scope_id, field="scope id")
    # 固定尾段（skills 的 SKILL.md / agent-spec 的 AGENTS.md）合法含 '.'，不在
    # charset 内，故不在此逐段校验；尾段的固定/动态约束与拒绝码统一由 parse_vrn
    # 判定（kind 专属的动态段校验由各 kind 包装函数负责）。
    uri = _SCHEME + "/".join((scope, scope_id, "resources", kind, *tail_segments))
    return parse_vrn(uri).display_uri


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
