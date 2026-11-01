"""扩展目录绑定的结构拒绝保留原因，不向公开记录复制动态键或字段值。"""

from __future__ import annotations

import copy

import pytest

from app.core.failures import execution_failure_message
from app.domain.itemized.errors import ItemSchemaError
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingError,
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
)


@pytest.fixture(params=["/private/mcp-binding.sqlite", r"C:\private\mcp-binding.sqlite", r"\\target\private\mcp-binding.sqlite"])
def native_location(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def dispatch_binding() -> ExtensionDispatchBindingRef:
    return ExtensionDispatchBindingRef(
        binding_ref=build_extension_catalog_binding(
            catalog_revision="sha256:" + "a" * 64, generation=1, targets=[],
        ),
        guidance_revision="sha256:" + "b" * 64,
        activation_policy_revision="sha256:" + "c" * 64,
        owner_session_id="session-a", owner_thread_id="thread-a",
        execution_id="execution-a", turn_id=None,
    )


def test_complete_schema_rejection_keeps_dynamic_key_only_in_original_cause(native_location: str) -> None:
    field: dict[str, object] = {"type": "number", "default": float("nan")}
    schema: dict[str, object] = {"type": "object", "properties": {native_location: field}}
    target = ExtensionTargetBindingInput(target_id="custom-tool", origin="custom", input_schema=schema)
    original = repr(schema)
    with pytest.raises(ExtensionCatalogBindingError) as caught:
        build_extension_catalog_binding(catalog_revision="sha256:" + "a" * 64, generation=1, targets=[target])
    error = caught.value
    assert str(error) == "扩展工具完整输入契约 无法 canonical 编码: error=ItemSchemaError"
    assert native_location not in execution_failure_message(error)
    assert isinstance(error.__cause__, ItemSchemaError) and native_location in str(error.__cause__)
    assert repr(schema) == original

    # 修正相同输入后沿原构造器生成绑定，不以空目录代替失败的工具定义。
    field["default"] = 1
    restored = build_extension_catalog_binding(catalog_revision="sha256:" + "a" * 64, generation=1, targets=[target])
    assert set(restored.targets) == {"custom-tool"}
    assert restored.resolve("custom-tool").schema_hash.startswith("sha256:")


def test_snapshot_conversion_rejection_does_not_publish_original_field_value(
    dispatch_binding: ExtensionDispatchBindingRef, native_location: str,
) -> None:
    frozen = dispatch_binding.to_dict()
    damaged = {**frozen, "generation": native_location}
    original = copy.deepcopy(damaged)
    with pytest.raises(ExtensionCatalogUnavailableError) as caught:
        ExtensionDispatchBindingRef.from_sealed_snapshot(damaged)
    error = caught.value
    assert error.code == "extension-catalog-unavailable"
    assert str(error) == "[extension-catalog-unavailable] 历史 extension dispatch binding 字段非法，拒绝 dispatch: error=ValueError"
    assert native_location not in execution_failure_message(error)
    # int() 用 repr 表达非法值，Windows 反斜杠会转义；核对原异常参数而非未转义子串。
    with pytest.raises(ValueError) as original_error:
        int(native_location)
    assert isinstance(error.__cause__, ValueError)
    assert error.__cause__.args == original_error.value.args
    assert damaged == original and dispatch_binding.to_dict() == frozen

    damaged["generation"] = frozen["generation"]
    restored = ExtensionDispatchBindingRef.from_sealed_snapshot(damaged)
    assert restored == dispatch_binding
    assert restored.dispatch_binding_hash == frozen["extension_dispatch_binding_hash"]
