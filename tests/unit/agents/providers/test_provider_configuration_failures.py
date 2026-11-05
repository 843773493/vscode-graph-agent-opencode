"""模型构建失败在列表与首选模型入口共享公开事实，原异常留在诊断中。"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from app.agents import agent_factory
from app.core import failures
from app.services.infrastructure.config_service import ConfigService


@pytest.fixture(params=["native", "native-text", "validation", "sqlite"])
def model_failure(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Exception, str]:
    location = tmp_path / "private-provider-build" / "credentials.json"
    if request.param in {"native", "native-text"}:
        try:
            location.read_bytes()
        except OSError as error:
            return (error if request.param == "native" else OSError(str(error))), str(location)
        raise AssertionError("缺失凭据文件必须失败")
    if request.param == "validation":
        with pytest.raises(ValidationError) as caught:
            TypeAdapter(dict[str, int]).validate_python({str(location): "invalid"})
        return caught.value, str(location)
    with closing(sqlite3.connect(":memory:")) as connection, pytest.raises(sqlite3.OperationalError) as caught:
        connection.execute("ATTACH DATABASE ? AS provider", (str(location),))
    return caught.value, str(location)


@pytest.fixture
def provider_config() -> dict[str, object]:
    return {"id": "configuration-failure", "model": "test-model", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True}}}


@pytest.fixture
def runtime_config(provider_config: dict[str, object]) -> ConfigService:
    return cast(ConfigService, SimpleNamespace(get_agent_runtime_config=lambda *_args, **_kwargs: {
        "providers": [provider_config], "system_prompt": "test", "runtime_identity_description": "",
    }))


@pytest.mark.parametrize("changed,reason", [
    ({"custom_llm_provider": ""}, "custom_llm_provider"),
    ({"custom_llm_provider": "chatgpt"}, "auth.type='oauth'"),
    ({"api_mode": {"protocol": "anthropic_messages", "model_info": {"supports_reasoning": True}}}, "custom_llm_provider='anthropic'"),
    ({"request_options": {"unknown_option": True}}, "不支持的字段"),
])
def test_local_configuration_rejection_is_shared_by_list_and_execution(provider_config, changed, reason):
    provider = {**provider_config, **changed}
    message = agent_factory.provider_configuration_error(provider)
    assert reason in message
    with pytest.raises((TypeError, ValueError)) as caught:
        agent_factory.build_model_from_provider(provider, {})
    assert str(caught.value) == message


@pytest.mark.parametrize("entry", ["availability", "selected"])
@pytest.mark.parametrize("broken_log", [False, True], ids=["logging", "broken-logging"])
def test_provider_native_failure_does_not_become_a_public_runtime_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    model_failure: tuple[Exception, str], provider_config: dict[str, object], runtime_config: ConfigService,
    entry: str, broken_log: bool,
) -> None:
    error, location = model_failure
    original_args = error.args
    blocked = tmp_path / "provider-log-blocker"
    blocked.write_bytes(b"keep log obstruction")
    diagnostics: list[BaseException] = []

    class FailingLog(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert record.exc_info is not None and record.exc_info[1] is not None
            diagnostics.append(record.exc_info[1])
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    handler = FailingLog()
    if broken_log:
        failures.logger.addHandler(handler)
    try:
        monkeypatch.setattr(agent_factory, "_validate_provider_configuration" if entry == "availability" else "build_model_from_provider", fail)
        if entry == "availability":
            message = agent_factory.provider_configuration_error(provider_config)
            assert message is not None
        else:
            with pytest.raises(RuntimeError) as caught:
                agent_factory.build_runtime_for_agent("default", runtime_config)
            assert caught.value.__cause__ is error
            message = str(caught.value)
            assert "provider_id=configuration-failure" in message
        assert type(error).__name__ in message
        assert location not in message
        assert error.args == original_args
        if broken_log:
            assert diagnostics == [error]
            assert "diagnostic_sink_error=NotADirectoryError" in message
            assert any(str(blocked) in note for note in error.__notes__)
        else:
            assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
            assert location in caplog.text and "configuration-failure" in caplog.text
    finally:
        failures.logger.removeHandler(handler)
        handler.close()
        assert blocked.read_bytes() == b"keep log obstruction"


@pytest.mark.parametrize("entry", ["availability", "selected"])
def test_provider_business_rejection_keeps_its_existing_message(
    monkeypatch: pytest.MonkeyPatch, provider_config: dict[str, object], runtime_config: ConfigService, entry: str,
) -> None:
    error = ValueError("OAuth 凭据缺少账号信息")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(agent_factory, "_validate_provider_configuration" if entry == "availability" else "build_model_from_provider", fail)
    if entry == "availability":
        assert agent_factory.provider_configuration_error(provider_config) == str(error)
    else:
        with pytest.raises(RuntimeError, match="当前选择的模型配置不可用.*OAuth 凭据缺少账号信息") as caught:
            agent_factory.build_runtime_for_agent("default", runtime_config)
        assert caught.value.__cause__ is error
