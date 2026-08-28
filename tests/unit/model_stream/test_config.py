from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.testing.model_stream import (
    ModelStreamConfigError,
    load_model_stream_config,
    load_model_stream_config_from_environment,
)

CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream.jsonc"
RESPONSES_CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream_responses.jsonc"
RESPONSES_REASONING_TEXT_CONFIG_PATH = (
    Path.cwd() / "configs" / "tests" / "model_stream_responses_reasoning_text.jsonc"
)


def test_model_stream_config_uses_explicit_defaults() -> None:
    config = load_model_stream_config(CONFIG_PATH)

    assert config.transport.mode == "replay"
    assert config.transport.scenario_id == "reasoning-tool"
    assert config.transport.matching_policy == "strict"
    assert config.transport.replay_policy == "request_reusable"
    assert config.transport.timing_mode == "none"
    assert config.transport.fixture_root == (
        Path.cwd() / "tests" / "fixtures" / "model_stream"
    ).resolve()


def test_missing_environment_keeps_transport_disabled() -> None:
    assert load_model_stream_config_from_environment(environment={}) is None


def test_responses_config_selects_responses_scenario_without_protocol_duplication() -> None:
    config = load_model_stream_config(RESPONSES_CONFIG_PATH)

    assert config.transport.scenario_id == "responses-reasoning-tool"
    assert config.transport.fixture_root == (
        Path.cwd() / "tests" / "fixtures" / "model_stream"
    ).resolve()


def test_responses_reasoning_text_config_is_an_explicit_alternate() -> None:
    config = load_model_stream_config(RESPONSES_REASONING_TEXT_CONFIG_PATH)

    assert config.transport.scenario_id == "responses-reasoning-text"


def test_invalid_environment_config_fails_with_schema_context(tmp_path: Path) -> None:
    schema_path = CONFIG_PATH.parent / "model_stream_schema.jsonc"
    path = tmp_path / "invalid-model-stream.jsonc"
    path.write_text(
        json.dumps(
            {
                "$schema": str(schema_path),
                "config_version": 1,
                "model_stream": {
                    "transport": {
                        "mode": "replay",
                        "fixture_root": "tests/fixtures/model_stream",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelStreamConfigError, match="scenario_id"):
        load_model_stream_config_from_environment(
            environment={"BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(path)},
            project_root=Path.cwd(),
        )


def test_missing_environment_config_path_fails_explicitly(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-model-stream.jsonc"

    with pytest.raises(FileNotFoundError, match="missing-model-stream.jsonc"):
        load_model_stream_config_from_environment(
            environment={"BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(missing_path)},
            project_root=Path.cwd(),
        )


def test_invalid_transport_mode_fails_schema_validation(tmp_path: Path) -> None:
    schema_path = CONFIG_PATH.parent / "model_stream_schema.jsonc"
    path = tmp_path / "invalid-mode-model-stream.jsonc"
    raw_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw_config["$schema"] = str(schema_path)
    raw_config["model_stream"]["transport"]["mode"] = "invalid"
    path.write_text(json.dumps(raw_config), encoding="utf-8")

    with pytest.raises(ModelStreamConfigError, match="mode"):
        load_model_stream_config(path)
