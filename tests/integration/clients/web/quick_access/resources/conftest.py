"""资源 Quick Access 页面共用正式 Workbench 静态资产。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def resource_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Path:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))
    return assets
