"""VRN resolver 单元测试：scope、operation、capability 与访问前拒绝。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    VrnGrammarError,
    parse_vrn,
    skill_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    CatalogBinding,
    VirtualResourceCatalog,
    VirtualResourceResolver,
    VrnResolveError,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    OPERATION_ACTIVATE,
    OPERATION_OBSERVE,
    OPERATION_READ_CONTENT,
    ResolutionContext,
    SemanticResourceDescriptor,
)

_CAPS = frozenset({OPERATION_ACTIVATE, OPERATION_READ_CONTENT})


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


def _catalog(bindings: dict) -> VirtualResourceCatalog:
    name_index = {}
    for uri in bindings:
        parsed = parse_vrn(uri)
        if parsed.kind == "skills":
            name_index[(parsed.scope, parsed.logical_name)] = uri
    return VirtualResourceCatalog(
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
    resolver = VirtualResourceResolver(_catalog({uri: _workspace_binding()}))
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
    resolver = VirtualResourceResolver(_catalog(bindings))
    with pytest.raises(VrnGrammarError):
        resolver.resolve(
            "boxteam://workspace/ws-1/resources/skills/../evil/SKILL.md",
            operation=OPERATION_ACTIVATE,
            context=_workspace_context(),
        )
    with pytest.raises(VrnResolveError) as excinfo:
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
    builtin_uri = skill_display_uri(scope="builtin", scope_id="dist-1", skill_name="code-review")
    memory_uri = "boxteam://memory/session/preference"
    resolver = VirtualResourceResolver(
        _catalog(
            {
                gateway_uri: CatalogBinding(
                    descriptor=_descriptor("res-gw-1", gateway_uri),
                    capabilities=_CAPS,
                    snapshot_ref="cas:1",
                ),
                builtin_uri: CatalogBinding(
                    descriptor=_descriptor("res-b-1", builtin_uri),
                    capabilities=_CAPS,
                    snapshot_ref="cas:2",
                ),
                memory_uri: CatalogBinding(
                    descriptor=_descriptor("res-m-1", memory_uri, kind="memory"),
                    capabilities=_CAPS,
                    snapshot_ref="cas:3",
                ),
            }
        )
    )
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(gateway_uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1", gateway_id="gw-OTHER"))
    assert excinfo.value.reason_code == "scope_mismatch"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(gateway_uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext(workspace_id="ws-1"))
    assert excinfo.value.reason_code == "scope_mismatch"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(
            builtin_uri,
            operation=OPERATION_READ_CONTENT,
            context=ResolutionContext(
                workspace_id="ws-1", gateway_id="gw-1", distribution_id="dist-OTHER"
            ),
        )
    assert excinfo.value.reason_code == "scope_mismatch"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(memory_uri, operation=OPERATION_READ_CONTENT, context=ResolutionContext())
    assert excinfo.value.reason_code == "scope_mismatch"


def test_unknown_resource_operation_capability_and_snapshot() -> None:
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    resolver = VirtualResourceResolver(_catalog({uri: _workspace_binding()}))
    forged = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="ghost")
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(forged, operation=OPERATION_ACTIVATE, context=_workspace_context())
    assert excinfo.value.reason_code == "unknown_resource"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(uri, operation="deploy", context=_workspace_context())
    assert excinfo.value.reason_code == "unknown_operation"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve(uri, operation=OPERATION_OBSERVE, context=_workspace_context())
    assert excinfo.value.reason_code == "capability_denied"
    no_snapshot = CatalogBinding(
        descriptor=_descriptor("res-ws-1", uri), capabilities=_CAPS, snapshot_ref=None
    )
    resolver2 = VirtualResourceResolver(_catalog({uri: no_snapshot}))
    with pytest.raises(VrnResolveError) as excinfo:
        resolver2.resolve(uri, operation=OPERATION_ACTIVATE, context=_workspace_context())
    assert excinfo.value.reason_code == "snapshot_unavailable"


def test_resolve_name_uses_current_catalog_index() -> None:
    uri = skill_display_uri(scope="workspace", scope_id="ws-1", skill_name="code-review")
    resolver = VirtualResourceResolver(_catalog({uri: _workspace_binding()}))
    handle = resolver.resolve_name(
        scope="workspace",
        logical_name="code-review",
        operation=OPERATION_ACTIVATE,
        context=_workspace_context(),
    )
    assert handle.descriptor.resource_id == "res-ws-1"
    with pytest.raises(VrnResolveError) as excinfo:
        resolver.resolve_name(
            scope="workspace",
            logical_name="missing",
            operation=OPERATION_ACTIVATE,
            context=_workspace_context(),
        )
    assert excinfo.value.reason_code == "unknown_resource"
