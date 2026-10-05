import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from tests.support.workspaces import prepare_default_test_workspace


def _run_workspace_identity_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.core.workspace_identity",
            *arguments,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.fixture
def cli_workspace(request: pytest.FixtureRequest) -> Path:
    workspace_root = (
        Path.cwd()
        / "out/tests/unit/core/test_workspace_identity/workspace"
        / request.node.name
    )
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    return prepare_default_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
    )


def test_workspace_identity_is_a_persistent_uuid4(tmp_path):
    first_id = load_or_create_workspace_id(tmp_path)
    second_id = load_or_create_workspace_id(tmp_path)

    assert first_id == second_id
    assert UUID(first_id).version == 4
    assert json.loads(
        workspace_identity_path(tmp_path).read_text(encoding="utf-8")
    ) == {"workspace_id": first_id}


def test_identity_cli_prints_and_reuses_one_workspace_uuid(cli_workspace: Path) -> None:
    arguments = ("--workspace-root", str(cli_workspace))
    first = _run_workspace_identity_cli(*arguments)
    second = _run_workspace_identity_cli(*arguments)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stderr == ""
    assert second.stderr == ""
    assert first.stdout.endswith("\n")
    assert first.stdout.count("\n") == 1
    workspace_id = first.stdout[:-1]
    assert str(UUID(workspace_id)) == workspace_id
    assert second.stdout == first.stdout
    assert json.loads(
        workspace_identity_path(cli_workspace).read_text(encoding="utf-8")
    ) == {"workspace_id": workspace_id}


def test_identity_cli_rejects_relative_workspace_root() -> None:
    result = _run_workspace_identity_cli(
        "--workspace-root",
        "relative/workspace",
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "必须是绝对路径" in result.stderr


def test_identity_cli_rejects_non_uuid_workspace_identity(
    cli_workspace: Path,
) -> None:
    identity_path = workspace_identity_path(cli_workspace)
    identity_path.write_text('{"workspace_id":"gw_invalid"}\n', encoding="utf-8")

    result = _run_workspace_identity_cli(
        "--workspace-root",
        str(cli_workspace),
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "必须是标准 UUID" in result.stderr
    assert str(identity_path) in result.stderr
