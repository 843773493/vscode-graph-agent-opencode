"""fork 资源策略只传递逻辑引用，原生路径输入与 Browser 页面例外不适用于此。"""

from dataclasses import replace
from typing import Literal

import pytest

from app.core.resource_uri import ResourceUriGrammarError
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetContext,
    ResourceTargetDecision,
    ResourceTargetFacts,
    RuntimeResourceTargetFacts,
)

_SOURCE_URI = "boxteam://workspace/ws-source/resources/files/root/missing.txt"
_TARGET_URI = "boxteam://gw-target/workspace/ws-target/resources/files/root/missing.txt"
_INVALID_REFERENCES = [
    ("", "empty_uri"),
    ("/work/private/file.txt", "unknown_scheme"),
    ("relative/file.txt", "unknown_scheme"),
    (r"C:\private\file.txt", "backslash_rejected"),
    (r"\\server\private\file.txt", "backslash_rejected"),
    ("file:///work/private/file.txt", "unknown_scheme"),
    ("resource-identity-1", "unknown_scheme"),
    (_SOURCE_URI + "?revision=2", "query_rejected"),
    (_SOURCE_URI + "#sha256:123", "fragment_rejected"),
]


@pytest.fixture
def source() -> ResourceTargetFacts:
    return ResourceTargetFacts(
        resource_uri=_SOURCE_URI, storage_domain_id="ws-source", lifecycle="snapshot",
        available=True, required_for_context=True, snapshot_import_supported=True,
        revision="revision-1", content_hash="sha256:123", content_length=3,
    )


@pytest.fixture
def decision() -> ResourceTargetDecision:
    return ResourceTargetDecision(
        resource_uri=_SOURCE_URI, target_uri=_TARGET_URI,
        action="reuse_reference", available=True,
    )


@pytest.mark.parametrize(("value", "reason"), _INVALID_REFERENCES)
def test_source_facts_reject_invalid_reference_before_policy(
    source: ResourceTargetFacts, value: str, reason: str,
) -> None:
    with pytest.raises(ResourceUriGrammarError) as error:
        replace(source, resource_uri=value)
    assert error.value.reason_code == reason
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize("field", ["resource_uri", "target_uri"])
@pytest.mark.parametrize(("value", "reason"), _INVALID_REFERENCES)
def test_decision_rejects_invalid_reference_even_when_omitted(
    decision: ResourceTargetDecision, field: str, value: str, reason: str,
) -> None:
    # omit 也不能借不可用状态把非法地址复制到后续错误或持久决策中。
    with pytest.raises(ResourceUriGrammarError) as error:
        replace(decision, action="omit", available=False, **{field: value})
    assert error.value.reason_code == reason
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize("uri", [
    _SOURCE_URI, _TARGET_URI,
    "boxteam://workspace/ws-source/resources/files/root/encoded%20name.txt",
])
def test_logical_references_preserve_original_bytes_and_independent_revision(
    source: ResourceTargetFacts, decision: ResourceTargetDecision, uri: str,
) -> None:
    # 不查当前 catalog 或文件是否存在，也不把远端 authority 归成本机地址。
    revised = replace(source, resource_uri=uri, revision="revision-2")
    assert revised.resource_uri == uri
    assert replace(decision, resource_uri=uri, target_uri=uri).target_uri == uri
    result = check_resource_target(revised, ResourceTargetContext(storage_domain_id="other"))
    assert result.resource_uri == uri
    assert result.action == "copy_snapshot"


def test_policy_preserves_distinct_source_and_existing_target(
    source: ResourceTargetFacts,
) -> None:
    existing = replace(source, resource_uri=_TARGET_URI, storage_domain_id="ws-target", lifecycle="workspace")
    result = check_resource_target(source, ResourceTargetContext(
        storage_domain_id="ws-target", existing_reference=existing,
    ))
    assert result.resource_uri == _SOURCE_URI
    assert result.target_uri == _TARGET_URI
    assert result.action == "reuse_reference"


@pytest.mark.parametrize("kind", ["terminal", "browser"])
def test_runtime_owner_without_resource_address_keeps_explicit_null(kind: Literal["terminal", "browser"]) -> None:
    runtime = RuntimeResourceTargetFacts(
        kind=kind, resource_id="runtime-1", workspace_id="ws-source",
        retention_scope="workspace", source_thread=("session-1", "thread-1"),
        thread_associations=(("session-1", "thread-1"),), owner_revision=2,
        status="running", reconciliation_status="verified", available_actions=("attach",),
    )
    result = check_resource_target(ResourceTargetFacts(
        resource_uri=None, storage_domain_id="ws-source", lifecycle="runtime",
        available=True, runtime=runtime,
    ), ResourceTargetContext(storage_domain_id="ws-source", workspace_id="ws-source"), choice="reuse_reference")
    assert result.resource_uri is None
    assert result.target_uri is None
    assert result.runtime is runtime
    assert result.action == "reuse_reference"
