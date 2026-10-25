from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def e2e_model_stream_config_path(request: pytest.FixtureRequest) -> str:
    config_name = getattr(request.module, "MODEL_STREAM_CONFIG_NAME", None)
    if not isinstance(config_name, str) or not config_name:
        raise RuntimeError(
            "model stream E2E 模块必须声明非空 MODEL_STREAM_CONFIG_NAME: "
            f"{request.module.__name__}"
        )
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / config_name
    )
