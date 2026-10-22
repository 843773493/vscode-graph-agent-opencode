"""URI 严格 grammar 单元测试：接受表与拒绝表。"""

from __future__ import annotations

import pytest

from app.core.resource_uri import (
    ResourceUriGrammarError,
    parse_resource_uri,
    parse_session_thread_uri,
    resource_display_uri,
    skill_display_uri,
    workspace_agent_spec_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    CatalogBinding,
    ResourceUriCatalog,
    ResourceUriResolver,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    OPERATION_READ_CONTENT,
    ResolutionContext,
    SemanticResourceDescriptor,
)


class _RecordingBindings(dict[str, CatalogBinding]):
    """记录真实 resolver 的查表键，证明规范化与拒绝发生在查表之前。"""

    def __init__(self) -> None:
        super().__init__()
        self.accessed_keys: list[str] = []

    def __getitem__(self, key: str) -> CatalogBinding:
        self.accessed_keys.append(key)
        return super().__getitem__(key)


@pytest.fixture
def resolver_and_bindings() -> tuple[ResourceUriResolver, _RecordingBindings]:
    bindings = _RecordingBindings()
    return ResourceUriResolver(ResourceUriCatalog(bindings=bindings, name_index={})), bindings


def test_parse_workspace_agent_spec() -> None:
    parsed = parse_resource_uri("boxteam://workspace/ws-1/resources/agent-spec/root/AGENTS.md")
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
        parsed = parse_resource_uri(
            f"boxteam://{scope}/{scope_id}/resources/skills/code-review/SKILL.md"
        )
        assert parsed.scope == scope
        assert parsed.scope_id == scope_id
        assert parsed.kind == "skills"
        assert parsed.logical_name == "code-review"


def test_parse_config_and_session_kinds() -> None:
    # config/session 为本次新增 kind：规范形态为 .../resources/{kind}/{...canonical
    # path segments}，logical_name 即尾段逐段拼接结果，且解析回自身。
    parsed = parse_resource_uri("boxteam://inline/source-development-0_0_2/resources/config/workspace_inline")
    assert parsed.scope == "inline"
    assert parsed.scope_id == "source-development-0_0_2"
    assert parsed.kind == "config"
    assert parsed.logical_name == "workspace_inline"
    assert parsed.tail_segments == ("workspace_inline",)
    assert parsed.display_uri == "boxteam://inline/source-development-0_0_2/resources/config/workspace_inline"

    parsed = parse_resource_uri("boxteam://workspace/ws-1/resources/session/sess-1/child")
    assert parsed.scope == "workspace"
    assert parsed.scope_id == "ws-1"
    assert parsed.kind == "session"
    assert parsed.logical_name == "sess-1/child"
    assert parsed.tail_segments == ("sess-1", "child")
    assert parsed.display_uri == "boxteam://workspace/ws-1/resources/session/sess-1/child"

    collection = parse_resource_uri("boxteam://workspace/ws-1/resources/session")
    assert collection.scope == "workspace"
    assert collection.scope_id == "ws-1"
    assert collection.kind == "session"
    assert collection.logical_name == ""
    assert collection.tail_segments == ()


def test_resource_display_uri_round_trip_all_kinds() -> None:
    # 统一构造函数：闭集内任一 kind 都能构造并解析回自身。
    cases = (
        ("workspace", "ws-1", "agent-spec", ("root", "AGENTS.md")),
        ("user", "local", "config", ("workspace_mutable_override",)),
        ("gateway", "gw-1", "skills", ("review", "SKILL.md")),
        ("inline", "source-development-0_0_2", "config", ("workspace_inline",)),
        ("workspace", "ws-1", "session", ()),
        ("workspace", "ws-1", "session", ("sess-1",)),
    )
    for scope, scope_id, kind, tail in cases:
        uri = resource_display_uri(
            scope=scope, scope_id=scope_id, kind=kind, tail_segments=tail
        )
        parsed = parse_resource_uri(uri)
        assert parsed.display_uri == uri
        assert parsed.kind == kind


def test_user_scope_is_in_closed_set() -> None:
    # ``user`` 是定稿 scope 闭集成员（workspace | user | gateway | inline）；其
    # scope_id 是单用户本地程序约定值 ``local``（app/core/user_identity.py），且
    # 该 scope 的 URI 能构造并解析回自身。
    uri = resource_display_uri(
        scope="user",
        scope_id="local",
        kind="config",
        tail_segments=("workspace_mutable_override",),
    )
    assert uri == "boxteam://user/local/resources/config/workspace_mutable_override"
    parsed = parse_resource_uri(uri)
    assert parsed.scope == "user"
    assert parsed.scope_id == "local"
    assert parsed.kind == "config"
    assert parsed.logical_name == "workspace_mutable_override"
    assert parsed.display_uri == uri


def test_resource_display_uri_rejects_unregistered_kind_and_scope() -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(
            scope="workspace", scope_id="ws-1", kind="plugins", tail_segments=("x",)
        )
    assert excinfo.value.reason_code == "unknown_resource_kind"
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(
            scope="memory", scope_id="local", kind="config", tail_segments=("a",)
        )
    assert excinfo.value.reason_code == "unknown_scope"


def test_config_requires_canonical_tail_but_session_allows_collection_root() -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri("boxteam://workspace/ws-1/resources/config")
    assert excinfo.value.reason_code == "malformed_path"
    assert parse_resource_uri("boxteam://workspace/ws-1/resources/session").tail_segments == ()
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri("boxteam://gateway/gw-1/resources/session")
    assert excinfo.value.reason_code == "malformed_path"


def test_session_thread_template_rejects_collection_root() -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_session_thread_uri("boxteam://workspace/ws-1/resources/session")
    assert excinfo.value.reason_code == "malformed_path"

    session_uri = "boxteam://workspace/ws-1/resources/session/ses_00000000400070008000000000000001"
    parsed, session_id, thread_id = parse_session_thread_uri(session_uri)
    assert parsed.display_uri == session_uri
    assert session_id == "ses_00000000400070008000000000000001"
    assert thread_id is None

    thread_uri = session_uri + "/threads/thr_00000000400070008000000000000002"
    parsed, session_id, thread_id = parse_session_thread_uri(thread_uri)
    assert parsed.display_uri == thread_uri
    assert session_id == "ses_00000000400070008000000000000001"
    assert thread_id == "thr_00000000400070008000000000000002"


def test_descriptor_closure_excludes_config_and_session() -> None:
    # 语法闭集（_RESOURCE_KINDS）与描述符闭集（_DESCRIPTOR_KINDS）是两个独立闭集：
    # config/session 是可寻址的语法 kind，但不在描述符闭集内，故不得用其中之一去
    # 校验另一个的输入。
    for kind, uri in (
        ("config", "boxteam://inline/source-development-0_0_2/resources/config/workspace_inline"),
        ("session", "boxteam://workspace/ws-1/resources/session/sess-1"),
    ):
        parse_resource_uri(uri)  # 语法闭集接受
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
    # memory 不是 URI scope；任何 boxteam://memory/ URI 必须在解析层 fail-closed。
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri("boxteam://memory/session/preference")
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
            "invalid_percent_encoding",
        ),
        (
            "boxteam://workspace/ws%252f/resources/skills/s/SKILL.md",
            "invalid_percent_encoding",
        ),
        (
            "boxteam://workspace/%2e%2e/resources/skills/s/SKILL.md",
            "invalid_percent_encoding",
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
        ("boxteam://gateway2/gw-1/resources/skills/s/SKILL.md", "unknown_scope"),
        (
            "boxteam://workspace/ws-1/resources/plugins/x",
            "unknown_resource_kind",
        ),
        ("boxteam://workspace/ws-1/resources/skills", "malformed_path"),
    ],
)
def test_grammar_rejects(uri: str, reason_code: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == reason_code


def test_display_builders_round_trip() -> None:
    uri = workspace_agent_spec_display_uri("ws-1")
    assert parse_resource_uri(uri).display_uri == uri
    uri = skill_display_uri(scope="gateway", scope_id="gw-1", skill_name="review")
    assert parse_resource_uri(uri).display_uri == uri


def test_display_builders_reject_bad_names() -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="../x")
    assert excinfo.value.reason_code == "invalid_character"
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        workspace_agent_spec_display_uri("ws/1")
    assert excinfo.value.reason_code == "invalid_character"


@pytest.mark.parametrize(
    ("scope", "scope_id"),
    [("workspace", "ws-1"), ("user", "local"), ("gateway", "gw-1"), ("inline", "dist-1")],
)
@pytest.mark.parametrize(
    ("children", "encoded_path"),
    [
        ((), ""),
        (("SKILL.md",), "/SKILL.md"),
        (("skill.md",), "/skill.md"),
        (("references", "中文 文档.v2.md"), "/references/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.v2.md"),
        (("scripts", "run.py"), "/scripts/run.py"),
        (("100%.txt",), "/100%25.txt"),
        (("%2e%2e",), "/%252e%252e"),
        (("%2e%2e.txt",), "/%252e%252e.txt"),
        (("a?b#c+.txt",), "/a%3Fb%23c%2B.txt"),
        ((".hidden", "a-_.~z.txt"), "/.hidden/a-_.~z.txt"),
        (("café.md",), "/caf%C3%A9.md"),
        (("cafe\u0301.md",), "/cafe%CC%81.md"),
    ],
)
def test_skill_root_and_file_children_round_trip(
    scope: str, scope_id: str, children: tuple[str, ...], encoded_path: str,
) -> None:
    # 这里只验证 grammar；包根与 child 可解析不代表目录访问能力已实现。
    uri = resource_display_uri(
        scope=scope, scope_id=scope_id, kind="skills", tail_segments=("review", *children),
    )
    assert uri == f"boxteam://{scope}/{scope_id}/resources/skills/review{encoded_path}"
    parsed = parse_resource_uri(uri)
    assert parsed.tail_segments == ("review", *children)
    assert parsed.logical_name == "review"
    assert parsed.display_uri == uri
    assert resource_display_uri(
        scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
        tail_segments=parsed.tail_segments,
    ) == uri


@pytest.mark.parametrize(
    ("wire_child", "filename", "canonical_child"),
    [
        ("%252e%252e.txt", "%2e%2e.txt", "%252e%252e.txt"),
        ("%53K%49LL.md", "SKILL.md", "SKILL.md"),
        ("caf%c3%a9.md", "café.md", "caf%C3%A9.md"),
        ("%7e%2e%5f%2d.txt", "~._-.txt", "~._-.txt"),
        ("a+b.txt", "a+b.txt", "a%2Bb.txt"),
    ],
)
def test_file_escape_normalization_reaches_canonical_catalog_key(
    wire_child: str, filename: str, canonical_child: str,
    resolver_and_bindings: tuple[ResourceUriResolver, _RecordingBindings],
) -> None:
    prefix = "boxteam://workspace/ws-1/resources/skills/review/"
    parsed = parse_resource_uri(prefix + wire_child)
    canonical_uri = prefix + canonical_child
    assert parsed.display_uri == canonical_uri
    assert parsed.tail_segments == ("review", filename)
    resolver, bindings = resolver_and_bindings
    descriptor = SemanticResourceDescriptor(
        resource_id="res-1", source_id="src-1", kind="skills", display_uri=canonical_uri,
        semantic_revision="rev-1", semantic_hash="hash-1",
    )
    bindings[canonical_uri] = CatalogBinding(
        descriptor=descriptor, capabilities=frozenset({OPERATION_READ_CONTENT}), snapshot_ref="snapshot-1",
    )
    handle = resolver.resolve(
        prefix + wire_child, operation=OPERATION_READ_CONTENT,
        context=ResolutionContext(workspace_id="ws-1"),
    )
    assert bindings.accessed_keys == [canonical_uri]
    assert handle.descriptor is descriptor
    assert handle.provenance.display_uri == canonical_uri


@pytest.mark.parametrize(
    ("child", "reason_code"),
    [
        ("%", "invalid_percent_encoding"),
        ("a%2", "invalid_percent_encoding"),
        ("%GG.txt", "invalid_percent_encoding"),
        ("%C3%28", "invalid_path_encoding"),
        ("%FF", "invalid_path_encoding"),
        ("%C0%AF", "invalid_path_encoding"),
        ("%ED%A0%80", "invalid_path_encoding"),
        ("%F4%90%80%80", "invalid_path_encoding"),
        ("%E4%B8", "invalid_path_encoding"),
        ("%2F", "invalid_character"),
        ("a%2fb", "invalid_character"),
        ("%5C", "backslash_rejected"),
        (".", "dot_segment"),
        ("..", "dot_segment"),
        ("%2e", "dot_segment"),
        ("%2E%2e", "dot_segment"),
        ("%00", "control_char_rejected"),
        ("%1F", "control_char_rejected"),
        ("%7F", "control_char_rejected"),
        ("%C2%85", "control_char_rejected"),
        ("", "empty_segment"),
        ("references//a.md", "empty_segment"),
        ("a.md/", "empty_segment"),
        ("中文.md", "non_ascii_rejected"),
        ("a b.md", "invalid_character"),
        ("a\tb.md", "control_char_rejected"),
        ("a\\b.md", "backslash_rejected"),
        ("a.md?x=1", "query_rejected"),
        ("a.md#fragment", "fragment_rejected"),
    ],
)
def test_invalid_file_child_rejected_before_catalog_access(
    child: str, reason_code: str,
    resolver_and_bindings: tuple[ResourceUriResolver, _RecordingBindings],
) -> None:
    uri = "boxteam://workspace/ws-1/resources/skills/review/" + child
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == reason_code
    resolver, bindings = resolver_and_bindings
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resolver.resolve(
            uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1"),
        )
    assert excinfo.value.reason_code == reason_code
    assert bindings.accessed_keys == []


@pytest.mark.parametrize(
    "uri",
    [
        "boxteam://gw%2D1/workspace/ws-1/resources/skills/review/SKILL.md",
        "boxteam://%77orkspace/ws-1/resources/skills/review/SKILL.md",
        "boxteam://workspace/ws%2D1/resources/skills/review/SKILL.md",
        "boxteam://workspace/ws-1/res%6Furces/skills/review/SKILL.md",
        "boxteam://workspace/ws-1/resources/%73kills/review/SKILL.md",
        "boxteam://workspace/ws-1/resources/skills/%72eview/SKILL.md",
        "boxteam://workspace/ws-1/resources/agent-spec/root/%41GENTS.md",
        "boxteam://inline/dist-1/resources/config/workspace%5Finline",
        "boxteam://workspace/ws-1/resources/session/sess%2D1",
        "boxteam://workspace/ws-1/resources/session/sess-1/assemblies/assembly%2D1",
        "boxteam://workspace/ws-1/resources/session/sess-1/attachments/attachment%2D1",
        "boxteam://workspace/ws-1/resources/session/sess-1/tool-results/result%2D1",
        "boxteam://workspace/ws-1/resources/folder/folder%2D1",
    ],
)
def test_identity_and_structure_escapes_are_never_decoded(uri: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == "invalid_percent_encoding"


@pytest.mark.parametrize(
    "uri",
    [
        "boxteam://网关/workspace/ws-1/resources/skills/review/SKILL.md",
        "boxteam://workspace/工作区/resources/skills/review/SKILL.md",
        "boxteam://workspace/ws-1/resources/skills/技能/SKILL.md",
        "boxteam://inline/dist-1/resources/config/配置",
        "boxteam://workspace/ws-1/resources/session/会话",
        "boxteam://workspace/ws-1/resources/session/sess-1/assemblies/装配",
    ],
)
def test_wire_identity_segments_remain_ascii(uri: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == "non_ascii_rejected"


@pytest.mark.parametrize(
    "authority",
    ["gw-1/workspace/other-ws/resources/skills/other-skill", "gw-1/inline/dist-2/resources/skills/other-skill"],
)
def test_builder_rejects_authority_segment_injection(authority: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(
            scope="workspace", scope_id="ws-1", kind="skills",
            tail_segments=("review", "SKILL.md"), gateway_authority=authority,
        )
    assert excinfo.value.reason_code == "invalid_character"


def test_file_canonicalization_preserves_authority_and_scope() -> None:
    uri = resource_display_uri(
        scope="workspace", scope_id="ws-1", kind="skills", tail_segments=("review", "中文.md"),
        gateway_authority="gw-1",
    )
    assert uri == "boxteam://gw-1/workspace/ws-1/resources/skills/review/%E4%B8%AD%E6%96%87.md"
    parsed = parse_resource_uri(uri)
    assert (parsed.gateway_authority, parsed.scope, parsed.scope_id) == ("gw-1", "workspace", "ws-1")
    local = parse_resource_uri(uri, local_gateway_id="gw-1")
    assert local.gateway_authority is None
    assert local.display_uri == "boxteam://workspace/ws-1/resources/skills/review/%E4%B8%AD%E6%96%87.md"
    assert local.tail_segments == ("review", "中文.md")


@pytest.mark.parametrize(
    ("filename", "reason_code"),
    [
        ("", "empty_segment"), (".", "dot_segment"), ("..", "dot_segment"),
        ("a/b.md", "invalid_character"), ("a\\b.md", "backslash_rejected"),
        ("\0", "control_char_rejected"), ("\u0085", "control_char_rejected"),
        ("\ud800", "invalid_path_encoding"),
    ],
)
def test_builder_rejects_invalid_unencoded_file_segments(filename: str, reason_code: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(
            scope="workspace", scope_id="ws-1", kind="skills", tail_segments=("review", filename),
        )
    assert excinfo.value.reason_code == reason_code


def test_grammar_error_codes_remain_closed() -> None:
    for reason_code in ("invalid_percent_encoding", "invalid_path_encoding"):
        assert ResourceUriGrammarError(reason_code, "拒绝编码").reason_code == reason_code
    with pytest.raises(ValueError, match="未知 ResourceUriGrammarError reason_code"):
        ResourceUriGrammarError("unregistered_reason", "不得新增隐式拒绝码")


@pytest.mark.parametrize("children", [(), ("src", "中文 文档.v2.md"), (".github", "100%.txt"), ("%2e", "a?#.md")])
def test_workspace_files_root_uses_same_file_segment_profile(children: tuple[str, ...]) -> None:
    uri = resource_display_uri(
        scope="workspace", scope_id="ws-1", kind="files", tail_segments=("root", *children),
        gateway_authority="gw-1",
    )
    parsed = parse_resource_uri(uri)
    assert parsed.tail_segments == ("root", *children)
    assert (parsed.scope_id, parsed.kind, parsed.gateway_authority) == ("ws-1", "files", "gw-1")
    assert parse_resource_uri(parsed.display_uri) == parsed


@pytest.mark.parametrize("scope", ["user", "inline"])
def test_files_root_rejects_non_workspace_scope(scope: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(f"boxteam://{scope}/test/resources/files/root")
    assert excinfo.value.reason_code == "malformed_path"


@pytest.mark.parametrize(
    ("tail", "reason"),
    [("", "malformed_path"), ("Root", "case_error"), ("other", "malformed_path"),
     ("%72oot", "invalid_percent_encoding"), ("root/%2e%2e", "dot_segment"),
     ("root/a%2fb", "invalid_character"), ("root/%FF", "invalid_path_encoding")],
)
def test_files_root_rejects_unknown_root_or_invalid_child(tail: str, reason: str) -> None:
    uri = "boxteam://workspace/ws-1/resources/files" + ("/" + tail if tail else "")
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == reason


@pytest.mark.parametrize("authority", [None, "gateway_peer"])
@pytest.mark.parametrize(
    ("collection", "artifact_id"),
    [
        ("attachments", "att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("tool-results", "tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt"),
    ],
)
def test_session_artifact_templates_preserve_owner_ids(
    authority: str | None, collection: str, artifact_id: str,
) -> None:
    session_id = "ses_019c3567da377a6f855e1bddac8f1f76"
    uri = resource_display_uri(
        scope="workspace", scope_id="ws-1", kind="session",
        tail_segments=(session_id, collection, artifact_id), gateway_authority=authority,
    )
    prefix = "boxteam://" + (f"{authority}/" if authority is not None else "")
    assert uri == f"{prefix}workspace/ws-1/resources/session/{session_id}/{collection}/{artifact_id}"
    parsed = parse_resource_uri(uri)
    assert parsed.session_artifact_kind == collection
    assert parsed.tail_segments == (session_id, collection, artifact_id)
    assert parsed.display_uri == uri
    assert parse_resource_uri(parsed.display_uri) == parsed
    if authority is not None:
        local = parse_resource_uri(uri, local_gateway_id=authority)
        assert local.gateway_authority is None
        assert local.display_uri == uri.replace(f"{authority}/", "", 1)
        assert local.session_artifact_kind == collection
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_session_thread_uri(uri)
    assert excinfo.value.reason_code == "malformed_path"


@pytest.mark.parametrize(
    ("scope", "tail", "reason"),
    [
        ("gateway", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "malformed_path"),
        ("user", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "malformed_path"),
        ("inline", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt", "malformed_path"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments", "malformed_path"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results", "malformed_path"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/child", "malformed_path"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt/child", "malformed_path"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/Attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "case_error"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/Tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt", "case_error"),
        ("workspace", "sess-invalid/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid_character"),
        ("workspace", "ses_019c3567da374a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_short", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt.bak", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.TXT", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/arbitrary.txt", "invalid_character"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/%61tt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "invalid_percent_encoding"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb%2Etxt", "invalid_percent_encoding"),
        ("workspace", "ses_019c3567da377a6f855e1bddac8f1f76/attachments/..", "dot_segment"),
    ],
)
def test_session_artifact_templates_reject_before_catalog_access(
    scope: str, tail: str, reason: str,
    resolver_and_bindings: tuple[ResourceUriResolver, _RecordingBindings],
) -> None:
    uri = f"boxteam://{scope}/ws-1/resources/session/{tail}"
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        parse_resource_uri(uri)
    assert excinfo.value.reason_code == reason
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(scope=scope, scope_id="ws-1", kind="session", tail_segments=tail.split("/"))
    assert excinfo.value.reason_code == reason
    resolver, bindings = resolver_and_bindings
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resolver.resolve(uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1"))
    assert excinfo.value.reason_code == reason
    assert bindings.accessed_keys == []


@pytest.mark.parametrize(
    "segments",
    [
        ("ses_019c3567da377a6f855e1bddac8f1f76/attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        ("ses_019c3567da377a6f855e1bddac8f1f76", "attachments/att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("ses_019c3567da377a6f855e1bddac8f1f76/attachments", "att_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("ses_019c3567da377a6f855e1bddac8f1f76/tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt",),
        ("ses_019c3567da377a6f855e1bddac8f1f76", "tool-results/tool_bbbbbbbbbbbbbbbbbbbbbbbb.txt"),
    ],
)
def test_session_artifact_builder_rejects_segment_injection(segments: tuple[str, ...]) -> None:
    with pytest.raises(ResourceUriGrammarError) as excinfo:
        resource_display_uri(scope="workspace", scope_id="ws-1", kind="session", tail_segments=segments)
    assert excinfo.value.reason_code == "invalid_character"


def test_gateway_files_requires_v7_directory_and_real_gateway_identity():
    gateway_id = "gateway_01900000000070008000000000000001"
    directory_id = "01900000000070008000000000000002"
    uri = resource_display_uri(scope="gateway", scope_id=gateway_id, kind="files",
                               tail_segments=(directory_id, "中文 %.txt"))
    assert parse_resource_uri(uri).tail_segments == (directory_id, "中文 %.txt")
    assert "%E4%B8%AD" in uri and "%25" in uri
    for scope_id, root in (("local", directory_id), (gateway_id, "root"),
                           (gateway_id, directory_id.replace("7000", "4000"))):
        with pytest.raises(ResourceUriGrammarError):
            resource_display_uri(scope="gateway", scope_id=scope_id, kind="files", tail_segments=(root,))


@pytest.mark.parametrize("uri,reason", [
    ("/private/native-uri-marker/secret", "unknown_scheme"),
    (r"C:\native-uri-marker\secret", "backslash_rejected"),
    (r"\\native-uri-marker\share\secret", "backslash_rejected"),
    ("file:///private/native-uri-marker/secret", "unknown_scheme"),
    ("boxteam://workspace/root/resources/files/root/x?path=/native-uri-marker", "query_rejected"),
    ("boxteam://workspace/root/resources/files/root/x#native-uri-marker", "fragment_rejected"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker\n", "control_char_rejected"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker 中", "non_ascii_rejected"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker x", "invalid_character"),
    ("BOXTEAM://workspace/native-uri-marker/resources/files/root", "scheme_case_error"),
    ("boxteam://native-uri-marker@host/resources/files/root", "userinfo_rejected"),
    ("boxteam://workspace/native-uri-marker//files/root", "empty_segment"),
    ("boxteam://native-uri-marker/id/resources/config/root", "unknown_scope"),
    ("boxteam://workspace/native-uri-marker/resources/unknown/root", "unknown_resource_kind"),
    ("boxteam://workspace/native-uri-marker/resources/files", "malformed_path"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker%ZZ", "invalid_percent_encoding"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker%FF", "invalid_path_encoding"),
    ("boxteam://workspace/root/resources/files/root/native-uri-marker%2Fsecret", "invalid_character"),
    ("boxteam://workspace/root/resources/session/native-uri-marker/attachments/att_" + "a" * 32, "invalid_character"),
])
def test_uri_grammar_rejections_do_not_echo_input(uri: str, reason: str) -> None:
    with pytest.raises(ResourceUriGrammarError) as captured:
        parse_resource_uri(uri)
    assert captured.value.reason_code == reason
    assert str(captured.value)
    assert "native-uri-marker" not in str(captured.value)


@pytest.mark.parametrize("field,reason", [
    ("scope", "unknown_scope"), ("scope_id", "invalid_character"),
    ("kind", "unknown_resource_kind"), ("gateway_authority", "invalid_character"),
    ("tail_segments", "backslash_rejected"),
])
def test_uri_constructor_rejection_does_not_publish_native_input(field: str, reason: str) -> None:
    arguments = {"scope": "workspace", "scope_id": "root", "kind": "files", "tail_segments": ("root",)}
    arguments[field] = ("root", "native-uri-marker\\secret") if field == "tail_segments" else "/private/native-uri-marker/secret"
    with pytest.raises(ResourceUriGrammarError) as captured:
        resource_display_uri(**arguments)
    assert captured.value.reason_code == reason
    assert "native-uri-marker" not in str(captured.value)
