"""SQL 资源定位不依赖进程目录，缺失资产不使用默认文本。"""

from pathlib import Path

import pytest

from app.core.sqlite_state import load_sql


def test_sql_resource_is_resolved_from_package_after_chdir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "schema_migrations" in load_sql("app.core", "sqlite_state.sql")


def test_missing_sql_resource_fails_explicitly():
    with pytest.raises(FileNotFoundError):
        load_sql("app.core", "missing-schema.sql")
