"""VRN 严格 grammar 单元测试：接受表与拒绝表。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    VrnGrammarError,
    memory_display_uri,
    parse_vrn,
    skill_display_uri,
    workspace_agent_spec_display_uri,
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
        ("gateway", "gw-1"),
        ("builtin", "dist-1"),
    ):
        parsed = parse_vrn(
            f"boxteam://{scope}/{scope_id}/resources/skills/code-review/SKILL.md"
        )
        assert parsed.scope == scope
        assert parsed.scope_id == scope_id
        assert parsed.kind == "skills"
        assert parsed.logical_name == "code-review"


def test_parse_memory() -> None:
    parsed = parse_vrn("boxteam://memory/session/preference")
    assert parsed.scope == "memory"
    assert parsed.scope_id == "session"
    assert parsed.kind is None
    assert parsed.logical_name == "preference"


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
        ("boxteam://memory/only-one-segment", "malformed_path"),
        ("boxteam://memory/a/b/c", "malformed_path"),
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
    uri = memory_display_uri(memory_scope="session", resource_name="pref")
    assert parse_vrn(uri).display_uri == uri


def test_display_builders_reject_bad_names() -> None:
    with pytest.raises(VrnGrammarError) as excinfo:
        skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="../x")
    assert excinfo.value.reason_code == "invalid_character"
    with pytest.raises(VrnGrammarError) as excinfo:
        workspace_agent_spec_display_uri("ws/1")
    assert excinfo.value.reason_code == "invalid_character"
    with pytest.raises(VrnGrammarError) as excinfo:
        memory_display_uri(memory_scope="session", resource_name="a b")
    assert excinfo.value.reason_code == "invalid_character"
