"""VRN 严格 grammar 单元测试：接受表与拒绝表。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    VrnGrammarError,
    parse_vrn,
    resource_display_uri,
    skill_display_uri,
    workspace_agent_spec_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    SemanticResourceDescriptor,
)


def test_parse_workspace_agent_spec() -> None:
    parsed = parse_vrn("boxteam://workspace/ws-1/resources/agent-spec/root/AGENTS.md")
    assert parsed.scope == "workspace"
    assert parsed.scope_id == "ws-1"
    assert parsed.kind == "agent-spec"
    assert parsed.logical_name == "root/AGENTS.md"


def test_parse_skill_three_scopes() -> None:
    for scope, scope_id in (
        ("workspace", "ws-1"),
        ("user", "local"),
        ("gateway", "gw-1"),
        ("inline", "dist-1"),
    ):
        parsed = parse_vrn(
            f"boxteam://{scope}/{scope_id}/resources/skills/code-review/SKILL.md"
        )
        assert parsed.scope == scope
        assert parsed.scope_id == scope_id
        assert parsed.kind == "skills"
        assert parsed.logical_name == "code-review"


def test_parse_config_and_session_kinds() -> None:
    # config/session 为本次新增 kind：规范形态为 .../resources/{kind}/{...canonical
    # path segments}，logical_name 即尾段逐段拼接结果，且解析回自身。
    parsed = parse_vrn("boxteam://inline/source-development-0_0_2/resources/config/workspace_inline")
    assert parsed.scope == "inline"
    assert parsed.scope_id == "source-development-0_0_2"
    assert parsed.kind == "config"
    assert parsed.logical_name == "workspace_inline"
    assert parsed.tail_segments == ("workspace_inline",)
    assert parsed.display_uri == "boxteam://inline/source-development-0_0_2/resources/config/workspace_inline"

    parsed = parse_vrn("boxteam://workspace/ws-1/resources/session/sess-1/child")
    assert parsed.scope == "workspace"
    assert parsed.scope_id == "ws-1"
    assert parsed.kind == "session"
    assert parsed.logical_name == "sess-1/child"
    assert parsed.tail_segments == ("sess-1", "child")
    assert parsed.display_uri == "boxteam://workspace/ws-1/resources/session/sess-1/child"


def test_resource_display_uri_round_trip_all_kinds() -> None:
    # 统一构造函数：闭集内任一 kind 都能构造并解析回自身。
    cases = (
        ("workspace", "ws-1", "agent-spec", ("root", "AGENTS.md")),
        ("user", "local", "config", ("workspace_mutable_override",)),
        ("gateway", "gw-1", "skills", ("review", "SKILL.md")),
        ("inline", "source-development-0_0_2", "config", ("workspace_inline",)),
        ("workspace", "ws-1", "session", ("sess-1",)),
    )
    for scope, scope_id, kind, tail in cases:
        uri = resource_display_uri(
            scope=scope, scope_id=scope_id, kind=kind, tail_segments=tail
        )
        parsed = parse_vrn(uri)
        assert parsed.display_uri == uri
        assert parsed.kind == kind


def test_user_scope_is_in_closed_set() -> None:
    # ``user`` 是定稿 scope 闭集成员（workspace | user | gateway | inline）；其
    # scope_id 是单用户本地程序约定值 ``local``（app/core/user_identity.py），且
    # 该 scope 的 VRN 能构造并解析回自身。
    uri = resource_display_uri(
        scope="user",
        scope_id="local",
        kind="config",
        tail_segments=("workspace_mutable_override",),
    )
    assert uri == "boxteam://user/local/resources/config/workspace_mutable_override"
    parsed = parse_vrn(uri)
    assert parsed.scope == "user"
    assert parsed.scope_id == "local"
    assert parsed.kind == "config"
    assert parsed.logical_name == "workspace_mutable_override"
    assert parsed.display_uri == uri


def test_resource_display_uri_rejects_unregistered_kind_and_scope() -> None:
    with pytest.raises(VrnGrammarError) as excinfo:
        resource_display_uri(
            scope="workspace", scope_id="ws-1", kind="plugins", tail_segments=("x",)
        )
    assert excinfo.value.reason_code == "unknown_resource_kind"
    with pytest.raises(VrnGrammarError) as excinfo:
        resource_display_uri(
            scope="memory", scope_id="local", kind="config", tail_segments=("a",)
        )
    assert excinfo.value.reason_code == "unknown_scope"


def test_config_and_session_require_canonical_tail() -> None:
    # 闭集内的新 kind 仍 fail-closed：缺少规范化尾段即 malformed_path。
    for kind in ("config", "session"):
        with pytest.raises(VrnGrammarError) as excinfo:
            parse_vrn(f"boxteam://workspace/ws-1/resources/{kind}")
        assert excinfo.value.reason_code == "malformed_path"


def test_descriptor_closure_excludes_config_and_session() -> None:
    # 语法闭集（_RESOURCE_KINDS）与描述符闭集（_DESCRIPTOR_KINDS）是两个独立闭集：
    # config/session 是可寻址的语法 kind，但不在描述符闭集内，故不得用其中之一去
    # 校验另一个的输入。
    for kind, uri in (
        ("config", "boxteam://inline/source-development-0_0_2/resources/config/workspace_inline"),
        ("session", "boxteam://workspace/ws-1/resources/session/sess-1"),
    ):
        parse_vrn(uri)  # 语法闭集接受
        with pytest.raises(ValueError) as excinfo:
            SemanticResourceDescriptor(
                resource_id="res-1",
                source_id="src-1",
                kind=kind,
                display_uri=uri,
                semantic_revision="rev-1",
                semantic_hash="hash-1",
            )
        assert "未知 SemanticResourceDescriptor.kind" in str(excinfo.value)


def test_memory_scope_is_rejected_fail_closed() -> None:
    # memory 不是 VRN scope；任何 boxteam://memory/ URI 必须在解析层 fail-closed。
    with pytest.raises(VrnGrammarError) as excinfo:
        parse_vrn("boxteam://memory/session/preference")
    assert excinfo.value.reason_code == "unknown_scope"


@pytest.mark.parametrize(
    ("uri", "reason_code"),
    [
        ("", "empty_uri"),
        ("file:///etc/passwd", "unknown_scheme"),
        ("https://workspace/ws-1/resources/skills/s/SKILL.md", "unknown_scheme"),
        (
            "Boxteam://workspace/ws-1/resources/skills/s/SKILL.md",
            "scheme_case_error",
        ),
        (
            "boxteam://user@workspace/ws-1/resources/skills/s/SKILL.md",
            "userinfo_rejected",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/s/SKILL.md?x=1",
            "query_rejected",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/s/SKILL.md#frag",
            "fragment_rejected",
        ),
        (
            "boxteam://workspace/ws-1\\evil/resources/skills/s/SKILL.md",
            "backslash_rejected",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/s%2f../SKILL.md",
            "percent_encoding_rejected",
        ),
        (
            "boxteam://workspace/ws%252f/resources/skills/s/SKILL.md",
            "percent_encoding_rejected",
        ),
        (
            "boxteam://workspace/%2e%2e/resources/skills/s/SKILL.md",
            "percent_encoding_rejected",
        ),
        (
            "boxteam://workspace/../resources/skills/s/SKILL.md",
            "dot_segment",
        ),
        (
            "boxteam://workspace/./resources/skills/s/SKILL.md",
            "dot_segment",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/技能/SKILL.md",
            "non_ascii_rejected",
        ),
        (
            "boxteam://workspace/ws-1\tx/resources/skills/s/SKILL.md",
            "control_char_rejected",
        ),
        (
            "boxteam://workspace//resources/skills/s/SKILL.md",
            "empty_segment",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/s/SKILL.md/",
            "empty_segment",
        ),
        (
            "boxteam://Workspace/ws-1/resources/skills/s/SKILL.md",
            "case_error",
        ),
        (
            "boxteam://workspace/ws-1/resources/Skills/s/SKILL.md",
            "case_error",
        ),
        (
            "boxteam://workspace/ws-1/resources/skills/s/skill.md",
            "case_error",
        ),
        ("boxteam://gateway2/gw-1/resources/skills/s/SKILL.md", "unknown_scope"),
        (
            "boxteam://workspace/ws-1/resources/plugins/x",
            "unknown_resource_kind",
        ),
        ("boxteam://workspace/ws-1/resources/skills", "malformed_path"),
        (
            "boxteam://workspace/ws-1/resources/skills/s/SKILL.md/extra",
            "malformed_path",
        ),
    ],
)
def test_grammar_rejects(uri: str, reason_code: str) -> None:
    with pytest.raises(VrnGrammarError) as excinfo:
        parse_vrn(uri)
    assert excinfo.value.reason_code == reason_code


def test_display_builders_round_trip() -> None:
    uri = workspace_agent_spec_display_uri("ws-1")
    assert parse_vrn(uri).display_uri == uri
    uri = skill_display_uri(scope="gateway", scope_id="gw-1", skill_name="review")
    assert parse_vrn(uri).display_uri == uri


def test_display_builders_reject_bad_names() -> None:
    with pytest.raises(VrnGrammarError) as excinfo:
        skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="../x")
    assert excinfo.value.reason_code == "invalid_character"
    with pytest.raises(VrnGrammarError) as excinfo:
        workspace_agent_spec_display_uri("ws/1")
    assert excinfo.value.reason_code == "invalid_character"
