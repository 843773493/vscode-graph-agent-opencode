from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.workspaces import prepare_test_workspace


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows junction")
def test_workspace_cleanup_does_not_modify_junction_target(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    original = external / "original"
    original.write_bytes(b"protected original")
    original.chmod(0o400)
    before = original.stat().st_mode
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    junction = workspace / "outside"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(external)],
        check=True, capture_output=True, timeout=10,
    )
    template = tmp_path / "template"
    template.mkdir()

    prepare_test_workspace(workspace_root=workspace, template_root=template, template_items=())

    assert not list(workspace.iterdir())
    assert original.read_bytes() == b"protected original"
    assert original.stat().st_mode == before
