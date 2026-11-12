from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.testing.model_stream import load_model_stream_config
from tests.support.model_stream.config import prepare_e2e_model_stream_config

CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream.jsonc"


@pytest.mark.parametrize("mode", ["off", "record", "replay"])
def test_prepare_e2e_model_stream_config_overrides_mode_and_artifact_root(
    tmp_path: Path,
    mode: str,
) -> None:
    output_root = tmp_path / "test_model_stream_replay"
    generated = prepare_e2e_model_stream_config(
        CONFIG_PATH,
        runtime_root=output_root / "runtime",
        artifacts_dir=output_root / "artifacts",
        mode=mode,
    )

    config = load_model_stream_config(generated)

    assert config.transport.mode == mode
    assert config.transport.scenario_id == "reasoning-tool"
    assert config.transport.artifact_root == (
        output_root / "artifacts" / "model-stream"
    ).resolve()
    assert generated.parent / "model_stream_schema.jsonc" == generated.parent / (
        json.loads(generated.read_text(encoding="utf-8"))["$schema"]
    )


def test_prepare_e2e_model_stream_config_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode 不受支持"):
        prepare_e2e_model_stream_config(
            CONFIG_PATH,
            runtime_root=tmp_path / "runtime",
            artifacts_dir=tmp_path / "artifacts",
            mode="unexpected",
        )


def test_model_stream_config_and_recordings_remain_owned_by_their_node(tmp_path: Path) -> None:
    generated = []
    for name in ("first", "second"):
        runtime = tmp_path / "runtime" / name
        artifacts = tmp_path / "artifacts" / name
        generated.append(prepare_e2e_model_stream_config(
            CONFIG_PATH, runtime_root=runtime, artifacts_dir=artifacts,
            exact_fixture_root=runtime / "fixture",
        ))
    first = load_model_stream_config(generated[0])
    second = load_model_stream_config(generated[1])

    assert generated[0] != generated[1]
    assert first.transport.fixture_root != second.transport.fixture_root
    assert first.transport.artifact_root == (tmp_path / "artifacts/first/model-stream").resolve()
    assert second.transport.artifact_root == (tmp_path / "artifacts/second/model-stream").resolve()
