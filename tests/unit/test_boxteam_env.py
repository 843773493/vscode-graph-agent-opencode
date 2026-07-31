from __future__ import annotations

import os
from pathlib import Path

from app.core.env import load_boxteam_env


def test_load_boxteam_env_reads_only_boxteam_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    env_path = config_root / ".env"
    env_path.write_text("BOXTEAM_ENV_TEST=installed\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BOXTEAM_ENV_TEST=source\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.delenv("BOXTEAM_ENV_TEST", raising=False)
    monkeypatch.chdir(tmp_path)

    loaded_path = load_boxteam_env()

    assert loaded_path == env_path
    assert os.environ["BOXTEAM_ENV_TEST"] == "installed"
