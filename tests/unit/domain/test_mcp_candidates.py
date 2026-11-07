"""配置输入提示与实际解析共用声明规则，安装不依赖工作区输入。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain.mcp_candidates import (
    McpCandidateVariant,
    candidate_parameters,
    parse_candidate_manifest,
    secret_env_input_keys,
)
from app.schemas.internal_v2.mcp import McpCandidateVariantDTO


@pytest.fixture
def package() -> dict[str, object]:
    return {"registryType": "pypi", "identifier": "input-server", "version": "1.0.0", "transport": {"type": "stdio"}}


def variant(package: dict[str, object]) -> McpCandidateVariant:
    result, = parse_candidate_manifest({"name": "inputs", "version": "1", "packages": [package]},
        kind="manual", protocol=None, name="inputs", version="1", entry_id=None).variants
    return result


def test_described_inputs_preserve_original_keys_order_and_repeated_defaults(package: dict[str, object]) -> None:
    package["packageArguments"] = [{"type": "positional", "value": "serve"}, {"type": "named", "name": "--verbose"},
        {"type": "named", "name": "--tag", "isRepeated": True, "isRequired": True,
         "default": ["a", "a", "b"], "choices": ["a", "b"], "description": "有序标签"},
        {"type": "positional", "valueHint": "directory", "format": "filepath"}]
    package["environmentVariables"] = [{"name": "MODE", "default": "normal", "choices": ["normal", "fast"]},
        {"name": "TOKEN", "isSecret": True, "isRequired": True}]
    selected = variant(package)
    described = McpCandidateVariantDTO.model_validate(selected.describe())
    assert described.configuration_unavailable_reason is None
    assert [item.key for item in described.configuration_inputs] == ["--tag", "directory", "MODE", "TOKEN"]
    tag, directory, mode, secret = described.configuration_inputs
    assert tag.default_value == ["a", "a", "b"] and tag.choices == ["a", "b"]
    assert tag.is_repeated and tag.is_required and not tag.can_skip and tag.description == "有序标签"
    assert directory.format == "filepath" and directory.can_skip
    assert mode.default_value == "normal" and mode.can_skip
    assert secret.is_secret and secret.default_value is None and secret.choices is None
    assert secret_env_input_keys(selected) == {"TOKEN"}
    _, installation = candidate_parameters(selected, {}, purpose="installation")
    assert installation["arguments"] == [] and installation["env"] == {}
    _, configuration = candidate_parameters(selected, {"TOKEN": {"kind": "mcp_private_env"}, "directory": "C:\\native\\target"},
        purpose="configuration")
    assert configuration["arguments"] == ["serve", "--verbose", "--tag", "a", "--tag", "a", "--tag", "b", "C:\\native\\target"]
    assert configuration["env"] == {"MODE": "normal", "TOKEN": {"kind": "mcp_private_env"}}


def test_template_projection_requires_explicit_value_without_rejecting_unresolved_number(package: dict[str, object]) -> None:
    package["packageArguments"] = [{"type": "named", "name": "--count", "format": "number", "choices": ["2"],
        "value": "{count}", "variables": {"count": {"default": "2"}}},
        {"type": "named", "name": "--label", "value": "prefix-{label}", "variables": {"label": {}}}]
    selected = variant(package)
    described = McpCandidateVariantDTO.model_validate(selected.describe())
    assert described.configuration_unavailable_reason is None
    assert [(item.key, item.can_skip, item.is_required) for item in described.configuration_inputs] == [
        ("count", False, False), ("label", False, False)]
    _, parameters = candidate_parameters(selected, {"label": ""}, purpose="configuration")
    assert parameters["arguments"] == ["--count", "2", "--label", "prefix-"]
    with pytest.raises(ValueError, match="模板变量缺少"):
        candidate_parameters(selected, {}, purpose="configuration")
    with pytest.raises(ValueError, match="choices"):
        candidate_parameters(selected, {"count": "3", "label": ""}, purpose="configuration")


def test_same_input_is_prompted_once_and_template_occurrence_cannot_be_skipped(package: dict[str, object]) -> None:
    package["packageArguments"] = [{"type": "positional", "valueHint": "mode", "format": "string"},
        {"type": "named", "name": "--mode", "value": "{mode}", "variables": {"mode": {"format": "string"}}},
        {"type": "positional", "valueHint": "mode", "format": "string"}]
    selected = variant(package)
    described = McpCandidateVariantDTO.model_validate(selected.describe())
    only, = described.configuration_inputs
    assert only.key == "mode" and not only.can_skip and not only.is_required
    _, parameters = candidate_parameters(selected, {"mode": ""}, purpose="configuration")
    assert parameters["arguments"] == ["", "--mode", "", ""]


@pytest.mark.parametrize("bad_inputs", [
    {"packageArguments": [{"type": "named", "name": "--password", "isSecret": True}]},
    {"environmentVariables": [{"name": "TOKEN", "isSecret": True, "default": "private-sentinel"}]},
    {"environmentVariables": [{"name": "TOKEN", "isSecret": True, "value": "private-sentinel"}]},
    {"packageArguments": [{"type": "named", "name": "--count", "format": "number", "default": "invalid"}]},
    {"packageArguments": [{"type": "named", "name": "--mode", "default": "bad", "choices": ["good"]}]},
    {"packageArguments": [{"type": "named", "name": "--mode", "value": "{undefined}", "variables": {}}]},
])
def test_configuration_error_is_local_to_variant_and_does_not_block_installation(
    package: dict[str, object], bad_inputs: dict[str, object],
) -> None:
    package.update(bad_inputs)
    selected = variant(package)
    described = McpCandidateVariantDTO.model_validate(selected.describe())
    assert described.configuration_inputs == [] and described.configuration_unavailable_reason
    assert "private-sentinel" not in described.model_dump_json()
    assert candidate_parameters(selected, {}, purpose="installation")[0] == "requires_artifact"
    with pytest.raises((ValueError, TypeError)):
        candidate_parameters(selected, {}, purpose="configuration")


@pytest.mark.parametrize("conflict", ["secret", "repeated", "default"])
def test_input_key_collision_is_rejected_by_projection_execution_and_private_write(
    package: dict[str, object], conflict: str,
) -> None:
    argument: dict[str, object] = {"type": "positional", "valueHint": "TOKEN"}
    if conflict == "secret":
        package["environmentVariables"] = [{"name": "TOKEN", "isSecret": True}]
    else:
        second = deepcopy(argument)
        second.update({"isRepeated": True} if conflict == "repeated" else {"default": "different"})
        package["runtimeArguments"] = [second]
    package["packageArguments"] = [argument]
    selected = variant(package)
    assert "冲突" in str(selected.describe()["configuration_unavailable_reason"])
    with pytest.raises(ValueError, match="冲突"):
        candidate_parameters(selected, {}, purpose="configuration")
    with pytest.raises(ValueError, match="冲突"):
        secret_env_input_keys(selected)


@pytest.mark.parametrize(("definition", "expected"), [({"choices": []}, []), ({}, None)])
def test_empty_and_absent_choices_remain_distinct(package: dict[str, object], definition: dict[str, object], expected: object) -> None:
    package["environmentVariables"] = [{"name": "MODE", **definition}]
    described = McpCandidateVariantDTO.model_validate(variant(package).describe())
    only, = described.configuration_inputs
    assert only.choices == expected
