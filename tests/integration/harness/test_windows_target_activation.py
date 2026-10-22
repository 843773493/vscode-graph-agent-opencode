"""原生 Git 激活失败必须中止，不能把旧 HEAD 当作激活成功。"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="必须由原生 PowerShell 执行")


@pytest.fixture
def activation_repository(request):
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    repository = context.runtime_root_for_node(request.node.nodeid) / "checkout's repository"
    repository.mkdir(parents=True)
    environment = dict(os.environ)
    environment.pop("GIT_INDEX_FILE", None)

    def git(*arguments):
        return subprocess.check_output(
            ["git", "-C", str(repository), *arguments], env=environment,
            stderr=subprocess.PIPE, timeout=10, text=True,
        ).strip()

    git("init")
    (repository / "marker.txt").write_text("base", encoding="utf-8")
    git("add", "marker.txt")
    git("-c", "user.name=Target Probe", "-c", "user.email=probe@localhost", "commit", "-m", "base")
    previous = git("rev-parse", "HEAD")
    (repository / "marker.txt").write_text("snapshot", encoding="utf-8")
    git("add", "marker.txt")
    git("-c", "user.name=Target Probe", "-c", "user.email=probe@localhost", "commit", "-m", "snapshot")
    snapshot = git("rev-parse", "HEAD")
    reference = "refs/boxteam/snapshots/probe"
    git("update-ref", reference, snapshot)
    git("reset", "--hard", previous)
    return context, repository, environment, reference, snapshot, previous


@pytest.mark.parametrize("locked", [False, True], ids=["success", "checkout-failure"])
def test_activation_reports_real_checkout_result(activation_repository, locked):
    context, repository, environment, reference, snapshot, previous = activation_repository
    lock = repository / ".git/index.lock"
    if locked:
        lock.write_text("probe lock", encoding="utf-8")
    source = Path.cwd() / "tools/cross-platform-development-targets/windows/Manage-Target.ps1"
    source_literal = source.as_posix().replace("'", "''")
    arguments = base64.b64encode(json.dumps(
        ["activate", repository.as_posix(), reference],
    ).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput(
    [IO.File]::ReadAllText('{source_literal}',[Text.Encoding]::UTF8),[ref]$tokens,[ref]$errors)
if ($errors.Count) {{ throw 'management script parse failed' }}
$functions=$ast.FindAll({{param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst]
}},$false)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
$arguments=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{arguments}')) | ConvertFrom-Json
Invoke-TargetAction @($arguments)
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            env=environment, capture_output=True, timeout=20, check=False,
        )
    finally:
        if locked:
            lock.unlink()
    name = "locked" if locked else "success"
    (context.artifacts_dir / f"{name}.stdout.raw").write_bytes(result.stdout)
    (context.artifacts_dir / f"{name}.stderr.raw").write_bytes(result.stderr)
    actual = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        env=environment, text=True, timeout=10,
    ).strip()
    if locked:
        assert result.returncode != 0
        assert b"git checkout" in result.stderr
        assert b"exit_code=128" in result.stderr
        assert previous.encode("ascii") not in result.stdout
        assert actual == previous
    else:
        assert result.returncode == 0, result.stderr
        assert snapshot.encode("ascii") in result.stdout
        assert actual == snapshot
