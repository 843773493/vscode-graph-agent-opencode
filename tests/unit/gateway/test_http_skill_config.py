"""Gateway HTTP Skill 配置经最终合并、schema 和 typed 校验进入运行配置。"""

import json
from pathlib import Path

import pytest

from app.gateway.config import load_gateway_config


@pytest.fixture
def source() -> dict:
    return {"id": "shared-one", "name": "demo", "fetch": {"protocol": "http", "url": "https://source.example.test/SKILL.md"}}


def _load(path: Path, sources: list[dict]):
    path.write_text(json.dumps({"config_version": 3, "resources": {"skill_sources": {"sources": sources}}}))
    return load_gateway_config(config_path=path, schema_path=Path.cwd() / "configs/gateway_schema.jsonc")


def test_http_sources_keep_stable_id_and_default_to_hourly_poll(tmp_path: Path, source: dict) -> None:
    config = _load(tmp_path / "gateway.jsonc", [source])
    assert config.skill_sources[0].id == "shared-one"
    assert config.skill_sources[0].updates.poll_seconds == 3600
    assert config.skill_sources[0].priority == 0
    assert config.payload["config_version"] == 3


@pytest.mark.parametrize("patch", [
    {"updates": {"mode": "event"}},
    {"updates": {"mode": "poll"}},
    {"updates": {"mode": "manual", "poll_interval": "1h"}},
    {"fetch": {"protocol": "unknown", "url": "https://example.test/"}},
    {"fetch": {"protocol": "http", "url": "https://example.test/#private"}},
    {"fetch": {"protocol": "http", "url": "https://example.test/", "timeout_seconds": 0}},
])
def test_final_config_rejects_unsupported_sources(tmp_path: Path, source: dict, patch: dict) -> None:
    with pytest.raises(ValueError):
        _load(tmp_path / "gateway.jsonc", [{**source, **patch}])


def test_final_config_rejects_duplicate_ids(tmp_path: Path, source: dict) -> None:
    with pytest.raises(ValueError, match="id 重复"):
        _load(tmp_path / "gateway.jsonc", [source, source])
