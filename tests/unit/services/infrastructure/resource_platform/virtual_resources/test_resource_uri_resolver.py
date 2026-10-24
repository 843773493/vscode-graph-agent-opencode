"""URI resolver 单元测试：scope、operation、capability 与访问前拒绝。"""

from __future__ import annotations

import pytest

from app.core.resource_uri import (
    ResourceUriGrammarError,
    parse_resource_uri,
    skill_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    RESOURCE_URI_RESOLVE_REASON_CODES,
    CatalogBinding,
    ResourceUriCatalog,
    ResourceUriResolveError,
    ResourceUriResolver,
    require_scope_binding,
    resource_navigation_node,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    OPERATION_ACTIVATE,
    OPERATION_OBSERVE,
    OPERATION_READ_CONTENT,
    ResolutionContext,
    SemanticResourceDescriptor,
)

_CAPS = frozenset({OPERATION_ACTIVATE, OPERATION_READ_CONTENT})


def test_resolve_error_codes_remain_closed() -> None:
    expected = {
        "scope_mismatch", "unknown_resource", "unknown_operation", "capability_denied",
        "snapshot_unavailable", "historical_snapshot_missing", "unsupported_view",
    }
    assert RESOURCE_URI_RESOLVE_REASON_CODES == expected
    for code in expected:
        assert ResourceUriResolveError(code, "解析拒绝").reason_code == code


@pytest.mark.parametrize("code", [
    "unregistered_reason", "unknown_scope", "invalid_path_encoding",
    "federation-unknown-peer", "target_not_resolvable", "federation-capability-denied",
])
def test_resolve_error_rejects_unregistered_and_other_domain_codes(code: str) -> None:
    with pytest.raises(ValueError, match="未知 ResourceUriResolveError reason_code"):
        ResourceUriResolveError(code, "不得把语法或联邦错误码用于 resolver")


class _RecordingBindings(dict):
    """记录访问次数的 catalog 绑定表；用于证明拒绝先于任何查表。"""

    def __init__(self) -> None:
        super().__init__()
        self.access_count = 0

    def __getitem__(self, key):
        self.access_count += 1
        return super().__getitem__(key)


def _descriptor(resource_id: str, display_uri: str, *, kind: str = "skills") -> SemanticResourceDescriptor:
    return SemanticResourceDescriptor(
        resource_id=resource_id,
        source_id=f"src-{resource_id}",
        kind=kind,
        display_uri=display_uri,
        semantic_revision="rev-1",
        semantic_hash="hash-1",
    )


def _catalog(bindings: dict) -> ResourceUriCatalog:
    name_index = {}
    for uri in bindings:
        parsed = parse_resource_uri(uri)
        if parsed.kind == "skills":
            name_index[(parsed.scope, parsed.logical_name)] = uri
    return ResourceUriCatalog(
        bindings=bindings,
        name_index=name_index,
    )


def _workspace_binding() -> CatalogBinding:
    return CatalogBinding(
        descriptor=_descriptor(
            "res-ws-1",
            skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review"),
        ),
        capabilities=_CAPS,
        snapshot_ref="cas:sha256:aaaa",
    )


def _workspace_context() -> ResolutionContext:
    return ResolutionContext(
        workspace_id="ws-1", gateway_id="gw-1", distribution_id="dist-1"
    )


def test_resolve_returns_typed_handle_with_provenance() -> None:
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    resolver = ResourceUriResolver(_catalog({uri: _workspace_binding()}))
    handle = resolver.resolve(
        uri, operation=OPERATION_ACTIVATE, context=_workspace_context()
    )
    assert handle.descriptor.resource_id == "res-ws-1"
    assert handle.snapshot_ref == "cas:sha256:aaaa"
    assert handle.provenance.resource_id == "res-ws-1"
    assert handle.provenance.semantic_revision == "rev-1"


def test_grammar_and_scope_reject_before_catalog_access() -> None:
    bindings = _RecordingBindings()
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    bindings[uri] = _workspace_binding()
    resolver = ResourceUriResolver(_catalog(bindings))
    with pytest.raises(ResourceUriGrammarError):
        resolver.resolve(
            "boxteam://workspace/ws-1/resources/skills/../evil/SKILL.md",
            operation=OPERATION_ACTIVATE,
            context=_workspace_context(),
        )
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            skill_display_uri(
                scope="workspace", scope_id="ws-OTHER", skill_name="code-review"
            ),
            operation=OPERATION_ACTIVATE,
            context=_workspace_context(),
        )
    assert excinfo.value.reason_code == "scope_mismatch"
    assert bindings.access_count == 0


def test_scope_mismatch_per_scope() -> None:
    gateway_uri = skill_display_uri(scope="gateway", scope_id="gw-1", skill_name="code-review")
    inline_uri = skill_display_uri(scope="inline", scope_id="dist-1", skill_name="code-review")
    resolver = ResourceUriResolver(
        _catalog(
            {
                gateway_uri: CatalogBinding(
                    descriptor=_descriptor("res-gw-1", gateway_uri),
                    capabilities=_CAPS,
                    snapshot_ref="cas:1",
                ),
                inline_uri: CatalogBinding(
                    descriptor=_descriptor("res-b-1", inline_uri),
                    capabilities=_CAPS,
                    snapshot_ref="cas:2",
                ),
            }
        )
    )
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(gateway_uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1", gateway_id="gw-OTHER"))
    assert excinfo.value.reason_code == "scope_mismatch"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(gateway_uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1"))
    assert excinfo.value.reason_code == "scope_mismatch"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            inline_uri,
            operation=OPERATION_READ_CONTENT,
            context=ResolutionContext(
                workspace_id="ws-1", gateway_id="gw-1", distribution_id="dist-OTHER"
            ),
        )
    assert excinfo.value.reason_code == "scope_mismatch"


def test_unknown_resource_operation_capability_and_snapshot() -> None:
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    resolver = ResourceUriResolver(_catalog({uri: _workspace_binding()}))
    forged = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="ghost")
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(forged, operation=OPERATION_ACTIVATE, context=_workspace_context())
    assert excinfo.value.reason_code == "unknown_resource"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(uri, operation="deploy", context=_workspace_context())
    assert excinfo.value.reason_code == "unknown_operation"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(uri, operation=OPERATION_OBSERVE, context=_workspace_context())
    assert excinfo.value.reason_code == "capability_denied"
    no_snapshot = CatalogBinding(
        descriptor=_descriptor("res-ws-1", uri), capabilities=_CAPS, snapshot_ref=None
    )
    resolver2 = ResourceUriResolver(_catalog({uri: no_snapshot}))
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver2.resolve(uri, operation=OPERATION_ACTIVATE, context=_workspace_context())
    assert excinfo.value.reason_code == "snapshot_unavailable"


def test_resolve_name_uses_current_catalog_index() -> None:
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    resolver = ResourceUriResolver(_catalog({uri: _workspace_binding()}))
    handle = resolver.resolve_name(
        scope="workspace",
        logical_name="code-review",
        operation=OPERATION_ACTIVATE,
        context=_workspace_context(),
    )
    assert handle.descriptor.resource_id == "res-ws-1"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve_name(
            scope="workspace",
            logical_name="missing",
            operation=OPERATION_ACTIVATE,
            context=_workspace_context(),
        )
    assert excinfo.value.reason_code == "unknown_resource"


def test_config_and_session_kinds_reach_catalog_lookup() -> None:
    # config/session 已进语法闭集，必须在 parser 之后贯通 resolver 门卫链：scope
    # 校验先行，随后落到 catalog 查询（当前无 owner 登记其描述符，故 unknown_resource
    # 是正确 fail-closed 结果，而不是在 grammar 期被 unknown_resource_kind 拒绝）。
    resolver = ResourceUriResolver(_catalog({}))
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            "boxteam://workspace/ws-1/resources/session/sess-1",
            operation=OPERATION_READ_CONTENT,
            context=_workspace_context(),
        )
    assert excinfo.value.reason_code == "unknown_resource"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            "boxteam://inline/source-development-0_0_2/resources/config/workspace_inline",
            operation=OPERATION_READ_CONTENT,
            context=ResolutionContext(
                workspace_id="ws-1", distribution_id="source-development-0_0_2"
            ),
        )
    assert excinfo.value.reason_code == "unknown_resource"


def test_config_and_session_still_fail_scope_binding() -> None:
    resolver = ResourceUriResolver(_catalog({}))
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            "boxteam://workspace/ws-OTHER/resources/session/sess-1",
            operation=OPERATION_READ_CONTENT,
            context=_workspace_context(),
        )
    assert excinfo.value.reason_code == "scope_mismatch"


def test_user_scope_binding_accepts_local_and_rejects_other() -> None:
    # ``user`` scope 的 principal 是单用户本地程序约定值 ``local``（见
    # app/core/user_identity.py）：与 workspace/gateway/inline 同纪律，显式携带并
    # 参与绑定校验，既非硬编码跳过、也非悬空 scope。
    require_scope_binding(
        "user", "local", ResolutionContext(user_scope_id="local")
    )
    with pytest.raises(ResourceUriResolveError) as excinfo:
        require_scope_binding(
            "user", "local", ResolutionContext(user_scope_id="someone")
        )
    assert excinfo.value.reason_code == "scope_mismatch"
    # 未绑定（None）同样拒绝，MUST NOT 回退到隐含上下文补全。
    with pytest.raises(ResourceUriResolveError) as excinfo:
        require_scope_binding("user", "local", ResolutionContext())
    assert excinfo.value.reason_code == "scope_mismatch"


def test_user_scope_resolves_through_catalog() -> None:
    uri = skill_display_uri(scope="user", scope_id="local", skill_name="code-review")
    resolver = ResourceUriResolver(
        _catalog(
            {
                uri: CatalogBinding(
                    descriptor=_descriptor("res-user-1", uri),
                    capabilities=frozenset({OPERATION_READ_CONTENT}),
                    snapshot_ref="cas:sha256:user",
                )
            }
        )
    )
    handle = resolver.resolve(
        uri,
        operation=OPERATION_READ_CONTENT,
        context=ResolutionContext(user_scope_id="local"),
    )
    assert handle.descriptor.resource_id == "res-user-1"
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resolver.resolve(
            uri,
            operation=OPERATION_READ_CONTENT,
            context=ResolutionContext(user_scope_id="someone"),
        )
    assert excinfo.value.reason_code == "scope_mismatch"


@pytest.mark.parametrize(
    ("uri", "shape", "operations", "child_path"),
    [
        ("boxteam://workspace/ws-1/resources/files/root", "directory", ("describe", "list", "search"), "file_segments"),
        ("boxteam://workspace/ws-1/resources/attachments/att_00000000000000000000000000000000", "directory", ("describe", "list", "search"), "file_segments"),
        ("boxteam://workspace/ws-1/resources/attachments/att_00000000000000000000000000000000/derived", "directory", ("describe", "list", "search"), "file_segments"),
        ("boxteam://workspace/ws-1/resources/attachments/att_00000000000000000000000000000000/original/content", "file", ("describe", "read", "search"), None),
        ("boxteam://inline/dist-1/resources/skills/review", "directory", ("describe", "list", "search"), "file_segments"),
        ("boxteam://workspace/ws-1/resources/skills/review/references", "directory", ("describe", "list", "search"), "file_segments"),
        ("boxteam://workspace/ws-1/resources/skills/review/SKILL.md", "file", ("describe", "read", "search"), None),
        ("boxteam://workspace/ws-1/resources/agent-spec/root/AGENTS.md", "file", ("describe", "read"), None),
        ("boxteam://inline/dist-1/resources/config/workspace_inline", "file", ("describe", "read"), None),
        ("boxteam://workspace/ws-1/resources/session/sess-1", "domain_object", ("describe",), None),
    ],
)
def test_fixed_navigation_templates_publish_shape_and_capabilities(uri, shape, operations, child_path) -> None:
    node = resource_navigation_node(parse_resource_uri(uri), resource_shape=shape)
    assert node.resource_shape == shape
    assert node.operations == operations
    assert node.child_path == child_path
    assert not {"activate", "write", "patch"} & set(node.operations)


@pytest.mark.parametrize(
    ("uri", "shape"),
    [
        ("boxteam://workspace/ws-1/resources/files/root", "file"),
        ("boxteam://workspace/ws-1/resources/attachments/att_00000000000000000000000000000000", "file"),
        ("boxteam://workspace/ws-1/resources/attachments/att_00000000000000000000000000000000/derived", "domain_object"),
        ("boxteam://workspace/ws-1/resources/skills/review", "file"),
        ("boxteam://inline/dist-1/resources/config/workspace_inline/guess", "file"),
        ("boxteam://workspace/ws-1/resources/session/sess-1/checkpoints", "directory"),
    ],
)
def test_navigation_cannot_turn_files_or_domain_objects_into_directories(uri, shape) -> None:
    with pytest.raises(ResourceUriResolveError) as excinfo:
        resource_navigation_node(parse_resource_uri(uri), resource_shape=shape)
    assert excinfo.value.reason_code == "capability_denied"
