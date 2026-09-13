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


def test_load_boxteam_env_merges_proxy_exclusions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    boxteam_home = tmp_path / "boxteam-home"
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    env_path = config_root / ".env"
    env_path.write_text(
        "NO_PROXY=localhost,100.64.0.129,10.31.5.12\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

    load_boxteam_env()

    assert set(os.environ["NO_PROXY"].split(",")) == {
        "localhost",
        "127.0.0.1",
        "100.64.0.129",
        "10.31.5.12",
    }
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]
