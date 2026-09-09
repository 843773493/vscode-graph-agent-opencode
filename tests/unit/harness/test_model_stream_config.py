from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.testing.model_stream import load_model_stream_config
from tests.support.model_stream_config import prepare_e2e_model_stream_config

CONFIG_PATH = Path.cwd() / "configs" / "tests" / "model_stream" / "model_stream.jsonc"


@pytest.mark.parametrize("mode", ["off", "record", "replay"])
def test_prepare_e2e_model_stream_config_overrides_mode_and_artifact_root(
    tmp_path: Path,
    mode: str,
) -> None:
    output_root = tmp_path / "test_model_stream_replay"
    generated = prepare_e2e_model_stream_config(
        CONFIG_PATH,
        output_root=output_root,
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
            output_root=tmp_path,
            mode="unexpected",
        )
