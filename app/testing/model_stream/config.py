from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Literal, cast

import jsonschema

from configs.runtime import read_jsonc_object

from .errors import ModelStreamConfigError

ModelStreamMode = Literal["off", "record", "replay"]
MatchingPolicy = Literal["strict"]
ReplayPolicy = Literal["request_reusable", "session_sequence"]
TimingMode = Literal["none"]

MODEL_STREAM_CONFIG_ENV = "BOXTEAM_TEST_MODEL_STREAM_CONFIG"
DEFAULT_ARTIFACT_ROOT = Path("out/tests/temp/model_stream/artifacts")


@dataclass(frozen=True, slots=True)
class ModelStreamTransportConfig:
    mode: ModelStreamMode
    scenario_id: str | None
    fixture_root: Path | None
    matching_policy: MatchingPolicy
    replay_policy: ReplayPolicy
    timing_mode: TimingMode
    artifact_root: Path


@dataclass(frozen=True, slots=True)
class ModelStreamConfig:
    config_path: Path
    project_root: Path
    transport: ModelStreamTransportConfig


def _resolve_path(value: str, *, project_root: Path) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (project_root / raw_path).resolve()


def _schema_path(config_path: Path, raw_config: Mapping[str, object]) -> Path:
    raw_schema = raw_config.get("$schema")
    if not isinstance(raw_schema, str) or not raw_schema:
        raise ModelStreamConfigError(
            f"模型 stream 测试配置缺少有效 $schema: {config_path}"
        )
    if "://" in raw_schema:
        raise ModelStreamConfigError(
            f"模型 stream 测试配置 schema 必须是本地路径: {config_path}"
        )
    return (config_path.parent / raw_schema).resolve()


def _validate_config(
    raw_config: dict[str, object],
    *,
    config_path: Path,
    schema_path: Path,
) -> None:
    if not schema_path.is_file():
        raise FileNotFoundError(
            f"模型 stream 测试配置 schema 不存在: config={config_path} schema={schema_path}"
        )
    schema = read_jsonc_object(schema_path)
    jsonschema.Draft202012Validator.check_schema(schema)
    try:
        jsonschema.validate(raw_config, schema)
    except jsonschema.ValidationError as error:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ModelStreamConfigError(
            f"模型 stream 测试配置验证失败: config={config_path} "
            f"schema={schema_path} location={location}: {error.message}"
        ) from error


def _object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelStreamConfigError(f"{label} 必须是对象")
    return value


def load_model_stream_config(
    config_path: Path | str,
    *,
    project_root: Path | str | None = None,
) -> ModelStreamConfig:
    resolved_project_root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path.cwd().resolve()
    )
    raw_path = Path(config_path).expanduser()
    resolved_config_path = (
        raw_path.resolve()
        if raw_path.is_absolute()
        else (resolved_project_root / raw_path).resolve()
    )
    raw_config = read_jsonc_object(resolved_config_path)
    schema_path = _schema_path(resolved_config_path, raw_config)
    _validate_config(
        raw_config,
        config_path=resolved_config_path,
        schema_path=schema_path,
    )

    model_stream = _object(raw_config["model_stream"], label="model_stream")
    transport = _object(model_stream["transport"], label="model_stream.transport")
    mode = cast(ModelStreamMode, transport["mode"])
    scenario_id = transport.get("scenario_id")
    if scenario_id is not None and not isinstance(scenario_id, str):
        raise ModelStreamConfigError("model_stream.transport.scenario_id 必须是字符串")

    fixture_root: Path | None = None
    if "fixture_root" in transport:
        raw_fixture_root = transport["fixture_root"]
        if not isinstance(raw_fixture_root, str):
            raise ModelStreamConfigError(
                "model_stream.transport.fixture_root 必须是字符串"
            )
        fixture_root = _resolve_path(raw_fixture_root, project_root=resolved_project_root)

    matching = _object(transport.get("matching", {}), label="transport.matching")
    matching_policy = cast(
        MatchingPolicy,
        matching.get("policy", "strict"),
    )
    replay = _object(transport.get("replay", {}), label="transport.replay")
    replay_policy = cast(
        ReplayPolicy,
        replay.get("policy", "request_reusable"),
    )
    timing = _object(transport.get("timing", {}), label="transport.timing")
    timing_mode = cast(TimingMode, timing.get("mode", "none"))
    recording = _object(
        transport.get("recording", {}),
        label="transport.recording",
    )
    raw_artifact_root = recording.get("artifact_root")
    artifact_root = (
        _resolve_path(raw_artifact_root, project_root=resolved_project_root)
        if isinstance(raw_artifact_root, str)
        else (resolved_project_root / DEFAULT_ARTIFACT_ROOT).resolve()
    )

    if mode != "off" and (scenario_id is None or fixture_root is None):
        raise ModelStreamConfigError(
            "model_stream.transport 在 record/replay 模式必须配置 scenario_id 和 fixture_root"
        )

    return ModelStreamConfig(
        config_path=resolved_config_path,
        project_root=resolved_project_root,
        transport=ModelStreamTransportConfig(
            mode=mode,
            scenario_id=scenario_id,
            fixture_root=fixture_root,
            matching_policy=matching_policy,
            replay_policy=replay_policy,
            timing_mode=timing_mode,
            artifact_root=artifact_root,
        ),
    )


def load_model_stream_config_from_environment(
    *,
    environment: Mapping[str, str] | None = None,
    project_root: Path | str | None = None,
) -> ModelStreamConfig | None:
    env = environment if environment is not None else environ
    raw_path = env.get(MODEL_STREAM_CONFIG_ENV)
    if raw_path is None:
        return None
    if not raw_path.strip():
        raise ModelStreamConfigError(
            f"{MODEL_STREAM_CONFIG_ENV} 不能是空字符串"
        )
    return load_model_stream_config(raw_path, project_root=project_root)
