"""JSONC 单成员编辑必须保留外围原件，不自行解释另一种语法。"""

from __future__ import annotations

import json

import pytest
from boxteam_plugin_sdk.failures import PUBLIC_ERROR_BYTES
from jsonschema import ValidationError as JsonSchemaError

from app.services.infrastructure.config.source_document import (
    parse_source_text,
    redact_source_text,
    restore_source_text,
    set_source_value,
    source_validation_error_detail,
)
from app.services.infrastructure.config.source_uri import config_file_uri
from configs.inheritance.resolver import ConfigNodeValidationError


@pytest.mark.parametrize("uri_length", [0, 13000, 100000])
def test_source_validation_detail_keeps_total_budget_and_never_clips_source_uri(uri_length: int) -> None:
    source_uri = config_file_uri(scope="user", scope_id="local", logical_name="source" + "x" * uri_length)
    raw = JsonSchemaError("private-input", validator="type", validator_value="integer",
                          path=["settings", *["field" * 50] * 5], instance="private-input")
    error = ConfigNodeValidationError(raw, source_uri=source_uri, document_path=True)
    error.__cause__ = raw
    result = source_validation_error_detail(error)
    assert isinstance(result, dict)
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized.encode("utf-8")) <= PUBLIC_ERROR_BYTES
    assert "private-input" not in serialized
    assert source_uri not in result["message"] and "node=" not in result["message"]
    assert result["message"] == "配置节点校验失败: 未满足 JSON schema type 约束: integer"
    assert result["details"]["issues"] == error.issues
    assert "cause" not in result["details"]
    if uri_length < 100000:
        assert result["details"]["source_uri"] == source_uri
    else:
        assert "source_uri" not in result["details"] and result["details"]["truncated"] is True


@pytest.mark.parametrize("text", [
    '{}', '{ // 原注释\n}', '{"mcp":{}}', '{"mcp":{"servers":{}}}',
    '{"other": ["} , //", {}, []], "mcp": {"servers": {},},}',
    '# 顶部\r\n{\r\n  "other": true, // 尾注释\r\n}\r\n',
])
def test_insert_member_preserves_original_outside_new_fragment(text: str) -> None:
    value = {"enabled": False, "transport": "streamable_http", "url": "https://mcp.example/server",
             "headers": {"Authorization": "test-value"}}
    result = set_source_value(text, ("mcp", "servers", "new"), value)
    payload = parse_source_text(result)
    assert payload["mcp"] == {"servers": {"new": value}}
    assert parse_source_text(text).get("other") == payload.get("other")
    if "原注释" in text:
        assert "// 原注释" in result
    if "尾注释" in text:
        assert '// 尾注释\r\n}\r\n' in result and result.startswith('# 顶部\r\n')


def test_replace_only_exact_member_preserves_other_server_and_secret_tokens() -> None:
    prefix = '// 保留\n{"mcp":{"servers":{"other":{"transport":"streamable_http","url":"https://other.example/mcp","headers":{"X-Secret":"\\u0061"}},"target":'
    suffix = ',},"supervision":{}},"ui":{}}\n'
    original = prefix + '{"transport":"streamable_http","url":"https://mcp.example/old"}' + suffix
    expected = {"transport": "streamable_http", "url": "https://mcp.example/target",
                "headers": {"Authorization": "${TARGET_KEY}"}}
    result = set_source_value(original, ("mcp", "servers", "target"), expected)
    assert result.startswith(prefix) and result.endswith(suffix)
    public = redact_source_text(result)
    synthetic = set_source_value(public, ("mcp", "servers", "target"), expected)
    restored_candidate = restore_source_text(public, synthetic)
    assert '${TARGET_KEY}' in restored_candidate and 'boxteam-redacted:/mcp/servers/other/headers/X-Secret' in restored_candidate
    assert restore_source_text(restored_candidate, original) == result
    with pytest.raises(ValueError, match="地址已改变"):
        restore_source_text(public.replace('https://mcp.example/target', 'https://mcp.example/other'), synthetic)


def test_replace_existing_server_keeps_crlf_and_member_indentation() -> None:
    before = '{\r\n  "mcp": {\r\n    "servers": {\r\n      "target": {},\r\n      "other": {}, // 原注释\r\n    },\r\n  },\r\n}\r\n'
    result = set_source_value(before, ("mcp", "servers", "target"), {
        "enabled": False, "transport": "streamable_http", "url": "https://mcp.example/target",
    })
    assert '\n' not in result.replace('\r\n', '')
    assert '      "target": {\r\n        "enabled": false,\r\n        "transport": "streamable_http",\r\n        "url": "https://mcp.example/target"\r\n      },' in result
    assert result.endswith('      "other": {}, // 原注释\r\n    },\r\n  },\r\n}\r\n')
    assert parse_source_text(result)["mcp"] == {"servers": {
        "target": {"enabled": False, "transport": "streamable_http", "url": "https://mcp.example/target"},
        "other": {},
    }}


@pytest.mark.parametrize("text", ['{"mcp":null}', '{"mcp":[]}', '{"mcp":{},"mcp":{}}'])
def test_ambiguous_or_non_object_ancestor_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        set_source_value(text, ("mcp", "servers", "target"), {})


@pytest.mark.parametrize("reference", ["${TARGET_MODEL_KEY}", "secret:sec_00000000000040008000000000000001", ""])
@pytest.mark.parametrize("scope", ["provider", "model"])
@pytest.mark.parametrize("preparsed", [False, True])
def test_model_key_reference_stays_visible_when_connection_is_edited(reference: str, scope: str, preparsed: bool) -> None:
    options = {"endpoint": "https://model.example/old", "api_key": reference}
    original = json.dumps({"config_version": 4, "settings": {}, "profiles": {
        scope: {"settings": {"model_options": options}},
    }})
    public = redact_source_text(original, payload=parse_source_text(original) if preparsed else None)
    assert public == original, "公开引用和无认证不能变成绑定旧地址的秘密占位"
    edited = public.replace("https://model.example/old", "https://model.example/new")
    assert restore_source_text(edited, original) == edited


@pytest.mark.parametrize("preparsed", [False, True])
def test_literal_model_key_and_arbitrary_headers_remain_secret(preparsed: bool) -> None:
    original = json.dumps({"config_version": 4, "settings": {}, "profiles": {"provider": {"settings": {"model_options": {
        "api_key": "literal-key-must-stay-hidden", "endpoint": "https://model.example/old",
        "request_options": {"default_headers": {"X-Custom": "header-must-stay-hidden"}},
    }}}}})
    public = redact_source_text(original, payload=parse_source_text(original) if preparsed else None)
    assert "literal-key-must-stay-hidden" not in public and "header-must-stay-hidden" not in public
    assert restore_source_text(public, original) == original
    with pytest.raises(ValueError, match="地址已改变"):
        restore_source_text(public.replace("https://model.example/old", "https://model.example/new"), original)


@pytest.mark.parametrize("preparsed", [False, True])
def test_public_source_keeps_exact_comments_spacing_and_string_escapes(preparsed: bool) -> None:
    original = '# 保留\r\n{ "title": "\\u0061", "list": [1, null,], // 原注释\r\n}\r\n'
    assert redact_source_text(original, payload=parse_source_text(original) if preparsed else None) == original


@pytest.mark.parametrize("preparsed", [False, True])
def test_header_reference_stays_public_beside_literal_secret(preparsed: bool) -> None:
    reference = {"kind": "mcp_private_header", "workspace_id": "11111111-1111-4111-8111-111111111111",
        "server_id": "secure", "header_name": "Authorization", "endpoint": "https://mcp.example/server", "version": "2" * 32}
    original = '// 保留\n' + json.dumps({"settings": {"workspace": {"mcp": {"servers": {"secure": {
        "headers": {"Authorization": reference, "X-Plain": "private-header"},
    }}}}}}) + '\n'
    expected = original.replace('"private-header"', '"boxteam-redacted:/settings/workspace/mcp/servers/secure/headers/X-Plain"')
    assert redact_source_text(original, payload=parse_source_text(original) if preparsed else None) == expected


@pytest.mark.parametrize("preparsed", [False, True])
def test_duplicate_key_cannot_hide_an_earlier_secret_token(preparsed: bool) -> None:
    original = '{"profiles":{"provider":{"settings":{"model_options":{"api_key":"private-overwritten-key","api_key":""}}}}}'
    assert redact_source_text(original, payload=parse_source_text(original) if preparsed else None) == original.replace('"private-overwritten-key"', '""')


def test_redaction_rejects_invalid_jsonc_without_echoing_body() -> None:
    with pytest.raises(ValueError) as failure:
        redact_source_text('{ "api_key": "private-marker", invalid }')
    assert "private-marker" not in str(failure.value)
