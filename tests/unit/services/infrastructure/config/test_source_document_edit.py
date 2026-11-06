"""JSONC 单成员编辑必须保留外围原件，不自行解释另一种语法。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.config.source_document import (
    parse_source_text,
    redact_source_text,
    restore_source_text,
    set_source_value,
)


@pytest.mark.parametrize("text", [
    '{}', '{ // 原注释\n}', '{"mcp":{}}', '{"mcp":{"servers":{}}}',
    '{"other": ["} , //", {}, []], "mcp": {"servers": {},},}',
    '# 顶部\r\n{\r\n  "other": true, // 尾注释\r\n}\r\n',
])
def test_insert_member_preserves_original_outside_new_fragment(text: str) -> None:
    value = {"enabled": False, "args": ["escaped\\path", "\"literal"], "env": {"MODE": "value"}}
    result = set_source_value(text, ("mcp", "servers", "new"), value)
    payload = parse_source_text(result)
    assert payload["mcp"] == {"servers": {"new": value}}
    assert parse_source_text(text).get("other") == payload.get("other")
    if "原注释" in text:
        assert "// 原注释" in result
    if "尾注释" in text:
        assert '// 尾注释\r\n}\r\n' in result and result.startswith('# 顶部\r\n')


def test_replace_only_exact_member_preserves_other_server_and_secret_tokens() -> None:
    prefix = '// 保留\n{"mcp":{"servers":{"other":{"env":{"SECRET":"\\u0061"}},"target":'
    suffix = ',},"supervision":{}},"ui":{}}\n'
    original = prefix + '{"args":["old"], "env":{"KEY":"old"}}' + suffix
    expected = {"transport": "stdio", "command": "/target/python", "env": {"KEY": "${TARGET_KEY}"}}
    result = set_source_value(original, ("mcp", "servers", "target"), expected)
    assert result.startswith(prefix) and result.endswith(suffix)
    public = redact_source_text(result)
    synthetic = set_source_value(public, ("mcp", "servers", "target"), expected)
    restored_candidate = restore_source_text(public, synthetic)
    assert '${TARGET_KEY}' in restored_candidate and 'boxteam-redacted:/mcp/servers/other/env/SECRET' in restored_candidate
    assert restore_source_text(restored_candidate, original) == result
    with pytest.raises(ValueError, match="地址已改变"):
        restore_source_text(public.replace('/target/python', '/other/python'), synthetic)


def test_replace_existing_server_keeps_crlf_and_member_indentation() -> None:
    before = '{\r\n  "mcp": {\r\n    "servers": {\r\n      "target": {},\r\n      "other": {}, // 原注释\r\n    },\r\n  },\r\n}\r\n'
    result = set_source_value(before, ("mcp", "servers", "target"), {"enabled": False, "args": ["value"]})
    assert '\n' not in result.replace('\r\n', '')
    assert '      "target": {\r\n        "enabled": false,\r\n        "args": [\r\n          "value"\r\n        ]\r\n      },' in result
    assert result.endswith('      "other": {}, // 原注释\r\n    },\r\n  },\r\n}\r\n')
    assert parse_source_text(result)["mcp"] == {"servers": {"target": {"enabled": False, "args": ["value"]}, "other": {}}}


@pytest.mark.parametrize("text", ['{"mcp":null}', '{"mcp":[]}', '{"mcp":{},"mcp":{}}'])
def test_ambiguous_or_non_object_ancestor_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        set_source_value(text, ("mcp", "servers", "target"), {})
