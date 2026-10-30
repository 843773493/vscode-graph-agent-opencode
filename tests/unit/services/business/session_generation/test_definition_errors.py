"""生成器保留真实 Schema 校验与诊断，公开错误不复制输入或动态键。"""

from __future__ import annotations

import errno
import re
from collections.abc import Callable

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationDefinitionDTO,
)
from app.services.business.session_generation.definition import (
    GenerationProviderUnavailable,
    resolve_generation_provider,
)
from app.services.business.session_generation.providers import (
    AgentPromptGenerationProvider,
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 真实日志写入故障。
)


@pytest.fixture
def definition_factory() -> Callable[[dict[str, object]], SessionGenerationDefinitionDTO]:
    def build(config: dict[str, object]) -> SessionGenerationDefinitionDTO:
        return SessionGenerationDefinitionDTO.model_validate({
            "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
            "name": "校验定义", "config": config,
            "placement": {"kind": "workspace", "workspace_id": "ws_test"},
            "execution_workspace_id": "ws_test", "context_source": {"kind": "fresh"},
            "session_strategy": {"mode": "new_per_run"},
        })
    return build


@pytest.mark.parametrize("native", ["/var/private/generator-secret", r"C:\private\generator-secret"])
@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
@pytest.mark.parametrize("case", ["value", "unknown", "nested", "root-key", "array", "required", "enum", "pattern"])
def test_schema_errors_keep_rules_and_original_diagnostics(
    definition_factory: Callable[[dict[str, object]], SessionGenerationDefinitionDTO],
    diagnostic_sink: DiagnosticFailure,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    native: str, log_fails: bool, case: str,
) -> None:
    string = {"type": "string"}
    schemas: dict[str, dict[str, object]] = {
        "value": {"properties": {"prompt": string}},
        "unknown": {"additionalProperties": False},
        "nested": {"properties": {"resources": {"additionalProperties": string}}},
        "root-key": {"additionalProperties": string},
        "array": {"properties": {"resources": {"items": {"additionalProperties": string}}}},
        "required": {"required": [native]},
        "enum": {"properties": {"prompt": {"enum": ["accepted"]}}},
        "pattern": {"properties": {"prompt": {"pattern": re.escape(native)}}},
    }
    configs: dict[str, dict[str, object]] = {
        "value": {"prompt": {native: native}}, "unknown": {native: native},
        "nested": {"resources": {native: 17}}, "root-key": {native: 17},
        "array": {"resources": [{native: 17}]}, "required": {},
        "enum": {"prompt": native}, "pattern": {"prompt": "unmatched"},
    }
    coordinates = {
        "value": ("prompt", 1, "type"), "unknown": (None, 0, "additionalProperties"),
        "nested": ("resources", 2, "type"), "root-key": (None, 1, "type"),
        "array": ("resources", 3, "type"), "required": (None, 0, "required"),
        "enum": ("prompt", 1, "enum"), "pattern": ("prompt", 1, "pattern"),
    }
    provider = AgentPromptGenerationProvider()
    provider.config_schema = {"type": "object", **schemas[case]}
    original = Draft202012Validator.validate
    causes: list[ValidationError] = []

    def validate(validator: Draft202012Validator, instance: object) -> None:
        try:
            original(validator, instance)
        except ValidationError as error:
            causes.append(error)
            if log_fails:
                diagnostic_sink.primary = error
            raise

    monkeypatch.setattr(Draft202012Validator, "validate", validate)
    with pytest.raises(ValueError) as caught:
        resolve_generation_provider({provider.type_id: provider}, definition_factory(configs[case]))
    error = caught.value
    parameter, depth, rule = coordinates[case]
    assert len(causes) == 1 and error.__cause__ is causes[0]
    if case == "pattern":
        assert causes[0].validator_value == re.escape(native)
    else:
        assert "generator-secret" in str(causes[0])
    assert native not in str(error) and "generator-secret" not in str(error)
    assert f"type_id={provider.type_id}" in str(error)
    assert "stage=session-generation-config, error=ValidationError" in str(error)
    assert f"parameter={parameter}, depth={depth}, rule={rule}" in str(error)
    assert any(record.exc_info and record.exc_info[1] is causes[0] for record in caplog.records)
    if log_fails:
        assert [item.errno for item in diagnostic_sink.errors] == [errno.ENOTDIR]
        assert "diagnostic_sink_error=NotADirectoryError" in str(error)
        assert str(diagnostic_sink.destination) not in str(error)
        assert str(diagnostic_sink.destination) in "\n".join(causes[0].__notes__)
    else:
        assert diagnostic_sink.errors == []
        assert "diagnostic_sink_error" not in str(error)


def test_valid_config_returns_original_provider(
    definition_factory: Callable[[dict[str, object]], SessionGenerationDefinitionDTO],
) -> None:
    provider = AgentPromptGenerationProvider()
    assert resolve_generation_provider(
        {provider.type_id: provider}, definition_factory({"prompt": "继续执行"}),
    ) is provider


@pytest.mark.parametrize("unavailable", ["missing", "version"])
def test_unavailable_provider_keeps_existing_business_reason(
    definition_factory: Callable[[dict[str, object]], SessionGenerationDefinitionDTO], unavailable: str,
) -> None:
    provider = AgentPromptGenerationProvider()
    definition = definition_factory({"prompt": "继续执行"})
    if unavailable == "version":
        definition.generator_type.version = "unsupported"
    providers = {} if unavailable == "missing" else {provider.type_id: provider}
    with pytest.raises(GenerationProviderUnavailable) as caught:
        resolve_generation_provider(providers, definition)
    assert str(caught.value) == (
        "工作区未注册会话生成器类型: builtin.agent_prompt" if unavailable == "missing"
        else "工作区不支持会话生成器版本: type_id=builtin.agent_prompt, version=unsupported"
    )
