"""候选规则核确切选择及无损参数，不执行 runtime、包管理器或 MCP。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain.mcp_candidates import (
    McpCandidateConflict,
    candidate_parameters,
    parse_candidate_manifest,
)


@pytest.fixture
def package_definition() -> dict[str, object]:
    return {"registryType": "npm", "identifier": "@example/second", "version": "1.2.3", "transport": {"type": "stdio"},
            "runtimeArguments": [{"type": "positional", "value": "--offline"}],
            "packageArguments": [{"type": "positional", "value": "serve"},
                                 {"type": "positional", "value": "literal-$&"},
                                 {"type": "named", "name": "--tag", "isRepeated": True, "isRequired": True}],
            "environmentVariables": [{"name": "MODE", "value": "fixed"}]}


def manifest(definition: dict[str, object]):
    return parse_candidate_manifest({"name": "same", "version": "release-1", "packages": [definition]},
                                    kind="manual", protocol=None, name="same", version="release-1", entry_id=None)


def test_fixed_and_repeated_arguments_preserve_order_and_replacement_text(package_definition: dict[str, object]) -> None:
    variant, = manifest(package_definition).variants
    disposition, parameters = candidate_parameters(variant, {"--tag": ["a", "b"]}, purpose="configuration")
    assert disposition == "requires_artifact"
    assert parameters["arguments"] == ["serve", "literal-$&", "--tag", "a", "--tag", "b"]
    assert parameters["runtime_arguments"] == ["--offline"]
    assert parameters["env"] == {"MODE": "fixed"}


@pytest.mark.parametrize("change", ["unknown", "secret", "choice", "format", "range", "missing_version", "transport", "duplicate_env"])
def test_unrepresentable_or_unpinned_package_is_rejected(package_definition: dict[str, object], change: str) -> None:
    definition = deepcopy(package_definition)
    if change == "unknown":
        definition["unsupportedLaunchField"] = "must-not-drop"
    elif change == "secret":
        definition["environmentVariables"] = [{"name": "TOKEN", "isSecret": True, "value": "private-sentinel"}]
    elif change == "choice":
        definition["environmentVariables"] = [{"name": "MODE", "value": "bad", "choices": ["allowed"]}]
    elif change == "format":
        definition["environmentVariables"] = [{"name": "MODE", "value": "1", "format": "unsupported"}]
    elif change == "range":
        definition["version"] = "^1.2.3"
    elif change == "missing_version":
        definition.pop("version")
    elif change == "transport":
        definition["transport"] = {"type": "sse", "url": "https://service.test/sse"}
    else:
        definition["environmentVariables"] = [{"name": "MODE", "value": "a"}, {"name": "MODE", "value": "b"}]
    variant, = manifest(definition).variants
    with pytest.raises(ValueError) as error:
        candidate_parameters(variant, {"--tag": ["a"]}, purpose="configuration")
    assert "private-sentinel" not in str(error.value)


def test_variant_ref_is_bound_to_manifest_and_exact_ordinal(package_definition: dict[str, object]) -> None:
    raw = {"name": "same", "version": "release-1", "packages": [package_definition, package_definition]}
    document = parse_candidate_manifest(raw, kind="manual", protocol=None, name="same", version="release-1", entry_id=None)
    second = document.variants[1]
    assert document.select(document.digest, second.kind, second.index, second.digest) == second
    with pytest.raises(McpCandidateConflict):
        document.select(document.digest, second.kind, 2, second.digest)
    raw["packages"] = [package_definition]
    updated = parse_candidate_manifest(raw, kind="manual", protocol=None, name="same", version="release-1", entry_id=None)
    with pytest.raises(McpCandidateConflict, match="manifest"):
        updated.select(document.digest, second.kind, second.index, second.digest)


def test_declared_variables_preserve_literal_replacement_and_do_not_accept_unknown_inputs() -> None:
    raw = {"name": "remote", "version": "1", "remotes": [{"type": "streamable-http", "url": "https://service.test/{path}",
           "variables": {"path": {"isRequired": True}}, "headers": [{"name": "X-Mode", "value": "{mode}",
           "variables": {"mode": {"choices": ["$&"]}}}]}]}
    doc = parse_candidate_manifest(raw, kind="manual", protocol=None, name="remote", version="1", entry_id=None)
    disposition, parameters = candidate_parameters(doc.variants[0], {"path": "mcp", "mode": "$&"}, purpose="configuration")
    assert disposition == "configuration_only"
    assert parameters == {"transport": "streamable_http", "url": "https://service.test/mcp", "headers": {"X-Mode": "$&"}}
    with pytest.raises(ValueError, match="未使用"):
        candidate_parameters(doc.variants[0], {"path": "mcp", "mode": "$&", "unused": "wrong"}, purpose="configuration")


@pytest.mark.parametrize("transport", ["sse", "unknown"])
def test_old_sse_and_unknown_remote_transport_are_not_reinterpreted(transport: str) -> None:
    raw = {"name": "remote", "version": "1", "remotes": [{"type": transport, "url": "https://service.test/mcp"}]}
    doc = parse_candidate_manifest(raw, kind="manual", protocol=None, name="remote", version="1", entry_id=None)
    with pytest.raises(ValueError, match="SSE"):
        candidate_parameters(doc.variants[0], {}, purpose="configuration")


@pytest.mark.parametrize(("definition", "values", "message"), [
    ({"isRequired": True}, {}, "必需"),
    ({"isRequired": True}, {"--value": ""}, "不能为空"),
    ({"isRequired": True, "isRepeated": True}, {"--value": []}, "不能为空"),
    ({"format": "unsupported"}, {}, "format"),
    ({"value": "{mode}", "variables": {"mode": {}}, "choices": ["allowed"]}, {"mode": "bad"}, "choices"),
    ({"value": "{mode}", "variables": {"mode": {}}, "format": "number"}, {"mode": "bad"}, "number"),
    ({"value": "{mode}", "variables": {"mode": {}}, "format": "boolean"}, {"mode": "yes"}, "boolean"),
])
def test_required_and_template_constraints_cannot_be_dropped(
    package_definition: dict[str, object], definition: dict[str, object], values: dict[str, object], message: str,
) -> None:
    package_definition["packageArguments"] = [{"type": "named", "name": "--value", **definition}]
    variant, = manifest(package_definition).variants
    with pytest.raises(ValueError, match=message):
        candidate_parameters(variant, values, purpose="configuration")


@pytest.mark.parametrize("argument", [
    {"type": "named", "name": "--value", "valueHint": "ambiguous", "value": "fixed"},
    {"type": "positional", "name": "ambiguous", "value": "fixed"},
])
def test_argument_type_cannot_silently_ignore_conflicting_name(package_definition: dict[str, object], argument: dict[str, object]) -> None:
    package_definition["packageArguments"] = [argument]
    variant, = manifest(package_definition).variants
    with pytest.raises(ValueError, match="不一致"):
        candidate_parameters(variant, {}, purpose="configuration")


def test_duplicate_optional_environment_name_is_rejected_before_missing_value_is_omitted(package_definition: dict[str, object]) -> None:
    package_definition["environmentVariables"] = [{"name": "MODE"}, {"name": "MODE", "value": "fixed"}]
    variant, = manifest(package_definition).variants
    with pytest.raises(ValueError, match="重复"):
        candidate_parameters(variant, {"--tag": ["a"]}, purpose="configuration")


@pytest.mark.parametrize("location", ["envelope", "server", "manual"])
def test_unknown_manifest_fields_are_not_silently_dropped(location: str, package_definition: dict[str, object]) -> None:
    server = {"name": "same", "version": "release-1", "packages": [package_definition]}
    if location == "manual":
        raw = {"name": "same", "version": "release-1", "definition": {"packages": [package_definition], "unknown": "must-not-drop"}}
    else:
        raw = {"server": server, "_meta": {}}
        (raw if location == "envelope" else server)["unknown"] = "must-not-drop"
    with pytest.raises(ValueError, match="不能无损映射"):
        parse_candidate_manifest(raw, kind="manual" if location == "manual" else "gallery", protocol="v0.1",
                                 name="same", version="release-1", entry_id=None)
