from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from configs.runtime import validate_config


@pytest.mark.parametrize("native", ["/private/config/secret-marker", r"C:\private\config\secret-marker"])
@pytest.mark.parametrize("case", ["value", "key", "dynamic", "schema", "policy"])
def test_validation_rejects_without_echoing_input_or_schema(
    tmp_path: Path, native: str, case: str,
) -> None:
    schema: dict[str, object] = {"type": "object"}
    config: dict[str, object] = {}
    if case == "value":
        schema["properties"] = {"field": {"type": "integer"}}
        config["field"] = native
    elif case == "key":
        schema["additionalProperties"] = False
        config[native] = "unused"
    elif case == "dynamic":
        schema["additionalProperties"] = {"type": "array", "items": {"type": "integer"}}
        config[native] = [native]
    elif case == "schema":
        schema["type"] = native
    else:
        schema["x-boxteam-config-domain"] = "gateway"
        schema["x-boxteam-policy-manifest"] = [native]
    schema_path = tmp_path / "schema.jsonc"
    schema_bytes = json.dumps(schema).encode()
    schema_path.write_bytes(schema_bytes)
    before = json.dumps(config)

    with pytest.raises(ValueError, match="配置.*失败") as failure:
        validate_config(config, schema_path=schema_path)

    assert "secret-marker" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    cause = failure.value.__cause__
    assert cause is not None
    assert "secret-marker" in str(cause)
    if case != "policy":
        assert isinstance(cause, (jsonschema.ValidationError, jsonschema.SchemaError))
        assert "rule=" in str(failure.value)
        assert "location=" in str(failure.value)
    assert json.dumps(config) == before
    assert schema_path.read_bytes() == schema_bytes


def test_validation_returns_the_original_valid_config(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.jsonc"
    schema_path.write_text('{"type":"object","properties":{"field":{"type":"integer"}}}')
    config: dict[str, object] = {"field": 7}
    assert validate_config(config, schema_path=schema_path) is config


@pytest.mark.parametrize("reference_kind", ["fragment", "file"])
def test_unresolved_schema_reference_does_not_echo_reference(tmp_path: Path, reference_kind: str) -> None:
    reference = "#/$defs/secret-marker" if reference_kind == "fragment" else (tmp_path / "secret-marker.json").as_uri()
    schema_path = tmp_path / "schema.jsonc"
    schema_path.write_text(json.dumps({"$ref": reference}))
    with pytest.raises(ValueError, match="stage=schema_reference") as failure:
        validate_config({}, schema_path=schema_path)
    assert "secret-marker" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    assert failure.value.__cause__ is not None
    assert reference in str(failure.value.__cause__)
