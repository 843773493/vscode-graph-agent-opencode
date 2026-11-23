"""真实 pytest 入口核对参数现场、共享装具及异常退出保护。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def artifact_project(request: pytest.FixtureRequest) -> Path:
    project = TestRunContext.from_test_file(Path(request.node.path)).artifacts_dir / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests/conftest.py").write_text('''
from pathlib import Path
import pytest
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace

@pytest.fixture
def case(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(workspace_root=context.workspace_root, template_root=Path.cwd(), template_items=())
    prepare_test_workspace(workspace_root=context.output_root / "secondary", template_root=Path.cwd(), template_items=())
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True)
    (runtime / "temporary.txt").write_text("temporary")
    return context
''', encoding="utf-8")
    return project


@pytest.fixture
def run_artifact_project(artifact_project: Path):
    def run(source: str, selection: str | None = None, *, collect_only: bool = False) -> tuple[subprocess.CompletedProcess[str], Path, list[dict]]:
        (artifact_project / "tests/test_case.py").write_text(source, encoding="utf-8")
        command = [sys.executable, "-m", "pytest", "-q", "--import-mode=importlib",
                   "-p", "no:cacheprovider", "-p", "tests.harness.python.artifacts", "tests"]
        if selection is not None:
            command.extend(["-k", selection])
        if collect_only:
            command.append("--collect-only")
        run_id = uuid4().hex
        result = subprocess.run(command, cwd=artifact_project, capture_output=True, text=True,
                                timeout=30, check=False, env=os.environ | {
                                    "PYTHONPATH": str(Path.cwd()), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                    "BOXTEAM_TEST_RUN_ID": run_id,
                                    # 子项目路径不能登记到父仓库矩阵的相对性能索引。
                                    "BOXTEAM_TEST_PERF_DIR": "",
                                })
        evidence = artifact_project / f"pytest-{run_id}.json"
        evidence.write_text(json.dumps({"command": command, "exit_code": result.returncode,
                                        "stdout": result.stdout, "stderr": result.stderr}), encoding="utf-8")
        output = artifact_project / "out/tests/test_case"
        if collect_only:
            return result, output, []
        latest = json.loads((output / "latest.json").read_text())
        run_root = (output / latest["run"]).parent.parent.parent
        receipts = [json.loads(path.read_text()) for path in sorted(run_root.glob("nodes/*/run.json"))]
        return result, output, receipts
    return run


def test_parameters_keep_failed_wal_database_and_separate_evidence(run_artifact_project, artifact_project: Path) -> None:
    (artifact_project / "tests/test_other.py").write_text('def test_other(case):\n    assert case.workspace_root.is_dir()\n', encoding="utf-8")
    result, output, receipts = run_artifact_project('''
import subprocess, sys
import pytest

@pytest.mark.parametrize("value", ["failed", "passed"])
def test_case(case, value):
    print("parameter=" + value)
    (case.output_root / "secondary/value.txt").write_text(value)
    assert not (case.workspace_root / "state.sqlite").exists()
    subprocess.run([sys.executable, "-c", "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('pragma journal_mode=wal'); c.execute('create table facts(value)'); c.execute('insert into facts values (?)', (sys.argv[2],)); c.commit(); os._exit(0)", str(case.workspace_root / "state.sqlite"), value], check=True, timeout=5)
    assert value == "passed"
''')
    assert result.returncode == 1, result.stdout + result.stderr
    failed, = (receipt for receipt in receipts if receipt["status"] == "failed")
    passed, = (receipt for receipt in receipts if receipt["status"] == "passed")
    assert failed["node_id"].endswith("[failed]") and passed["node_id"].endswith("[passed]")
    saved = output / failed["state_paths"][str(output / "workspace")]
    assert failed["state_boundary"] == "node_end"
    secondary = output / failed["state_paths"][str(output / "secondary")]
    assert (secondary / "value.txt").read_text() == "failed"
    assert (output / "secondary/value.txt").read_text() == "passed"
    assert (saved / "state.sqlite-wal").is_file()
    for root, expected in ((saved, "failed"), (output / "workspace", "passed")):
        with sqlite3.connect(f"file:{root / 'state.sqlite'}?mode=ro", uri=True) as connection:
            assert connection.execute("select value from facts").fetchall() == [(expected,)]
    assert "parameter=failed" in (output / failed["artifacts"] / "pytest-call.log").read_text()
    assert "parameter=passed" in (output / passed["artifacts"] / "pytest-call.log").read_text()
    latest_failed = json.loads((output / "latest-failed.json").read_text())
    assert json.loads((output / latest_failed["run"]).read_text())["node_id"] == failed["node_id"]
    other = output.with_name("test_other")
    other_index = json.loads((other / "latest.json").read_text())
    other_receipt = json.loads((other / other_index["run"]).read_text())
    assert other_receipt["runtime"] == "removed"
    assert not (other / "runtime" / other_receipt["run_id"]).exists()
    assert (output / "runtime" / failed["run_id"]).exists()


@pytest.mark.parametrize("scope", ["module", "session", "session_teardown_failure"])
def test_shared_fixture_is_saved_only_after_its_finalizer(run_artifact_project, artifact_project: Path, scope: str) -> None:
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
@pytest.fixture(scope="SCOPE")
def shared(request):
    context = TestRunContext.from_test_file(Path("tests/test_case.py")).prepare()
    state = context.workspace_root / "state.txt"
    state.write_text("opened")
    yield state
    state.write_text(state.read_text() + ",closed")
    FINALIZE
'''.replace("SCOPE", scope.split("_")[0]).replace("FINALIZE", 'raise RuntimeError("共享装具关闭失败")' if scope.endswith("failure") else "pass"))
    if scope.startswith("session"):
        (artifact_project / "tests/test_zlast.py").write_text('def test_last(shared):\n    assert shared.read_text() == "opened,first,second"\n', encoding="utf-8")
    result, output, receipts = run_artifact_project('''
def test_first(shared):
    shared.write_text(shared.read_text() + ",first")
    assert False, "明确的失败"

def test_second(shared):
    assert shared.read_text() == "opened,first"
    shared.write_text(shared.read_text() + ",second")
''')
    assert result.returncode == 1, result.stdout + result.stderr
    failed, = (receipt for receipt in receipts if receipt["status"] == "failed")
    if scope.endswith("failure"):
        assert failed["state"] == "retained_in_place"
        assert (output / "workspace/state.txt").read_text() == "opened,first,second,closed"
        assert (output / "runtime/workspace-owner.json").is_file()
        return
    saved = output / failed["state_paths"][str(output / "workspace")]
    assert failed["state_boundary"] == "shared_fixture_end"
    assert (saved / "state.txt").read_text() == "opened,first,second,closed"
    assert not (output / "runtime/workspace-owner.json").exists()


def test_shared_fixture_parameter_change_preserves_previous_state(run_artifact_project, artifact_project: Path) -> None:
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
@pytest.fixture(scope="module", params=["failed", "passed"])
def parameter_state(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(workspace_root=context.workspace_root, template_root=Path.cwd(), template_items=())
    state = context.workspace_root / "state.txt"
    state.write_text(request.param)
    yield state
    state.write_text(state.read_text() + ",closed")
''')
    result, output, receipts = run_artifact_project('''
def test_parameter(parameter_state):
    assert parameter_state.read_text() == "passed"
''')
    assert result.returncode == 1, result.stdout + result.stderr
    failed, = (receipt for receipt in receipts if receipt["status"] == "failed")
    saved = output / failed["state_paths"][str(output / "workspace")]
    assert (saved / "state.txt").read_text() == "failed,closed"
    assert (output / "workspace/state.txt").read_text() == "passed,closed"
    assert failed["state_boundary"] == "shared_fixture_end"
    assert not (output / "runtime/workspace-owner.json").exists()


def test_protocol_cleanup_error_never_reports_passed(run_artifact_project, artifact_project: Path) -> None:
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    raise RuntimeError("协议收尾故障")
''')
    result, output, receipts = run_artifact_project('''
def test_passed_body(case):
    (case.workspace_root / "state.txt").write_text("original")
''')
    assert result.returncode == 3, result.stdout + result.stderr
    receipt, = receipts
    assert set(receipt["reports"].values()) == {"passed"}
    assert receipt["status"] == "incomplete"
    assert "协议收尾故障" in receipt["protocol_error"]
    assert (output / "runtime/workspace-owner.json").is_file()
    assert (output / "workspace/state.txt").read_text() == "original"


@pytest.mark.parametrize("mode", ["success", "preserve", "crash", "setup_failure", "teardown_failure"])
def test_runtime_cleanup_and_unfinished_state_protection(run_artifact_project, mode: str) -> None:
    source = '''
import os
import pytest
from pathlib import Path
from tests.harness.python.run_context import TestRunContext

@pytest.fixture(scope="session")
def shared_runtime():
    context = TestRunContext.from_test_file(Path("tests/test_case.py"))
    path = context.runtime_root_for_node("shared") / "live.txt"
    path.parent.mkdir(parents=True)
    path.write_text("shared")
    yield path
    assert path.read_text() == "shared"

@pytest.fixture
def owned(case):
    (case.workspace_root / "untouched.txt").write_text("original")
    SETUP
    yield case
    TEARDOWN

DECORATOR
def test_first(owned, shared_runtime):
    BODY

def test_second(shared_runtime):
    assert shared_runtime.read_text() == "shared"
'''.replace("TEARDOWN", 'raise RuntimeError("关闭失败")' if mode == "teardown_failure" else "pass")
    source = source.replace("SETUP", 'raise RuntimeError("准备失败")' if mode == "setup_failure" else "pass")
    source = source.replace("DECORATOR", "@pytest.mark.preserve_test_state" if mode == "preserve" else "")
    source = source.replace("BODY", "os._exit(23)" if mode == "crash" else 'assert shared_runtime.read_text() == "shared"')
    result, output, receipts = run_artifact_project(source)
    assert result.returncode == {"crash": 23, "setup_failure": 1, "teardown_failure": 1}.get(mode, 0), result.stdout + result.stderr
    runtime = output / "runtime" / receipts[0]["run_id"]
    assert runtime.exists() == (mode != "success")
    if mode in {"crash", "teardown_failure"}:
        marker = output / "runtime/workspace-owner.json"
        original_owner = marker.read_bytes()
        for _ in range(3):
            retry, _, _ = run_artifact_project('def test_retry(case):\n    assert False, "不应进入测试正文"\n')
            assert retry.returncode == 1 and "已保护原现场" in retry.stdout
        assert marker.read_bytes() == original_owner
        assert (output / "workspace/untouched.txt").read_text() == "original"
    elif mode in {"preserve", "setup_failure"}:
        saved, = (receipt for receipt in receipts if receipt["state"] == "saved")
        assert (output / saved["state_paths"][str(output / "workspace")] / "untouched.txt").read_text() == "original"
    else:
        assert all(receipt["runtime"] == "removed" for receipt in receipts)


def test_retention_groups_parameters_and_preserves_latest_failure_and_pins(run_artifact_project, artifact_project: Path) -> None:
    source = '''
import pytest
from tests.harness.performance.record import Perf

DECORATOR
@pytest.mark.parametrize("value", [1, 2])
def test_case(case, value):
    recorder = Perf(case)
    recorder.record("sample", "retention", value=value)
    recorder.close()
    (case.workspace_root / "value.txt").write_text(str(value))
    if not EXPECTED:
        original = case.workspace_root / "original"
        original.mkdir()
        (original / "attachment.txt").write_text("immutable attachment")
        original.chmod(0o500)
    assert EXPECTED
'''
    legacy = artifact_project / "out/tests/test_case/runs/legacy"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text('{"status": "passed"}')
    runs = []
    for index, mode in enumerate(["failed", "failed", "passed", "passed", "pinned", "passed", "passed"]):
        result, output, receipts = run_artifact_project(source.replace(
            "DECORATOR", "@pytest.mark.preserve_test_state" if mode == "pinned" else "",
        ).replace("EXPECTED", "False" if mode == "failed" else "True"))
        assert result.returncode == (1 if mode == "failed" else 0), result.stdout + result.stderr
        assert len(receipts) == 2
        run = output / "runs" / receipts[0]["run_id"]
        performance = (output / receipts[0]["performance"]).parent
        assert len(list(performance.glob("*/*.jsonl"))) == 2
        runs.append((run, performance))
        if index == 1:
            first = json.loads((runs[0][0] / "run.json").read_text())
            assert all(json.loads((output / path).read_text())["runtime"] == "removed" for path in first["nodes"])
            assert not (output / "runtime" / runs[0][0].name).exists()
        if index == 3:
            assert not runs[0][0].exists() and not runs[0][1].exists()
            assert all(path.is_dir() for path, _ in runs[1:])
    assert {path.name for path in (output / "runs").iterdir()} == {"legacy", *(runs[i][0].name for i in (1, 4, 5, 6))}
    for index in (1, 4):
        assert (output / "runtime" / runs[index][0].name).is_dir()
        assert list(runs[index][0].glob("nodes/*/state/workspace/value.txt"))
    for pointer, expected in (("latest.json", 6), ("latest-failed.json", 1)):
        target = output / json.loads((output / pointer).read_text())["run"]
        assert target.is_file() and target.is_relative_to(runs[expected][0])


def test_shared_evidence_outlives_each_consumer_and_has_no_stale_node_links(run_artifact_project, artifact_project: Path) -> None:
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
import sys
from types import SimpleNamespace
from tests.integration import conftest as integration_fixtures

prepared_mcp_stdio_package = integration_fixtures.prepared_mcp_stdio_package

@pytest.fixture(scope="session", autouse=True)
def package_builder():
    def build(command, **kwargs):
        Path(command[command.index("--out") + 1]).write_bytes(b"explicit package build substitute")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(integration_fixtures, "subprocess", SimpleNamespace(run=build))
        patch.setattr(integration_fixtures, "resolve_workspace_python_executable", lambda root: sys.executable)
        yield

@pytest.fixture(scope="session")
def shared_log(prepared_mcp_stdio_package):
    assert prepared_mcp_stdio_package.is_file()
    context = TestRunContext.from_test_file(Path("tests/conftest.py")).prepare()
    path = context.run_root / "artifacts/build.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("shared build evidence")
    return path
''')
    (artifact_project / "tests/test_other.py").write_text('def test_other(shared_log):\n    assert shared_log.is_file()\n')
    source = 'def test_case(shared_log):\n    assert shared_log.is_file()\n'
    result, output, _ = run_artifact_project(source)
    assert result.returncode == 0, result.stdout + result.stderr
    first = json.loads(next((output / "runs").glob("*/run.json")).read_text())
    session_path = artifact_project / first["session"]
    session = json.loads(session_path.read_text())
    package_runtime, = (artifact_project / item["path"] for item in session["runtime_resources"]
                       if "out/tests/integration/conftest/runs/" in item["path"])
    assert not package_runtime.exists()
    shared, = (artifact_project / "out/tests/conftest/runs").glob("*/artifacts/build.log")
    for _ in range(2):
        result, _, _ = run_artifact_project(source, selection="test_case")
        assert result.returncode == 0, result.stdout + result.stderr
    assert shared.read_text() == "shared build evidence"
    session = json.loads(session_path.read_text())
    assert len(session["nodes"]) == 1
    assert "test_other" in session["nodes"][0]
    assert all((artifact_project / node).is_file() for node in session["nodes"])
    for _ in range(2):
        result, _, _ = run_artifact_project(source)
        assert result.returncode == 0, result.stdout + result.stderr
    assert not shared.exists() and not session_path.exists()


def test_collection_has_no_run_and_all_skipped_is_not_passed(run_artifact_project, artifact_project: Path) -> None:
    source = 'import pytest\n@pytest.mark.skip(reason="缺少前置条件")\ndef test_case():\n    assert False\n'
    result, output, _ = run_artifact_project(source, collect_only=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not output.exists()
    assert not list((artifact_project / "out").rglob("pytest-session.json"))
    result, output, receipts = run_artifact_project(source)
    assert result.returncode == 0 and "1 skipped" in result.stdout, result.stdout + result.stderr
    assert receipts[0]["status"] == "skipped"
    run = output / "runs" / receipts[0]["run_id"] / "run.json"
    assert json.loads(run.read_text())["status"] == "skipped"


def test_direct_pytest_exports_identity_to_javascript(run_artifact_project, artifact_project: Path) -> None:
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
import os
def pytest_configure(config):
    for name in ("BOXTEAM_TEST_RUN_ID", "BOXTEAM_TEST_RUN_STARTED_AT", "PYTEST_XDIST_TESTRUNUID"):
        os.environ.pop(name, None)
''')
    module = (Path.cwd() / "tests/harness/js/run-context.mjs").as_uri()
    script = f'''import {{ TestRunContext }} from {json.dumps(module)};
const context = TestRunContext.fromTestFile(process.argv[1]);
console.log(JSON.stringify({{run: context.runRoot, artifacts: context.artifactsDir, performance: context.performanceDir, runtime: context.runtimeDir}}));'''
    result, _, _ = run_artifact_project('''
import json, os, subprocess
def test_case(case):
    assert os.environ["BOXTEAM_TEST_RUN_ID"]
    assert os.environ["BOXTEAM_TEST_RUN_STARTED_AT"]
    response = subprocess.run(["node", "--input-type=module", "--eval", SCRIPT, str(case.test_file)],
                              check=True, capture_output=True, text=True, timeout=5)
    node_id = os.environ["PYTEST_CURRENT_TEST"].rsplit(" (", 1)[0]
    assert json.loads(response.stdout) == {"run": str(case.run_root), "artifacts": str(case.artifacts_dir),
                                          "performance": str(case.performance_dir),
                                          "runtime": str(case.runtime_root_for_node(node_id))}
'''.replace("SCRIPT", repr(script)))
    assert result.returncode == 0, result.stdout + result.stderr


def test_cleanup_failure_protects_remaining_runtime(run_artifact_project, artifact_project: Path) -> None:
    conftest = artifact_project / "tests/conftest.py"
    original = conftest.read_text()
    conftest.write_text(original + '''
@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    from tests.harness.python.retention import RunRetention
    def fail(self, path):
        raise OSError("清理磁盘失败")
    RunRetention.remove = fail
''')
    source = 'def test_case(case):\n    assert case.workspace_root.is_dir()\n'
    result, output, receipts = run_artifact_project(source)
    assert result.returncode != 0 and "清理磁盘失败" in result.stderr
    runtime = output / "runtime" / receipts[0]["run_id"]
    run = output / "runs" / receipts[0]["run_id"]
    conftest.write_text(original)
    for _ in range(3):
        result, _, _ = run_artifact_project(source)
        assert result.returncode == 0, result.stdout + result.stderr
    assert runtime.is_dir() and (run / "run.json").is_file()
    summary = json.loads((run / "run.json").read_text())
    session = json.loads((artifact_project / summary["session"]).read_text())
    assert session["status"] == "incomplete"


def test_shared_model_stream_config_belongs_to_run_not_first_parameter(run_artifact_project, artifact_project: Path) -> None:
    config_path = Path.cwd() / "configs/tests/model_stream/model_stream.jsonc"
    for relative in (
        "tests/fixtures/model_stream/handwritten/openai_chat/basic-text.json",
        "configs/tests/model_stream/model_stream_web_basic_chat_tool_loop.jsonc",
        "configs/tests/model_stream/model_stream_schema.jsonc",
    ):
        target = artifact_project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path.cwd() / relative, target)
    with (artifact_project / "tests/conftest.py").open("a", encoding="utf-8") as stream:
        stream.write('''
from tests.e2e.conftest import e2e_model_stream_runtime_config_path

@pytest.fixture(scope="module")
def e2e_model_stream_config_path():
    return CONFIG_PATH

@pytest.fixture(scope="module")
def e2e_model_stream_mode():
    return "record"
'''.replace("CONFIG_PATH", repr(str(config_path))))
    source = '''
import json
from pathlib import Path
import pytest
from tests.harness.python.run_context import TestRunContext

@pytest.mark.parametrize("value", ["first", "second"])
def test_case(e2e_model_stream_runtime_config_path, value):
    context = TestRunContext.from_test_file(Path(__file__))
    config = json.loads(Path(e2e_model_stream_runtime_config_path).read_text())
    destination = Path(config["model_stream"]["transport"]["recording"]["artifact_root"])
    assert destination == context.run_root / "artifacts/model-stream"
'''
    replay_source = '''
from pathlib import Path
import pytest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_generation_strategies import generation_replay

@pytest.mark.parametrize("value", ["first", "second"])
def test_case(generation_replay, value):
    context = TestRunContext.from_test_file(Path(__file__))
    assert generation_replay.artifacts == context.run_root / "artifacts/model-stream"
'''
    for scenario in (source, source, replay_source):
        result, _, receipts = run_artifact_project(scenario)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(receipts) == 2
