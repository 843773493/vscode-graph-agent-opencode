from __future__ import annotations

import pytest

from app.agents.policy import (
    custom_tool_spec_names,
    parse_custom_tool_spec,
    parse_custom_tool_specs,
)

TEST_TOOL_SPEC = {
    "name": "test_tool_2",
    "factory": "app.agents.tools.testing:create_test_tool_2",
}


def test_custom_tool_spec_names_reads_names_from_config_spec() -> None:
    assert custom_tool_spec_names([TEST_TOOL_SPEC]) == {"test_tool_2"}


def test_builtin_tool_id_resolves_current_factory_and_preserves_stable_config() -> None:
    specs = parse_custom_tool_specs([{"tool_id": "read_context"}])

    assert specs[0].name == "read_context"
    assert specs[0].factory_path.endswith(":create_read_context_tool")
    assert specs[0].to_config() == {"tool_id": "read_context"}


def test_custom_tool_spec_parser_strips_fields_and_copies_options() -> None:
    options = {"mode": "strict"}

    specs = parse_custom_tool_specs(
        [
            {
                "name": "  test_tool_2  ",
                "factory": "  app.agents.tools.testing:create_test_tool_2  ",
                "options": options,
                "description": "  测试工具  ",
            }
        ]
    )
    options["mode"] = "changed"

    assert specs[0].name == "test_tool_2"
    assert specs[0].factory_path == ("app.agents.tools.testing:create_test_tool_2")
    assert specs[0].options == {"mode": "strict"}
    assert specs[0].description == "测试工具"


def test_custom_tool_spec_parser_rejects_duplicate_normalized_names() -> None:
    with pytest.raises(ValueError, match="重复扩展工具名: test_tool_2"):
        parse_custom_tool_specs(
            [
                TEST_TOOL_SPEC,
                {
                    **TEST_TOOL_SPEC,
                    "name": " test_tool_2 ",
                },
            ]
        )


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        ("name", "   ", ValueError, r"\.name 必须是非空字符串"),
        ("factory", "   ", ValueError, r"\.factory 必须是非空字符串"),
        ("factory", "missing_colon", ValueError, "必须使用"),
        ("options", [], TypeError, "options 必须是对象"),
        ("options", "invalid", TypeError, "options 必须是对象"),
    ],
)
def test_custom_tool_spec_parser_rejects_invalid_fields(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        parse_custom_tool_specs([{**TEST_TOOL_SPEC, field: value}])


def test_custom_tool_spec_parser_rejects_name_only_config() -> None:
    with pytest.raises(ValueError, match="不支持只写工具名"):
        parse_custom_tool_specs(["test_tool_2"])


def test_custom_tool_spec_parses_independent_skill_attribution():
    spec = parse_custom_tool_spec(
        {
            "name": "test_tool_2",
            "factory": "app.agents.tools.testing:create_test_tool_2",
            "skills": ["demo-skill"],
        }
    )

    assert spec.skills == ("demo-skill",)
    assert spec.to_config()["skills"] == ["demo-skill"]


def test_custom_tool_spec_rejects_invalid_skill_attribution():
    with pytest.raises(ValueError, match="重复 Skill 名"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": ["demo", "demo"],
            }
        )
    with pytest.raises(ValueError, match="格式无效"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": ["Bad_Skill"],
            }
        )
    with pytest.raises(TypeError, match="字符串数组"):
        parse_custom_tool_spec(
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
                "skills": "demo",
            }
        )
