from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _find_node() -> str:
    for candidate in ("node", "node.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    pytest.skip("node is not available in PATH")


@pytest.mark.unit
def test_node_pty_smoke_runs_a_real_shell_command(tmp_path: Path) -> None:
    node = _find_node()
    script = Path(__file__).with_name("node_pty_smoke.cjs")

    completed = subprocess.run(
        [node, str(script), str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["cwd"] == str(tmp_path.resolve())
    cleaned_output = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", payload["output"]).replace("\r", "")
    assert "node-pty-ok" in cleaned_output

    if subprocess.os.name == "nt":
        assert payload["shell"].lower().endswith("cmd.exe") or payload["shell"].lower().endswith("powershell.exe")
    else:
        assert payload["shell"].endswith("sh")