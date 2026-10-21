from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

import app.services.infrastructure.config_service.config_source_layers as source_layers
from app.services.infrastructure.config_service import ConfigService
from configs.runtime import read_jsonc_object


@pytest.fixture
def validation_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inline = tmp_path / "workspace_inline.jsonc"
    inline.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    schema = tmp_path / "workspace_schema.jsonc"
    schema.write_bytes(Path("configs/workspace_schema.jsonc").read_bytes())
    monkeypatch.setattr(source_layers, "get_user_workspace_schema_path", lambda: tmp_path / "absent.jsonc")
    return inline, schema, tmp_path


def test_missing_schema_reports_resolution_failure_without_native_paths(validation_files) -> None:
    inline, schema, directory = validation_files
    schema.unlink()
    service = ConfigService(config_dir=directory, inline_config_path=inline,
        config_path=directory / "workspace.jsonc")
    with pytest.raises(FileNotFoundError, match="Workspace 配置 schema 不存在") as failure:
        service.get_snapshot()
    assert str(directory) not in str(failure.value)
    assert isinstance(failure.value.__cause__, FileNotFoundError)
    assert str(schema) in str(failure.value.__cause__)


def test_workspace_startup_rejects_invalid_schema_without_echoing_source(validation_files) -> None:
    inline, schema_path, directory = validation_files
    schema = read_jsonc_object(schema_path)
    marker = str(directory / "secret-marker")
    schema["type"] = marker
    schema_path.write_text(json.dumps(schema))
    service = ConfigService(config_dir=directory, inline_config_path=inline,
        config_path=directory / "workspace.jsonc")
    with pytest.raises(ValueError, match="stage=schema") as failure:
        service.validate_workspace_config()
    assert "secret-marker" not in str(failure.value)
    assert str(directory) not in str(failure.value)
    assert isinstance(failure.value.__cause__, jsonschema.SchemaError)
    assert failure.value.__cause__.instance == marker


def test_shadow_schema_validation_rejects_without_echoing_config(validation_files) -> None:
    inline, _schema_path, directory = validation_files
    service = ConfigService(config_dir=directory, inline_config_path=inline,
        config_path=directory / "workspace.jsonc")
    service.validate_workspace_config()
    snapshot = service.get_snapshot()
    config = snapshot.to_dict()
    config["logger"]["level"] = str(directory / "secret-marker")
    with pytest.raises(ValueError, match="stage=config") as failure:
        service._validate_shadow_config(config)
    assert "secret-marker" not in str(failure.value)
    assert str(directory) not in str(failure.value)
    assert isinstance(failure.value.__cause__, jsonschema.ValidationError)
    assert failure.value.__cause__.validator == "enum"
    assert service.get_snapshot() is snapshot
