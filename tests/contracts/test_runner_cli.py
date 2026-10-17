import json
import os
import subprocess
from pathlib import Path

import commentjson
import pytest

RUNNER = Path.cwd() / "tests" / "runner" / "run-tests.mjs"
VALID_SUITE_ID = "unit-synthetic"
VALID_SUITE_COMMAND = [
    "bun",
    "-e",
    "await Bun.write(process.env.CLI_SENTINEL, 'spawned')",
]
INVALID_ARGUMENTS = [
    pytest.param([], "--suite=<id>", id="missing-selection"),
    pytest.param(["--unknown"], "未知参数", id="unknown-option"),
    pytest.param(["--suite=not-configured"], "不存在 suite", id="unknown-suite-id"),
    pytest.param(["--suite"], "--suite 缺少值", id="suite-without-value"),
    pytest.param(["--suite", VALID_SUITE_ID], "--suite 缺少值", id="suite-separated-value"),
    pytest.param(["--suite="], "值不能为空", id="empty-suite"),
    pytest.param(["--suite=   "], "值不能为空", id="blank-suite"),
    pytest.param(
        ["--suite=" + VALID_SUITE_ID, "--suite=" + VALID_SUITE_ID],
        "不能重复",
        id="duplicate-suite",
    ),
    pytest.param(["--list", "--list"], "--list 不能重复", id="duplicate-list"),
    pytest.param(
        ["--list", "--suite=" + VALID_SUITE_ID],
        "不能同时使用",
        id="list-and-suite",
    ),
    pytest.param(["--all"], "不支持 --all", id="all-option"),
    pytest.param(["--suite=all"], "不支持 --suite=all", id="all-suite"),
    pytest.param([VALID_SUITE_ID], "未知参数", id="positional-suite"),
]


def run_runner(project: Path, arguments: list[str], sentinel: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "BOXTEAM_PROJECT_ROOT": str(project),
        "CLI_SENTINEL": str(sentinel),
    }
    return subprocess.run(
        ["bun", str(RUNNER), *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.fixture
def synthetic_project(tmp_path: Path) -> tuple[Path, Path]:
    runner_dir = tmp_path / "tests" / "runner"
    runner_dir.mkdir(parents=True)
    matrix = {
        "schemaVersion": 1,
        "suites": [
            {
                "id": VALID_SUITE_ID,
                "layer": "unit",
                "client": None,
                "enabled": True,
                "dataLimitKb": 4194304,
                "command": VALID_SUITE_COMMAND,
            }
        ],
    }
    (runner_dir / "matrix.jsonc").write_text(json.dumps(matrix), encoding="utf-8")
    return tmp_path, tmp_path / "spawned.marker"


@pytest.mark.parametrize(("arguments", "message"), INVALID_ARGUMENTS)
def test_invalid_cli_arguments_fail_before_starting_suite(
    synthetic_project: tuple[Path, Path], arguments: list[str], message: str,
) -> None:
    project, sentinel = synthetic_project
    protected_output = project / "protected-output.txt"
    protected_output.write_text("preserve", encoding="utf-8")

    result = run_runner(project, arguments, sentinel)

    assert result.returncode != 0
    assert message in result.stderr
    assert not sentinel.exists(), "非法参数启动了矩阵中的 suite 命令"
    assert protected_output.read_text(encoding="utf-8") == "preserve"


def test_explicit_suite_runs_its_matrix_command(
    synthetic_project: tuple[Path, Path],
) -> None:
    project, sentinel = synthetic_project

    result = run_runner(project, [f"--suite={VALID_SUITE_ID}"], sentinel)

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "spawned"


def test_list_prints_matrix_without_starting_suite(
    synthetic_project: tuple[Path, Path],
) -> None:
    project, sentinel = synthetic_project

    result = run_runner(project, ["--list"], sentinel)

    assert result.returncode == 0, result.stderr
    assert f"{VALID_SUITE_ID}\tunit\tsystem\tenabled" in result.stdout
    assert not sentinel.exists()


def test_repository_matrix_lists_full_and_focused_web_suites(tmp_path: Path) -> None:
    sentinel = tmp_path / "suite-started.marker"

    result = run_runner(Path.cwd(), ["--list"], sentinel)

    assert result.returncode == 0, result.stderr
    assert "e2e-web\te2e\tweb\tenabled" in result.stdout
    assert "e2e-web-basic-chat-tool-loop\te2e\tweb\tenabled" in result.stdout
    assert not sentinel.exists()


def test_web_matrix_suites_keep_full_and_focused_pytest_scopes() -> None:
    matrix_path = Path.cwd() / "tests" / "runner" / "matrix.jsonc"
    matrix = commentjson.loads(matrix_path.read_text(encoding="utf-8"))
    suites = matrix["suites"]
    full_suite = [suite for suite in suites if suite["id"] == "e2e-web"]
    focused_suite = [suite for suite in suites if suite["id"] == "e2e-web-basic-chat-tool-loop"]

    assert len(full_suite) == 1
    assert full_suite[0]["command"] == ["uv", "run", "pytest", "tests/e2e/clients/web"]
    assert len(focused_suite) == 1
    assert focused_suite[0]["command"] == [
        "uv",
        "run",
        "pytest",
        "tests/e2e/clients/web/test_basic_chat_tool_loop.py::test_basic_chat_tool_loop_through_real_web_surface",
    ]
