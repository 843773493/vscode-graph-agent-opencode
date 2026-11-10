"""私有参数只按原 owner 的精确引用交付，校验错误不复制秘密。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue, Secret

from app.core import file_permissions
from app.services.infrastructure.config.plugin_private_inputs import (
    PluginPrivateInputStore,
    validate_plugin_parameters,
    validate_private_input,
)

_CREDENTIAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["credential"],
    "properties": {
        "credential": {"type": "string", "minLength": 8, "writeOnly": True},
    },
}


@pytest.fixture
def private_inputs(tmp_path: Path) -> PluginPrivateInputStore:
    return PluginPrivateInputStore(
        directory=tmp_path / "credentials/plugins",
        gateway_id="gateway_" + "a" * 32,
        workspace_id=None,
    )


def test_nested_private_json_roundtrip_and_immutable_versions(private_inputs):
    store = private_inputs
    secret = {"authorization": "private-sentinel", "scopes": ["read", "write"]}
    reference = store.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/nested/0/a~1b",
        value=Secret(secret),
    )
    source = {"nested": [{"a/b": reference.model_dump(mode="json")}]}
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"a/b": {"type": "object", "writeOnly": True}},
                },
            },
        },
    }
    assert "private-sentinel" not in json.dumps(source)
    assert store.resolve(source, package_id="probe.tools", plugin_id="probe", schema=schema) == {
        "nested": [{"a/b": secret}],
    }
    second = store.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/nested/0/a~1b",
        value=Secret("replacement"),
    )
    assert second.version != reference.version
    assert store.resolve(source, package_id="probe.tools", plugin_id="probe", schema=schema)["nested"] == [
        {"a/b": secret},
    ]
    if os.name != "nt":
        assert (store.directory / f"{reference.version}.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gateway_id", "other"),
        ("workspace_id", "other"),
        ("package_id", "other"),
        ("plugin_id", "other"),
        ("parameter_pointer", "/other"),
    ],
)
def test_reference_cannot_cross_owner_plugin_or_parameter(private_inputs, field, value):
    store = private_inputs
    reference = store.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    ).model_dump(mode="json")
    reference[field] = value
    with pytest.raises(ValueError, match="不属于") as caught:
        store.resolve(
            {"credential": reference},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )
    assert "private-sentinel" not in str(caught.value)


def test_missing_or_replaced_private_file_is_rejected(private_inputs):
    store = private_inputs
    reference = store.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )
    path = store.directory / f"{reference.version}.json"
    saved = json.loads(path.read_text())

    # 模拟绕过只读保护的外部文件替换，使用共享权限工具显式恢复写权限。
    file_permissions.prepare_readonly_directory_removal(store.directory)
    path.write_text(json.dumps({**saved, "reference": {"plugin_id": "other"}}))
    with pytest.raises(ValueError, match="身份不一致"):
        store.resolve(
            {"credential": reference.model_dump(mode="json")},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )
    path.unlink()
    with pytest.raises(ValueError, match="版本不存在"):
        store.resolve(
            {"credential": reference.model_dump(mode="json")},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )


def test_corrupt_private_file_error_does_not_echo_file_contents(private_inputs):
    sentinel = "corrupt-private-file-secret-sentinel"
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )
    path = private_inputs.directory / f"{reference.version}.json"
    file_permissions.prepare_readonly_directory_removal(private_inputs.directory)
    path.write_text(f'{{"value":"{sentinel}" trailing}}')

    with pytest.raises(ValueError, match="JSON 无效") as caught:
        private_inputs.resolve(
            {"credential": reference.model_dump(mode="json")},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )
    assert sentinel not in str(caught.value)


@pytest.mark.parametrize("serialized", ["null", '["non-object-private-secret-sentinel"]'])
def test_non_object_private_file_has_explicit_error_without_echoing_contents(private_inputs, serialized):
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )
    path = private_inputs.directory / f"{reference.version}.json"
    file_permissions.prepare_readonly_directory_removal(private_inputs.directory)
    path.write_text(serialized)

    with pytest.raises(ValueError, match="文件结构无效") as caught:
        private_inputs.resolve(
            {"credential": reference.model_dump(mode="json")},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )

    assert "non-object-private-secret-sentinel" not in str(caught.value)


def test_write_only_reference_and_resolved_value_both_obey_schema(private_inputs):
    schema = _CREDENTIAL_SCHEMA
    with pytest.raises(ValueError, match="writeOnly") as caught:
        validate_plugin_parameters(schema, {"credential": "private-sentinel"}, {"credential": "private-sentinel"})
    assert "private-sentinel" not in str(caught.value)

    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )
    source = {"credential": reference.model_dump(mode="json")}
    validate_plugin_parameters(schema, source, {"credential": "private-sentinel"})
    with pytest.raises(ValueError, match="minLength"):
        validate_plugin_parameters(schema, source, {"credential": "bad"})

    validate_private_input(schema, source, "/credential", "private-sentinel")
    invalid_value = "short"
    with pytest.raises(ValueError, match="minLength") as invalid:
        validate_private_input(schema, source, "/credential", invalid_value)
    assert invalid_value not in str(invalid.value)


def test_resolved_schema_error_does_not_echo_secret_object_keys():
    secret_key = "private-object-key-sentinel"
    schema = {
        "type": "object",
        "properties": {
            "credential": {
                "type": "object",
                "additionalProperties": {"type": "integer"},
                "writeOnly": True,
            },
        },
    }

    with pytest.raises(ValueError) as caught:
        validate_plugin_parameters(
            schema,
            {"credential": {"kind": "plugin_private_file"}},
            {"credential": {secret_key: "bad"}},
        )
    assert secret_key not in str(caught.value)


def test_malformed_reference_error_does_not_echo_reference_contents(private_inputs):
    malformed_value = "malformed-private-reference-sentinel"
    malformed = {"kind": "plugin_private_file", "version": malformed_value}
    with pytest.raises(ValueError, match="结构无效") as caught:
        private_inputs.resolve(
            {"credential": malformed},
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )
    assert malformed_value not in str(caught.value)


def test_environment_reference_is_resolved_by_owner_and_missing_name_fails(
    private_inputs, monkeypatch: pytest.MonkeyPatch,
):
    environment_name = "PLUGIN_PRIVATE_INPUT_TEST_SECRET"
    secret = "owner-resolved-private-sentinel"
    monkeypatch.setenv(environment_name, secret)
    source = {"credential": f"env:{environment_name}"}

    assert private_inputs.resolve(
        source,
        package_id="probe.tools",
        plugin_id="probe",
        schema=_CREDENTIAL_SCHEMA,
    ) == {"credential": secret}

    monkeypatch.delenv(environment_name)
    with pytest.raises(ValueError, match=environment_name) as caught:
        private_inputs.resolve(
            source,
            package_id="probe.tools",
            plugin_id="probe",
            schema=_CREDENTIAL_SCHEMA,
        )
    assert secret not in str(caught.value)


def test_without_schema_does_not_resolve_private_reference_or_environment(private_inputs, monkeypatch):
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    ).model_dump(mode="json")
    environment_name = "PLUGIN_PRIVATE_INPUT_NO_SCHEMA"
    monkeypatch.setenv(environment_name, "environment-private-sentinel")
    source = {
        "credential": reference,
        "endpoint": f"${{{environment_name}}}",
    }

    assert private_inputs.resolve(
        source,
        package_id="probe.tools",
        plugin_id="probe",
        schema=None,
    ) == source


def test_ordinary_configuration_values_do_not_resolve_private_markers(private_inputs, monkeypatch):
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    ).model_dump(mode="json")
    environment_name = "PLUGIN_PRIVATE_INPUT_PUBLIC_VALUE"
    monkeypatch.setenv(environment_name, "must-remain-an-ordinary-string")
    source = {
        "metadata": {"credential": reference},
        "setting": f"env:{environment_name}",
    }
    schema = {
        "type": "object",
        "properties": {
            "metadata": {"type": "object"},
            "setting": {"type": "string"},
        },
    }

    assert private_inputs.resolve(
        source,
        package_id="probe.tools",
        plugin_id="probe",
        schema=schema,
    ) == source


def test_private_reference_and_actual_value_validation_follow_array_ref_schema(private_inputs):
    pointer = "/entries/0/credential"
    schema = {
        "$defs": {
            "entry": {
                "type": "object",
                "required": ["credential"],
                "additionalProperties": False,
                "properties": {
                    "credential": {"type": "string", "minLength": 8, "writeOnly": True},
                },
            },
        },
        "type": "object",
        "required": ["entries"],
        "additionalProperties": False,
        "properties": {"entries": {"type": "array", "items": {"$ref": "#/$defs/entry"}}},
    }
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer=pointer,
        value=Secret("private-sentinel"),
    )
    source = cast(
        dict[str, JsonValue],
        {"entries": [{"credential": reference.model_dump(mode="json")}]},
    )

    assert private_inputs.resolve(
        source,
        package_id="probe.tools",
        plugin_id="probe",
        schema=schema,
    ) == {"entries": [{"credential": "private-sentinel"}]}
    validate_private_input(schema, source, pointer, "private-sentinel")
    with pytest.raises(ValueError, match="minLength"):
        validate_private_input(schema, source, pointer, "bad")


def test_private_value_validation_respects_selected_oneof_branch(private_inputs):
    schema = {
        "$defs": {
            "first": {
                "properties": {
                    "mode": {"const": "first"},
                    "credential": {"type": "string", "minLength": 8, "writeOnly": True},
                    "endpoint": {"type": "string"},
                },
                "required": ["mode", "credential", "endpoint"],
            },
            "second": {
                "properties": {
                    "mode": {"const": "second"},
                    "credential": {"type": "string"},
                    "endpoint": {"type": "string", "writeOnly": True},
                },
                "required": ["mode", "credential", "endpoint"],
            },
        },
        "type": "object",
        "required": ["mode", "credential"],
        "oneOf": [
            {"$ref": "#/$defs/first"},
            {"$ref": "#/$defs/second"},
        ],
    }
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )
    source = {
        "mode": "first",
        "credential": reference.model_dump(mode="json"),
        "endpoint": "env:PLUGIN_PRIVATE_INPUT_ORDINARY_ENDPOINT",
    }

    validate_private_input(schema, source, "/credential", "private-sentinel")
    with pytest.raises(ValueError, match="minLength"):
        validate_private_input(schema, source, "/credential", "bad")
    assert private_inputs.resolve(
        source,
        package_id="probe.tools",
        plugin_id="probe",
        schema=schema,
    ) == {"mode": "first", "credential": "private-sentinel", "endpoint": source["endpoint"]}


def test_private_reference_rejects_ambiguous_oneof_branch(private_inputs):
    schema = {
        "type": "object",
        "oneOf": [
            {"properties": {"credential": {"type": "string", "writeOnly": True}}},
            {"properties": {"credential": {"type": "string", "writeOnly": True}}},
        ],
    }
    reference = private_inputs.write(
        package_id="probe.tools",
        plugin_id="probe",
        parameter_pointer="/credential",
        value=Secret("private-sentinel"),
    )

    with pytest.raises(ValueError, match="oneOf 分支不能唯一确定"):
        private_inputs.resolve(
            {"credential": reference.model_dump(mode="json")},
            package_id="probe.tools",
            plugin_id="probe",
            schema=schema,
        )
