from __future__ import annotations

import hashlib
import json
import os
import runpy
import signal
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin" or os.name == "nt"),
    reason=f"run_task 不支持当前进程平台: {sys.platform}",
)

PROJECT_ROOT = Path.cwd().resolve()
SCRIPTS = PROJECT_ROOT / ".codex/skills/team-collaboration-gpt/scripts"
RUNNER_PATH = SCRIPTS / "run_task.py"


@dataclass(slots=True)
class RunTaskFixture:
    profile_path: Path
    workdir: Path
    artifact_root: Path
    profile: dict[str, object]


def _create_run_task_fixture(
    tmp_path: Path,
    *,
    source_paths: list[str],
    extra: dict[str, object] | None = None,
) -> RunTaskFixture:
    project = tmp_path
    workdir = project / "w"
    artifact_root = project / "out/tests/temp/a"
    workdir.mkdir(parents=True)
    artifact_root.mkdir(parents=True)
    boxteam_home = project / "out/boxteam-home"
    boxteam_home.mkdir(parents=True)
    temporary_root = project / "out/tmp"
    temporary_root.mkdir(parents=True)
    profile: dict[str, object] = {
        "workdir": str(workdir),
        "project_root": str(project),
        "artifacts": str(artifact_root),
        "max_seconds": 20,
        "source_paths": source_paths,
        "environment": {
            "BOXTEAM_HOME": str(boxteam_home),
            "TMPDIR": str(temporary_root),
            "TMP": str(temporary_root),
            "TEMP": str(temporary_root),
        },
        "allowed_options": [],
    }
    if extra:
        profile.update(extra)
    profile_path = artifact_root / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return RunTaskFixture(profile_path, workdir, artifact_root, profile)


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    runner: dict[str, object],
    profile_path: Path,
    attempt: str,
    command: list[str],
    *,
    seconds: int = 10,
) -> int:
    monkeypatch.setattr(sys, "argv", [
        str(RUNNER_PATH), "--profile", str(profile_path), "--attempt", attempt,
        "--seconds", str(seconds), "--", *command,
    ])
    return runner["main"]()


def _runner() -> dict[str, object]:
    return runpy.run_path(str(RUNNER_PATH), run_name="run_task_source_snapshot_test")


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    original = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    try:
        yield
    finally:
        for signum, handler in original.items():
            signal.signal(signum, handler)


def test_run_task_records_profile_and_baseline_bytes_used_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    source = b"source before run\n"
    baseline_payload = {
        "candidate_tree": "candidate-a",
        "files": {"source.py": {
            "sha256": hashlib.sha256(source).hexdigest(), "bytes": len(source),
        }},
    }
    fixture = _create_run_task_fixture(
        tmp_path, source_paths=["source.py"],
    )
    profile_path = fixture.profile_path
    workdir = fixture.workdir
    artifact_root = fixture.artifact_root
    profile = fixture.profile
    (workdir / "source.py").write_bytes(source)
    baseline_path = artifact_root / "source-baseline.json"
    profile["source_baseline"] = str(baseline_path)
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline_payload), encoding="utf-8")
    profile_bytes = profile_path.read_bytes()
    baseline_bytes = baseline_path.read_bytes()
    runner = _runner()
    original_fingerprints = runner["source_fingerprints"]
    calls = 0

    def change_paths_after_snapshot(workdir_arg: Path, paths: list[str]) -> dict[str, object]:
        nonlocal calls
        fingerprints = original_fingerprints(workdir_arg, paths)
        if calls == 0:
            changed_profile = {**profile, "environment": {"changed": "after-read"}}
            profile_path.write_text(json.dumps(changed_profile), encoding="utf-8")
            changed_baseline = {**baseline_payload, "candidate_tree": "changed-after-read"}
            baseline_path.write_text(json.dumps(changed_baseline), encoding="utf-8")
        calls += 1
        return fingerprints

    monkeypatch.setitem(runner["main"].__globals__, "source_fingerprints", change_paths_after_snapshot)
    assert _invoke(
        monkeypatch,
        runner,
        profile_path,
        "success",
        [sys.executable, "-c", "import sys; print('child-output'); print('child-error', file=sys.stderr)"],
    ) == 0

    result_dir = artifact_root / "runs/success"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    baseline_check = json.loads(
        (result_dir / "source-baseline-check.json").read_text(encoding="utf-8")
    )
    assert result["profile_sha256"] == hashlib.sha256(profile_bytes).hexdigest()
    assert baseline_check["baseline_sha256"] == hashlib.sha256(baseline_bytes).hexdigest()
    assert "child-output" in (result_dir / "stdout.raw").read_text(encoding="utf-8")
    assert "child-error" in (result_dir / "stderr.raw").read_text(encoding="utf-8")

    monkeypatch.syspath_prepend(str(SCRIPTS))
    verify_run = runpy.run_path(str(SCRIPTS / "verify_run.py"), run_name="verify_run_snapshot_test")
    with pytest.raises(ValueError, match="profile_matches"):
        verify_run["verify_run"](result_dir, RUNNER_PATH)


def test_run_task_uses_parsed_profile_when_pytest_selection_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    selector = "tests/unit/test_allowed.py"
    allowed_node = f"{selector}::test_allowed"
    fixture = _create_run_task_fixture(
        tmp_path, source_paths=[], extra={"pytest_node_ids": {selector: [allowed_node]}},
    )
    profile_path = fixture.profile_path
    artifact_root = fixture.artifact_root
    test_file = fixture.workdir / selector
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_allowed(): pass\n", encoding="utf-8")
    profile_bytes = profile_path.read_bytes()
    runner = _runner()
    original_fingerprints = runner["source_fingerprints"]
    changed = False

    def change_profile_after_snapshot(workdir_arg: Path, paths: list[str]) -> dict[str, object]:
        nonlocal changed
        fingerprints = original_fingerprints(workdir_arg, paths)
        if not changed:
            changed_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            changed_profile["pytest_node_ids"][selector].append(
                f"{selector}::test_unlisted"
            )
            profile_path.write_text(json.dumps(changed_profile), encoding="utf-8")
            changed = True
        return fingerprints

    monkeypatch.setitem(runner["main"].__globals__, "source_fingerprints", change_profile_after_snapshot)
    exit_code = _invoke(
        monkeypatch, runner, profile_path, "selection-blocked",
        ["uv", "run", "pytest", f"{selector}::test_unlisted"],
    )

    result_dir = artifact_root / "runs/selection-blocked"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    selection = json.loads(
        (result_dir / "command-selection-check.json").read_text(encoding="utf-8")
    )
    assert exit_code == 2
    assert result["profile_sha256"] == hashlib.sha256(profile_bytes).hexdigest()
    assert result["child_started"] is False
    assert selection["launch_blocked"] is True
    assert not (result_dir / "command-exit.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 专用进程组探针")
def test_process_group_members_uses_portable_process_listing_without_procfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    observed: dict[str, object] = {}

    def fake_process_listing(command: list[str], **options: object) -> SimpleNamespace:
        observed["command"] = command
        observed["options"] = options
        return SimpleNamespace(stdout="123 456 S\n789 456 Z\n321 654 S\n")

    monkeypatch.setattr(runner["sys"], "platform", "darwin")
    monkeypatch.setattr(runner["subprocess"], "run", fake_process_listing)

    assert runner["process_group_members"](456) == [123]
    assert observed["command"] == ["ps", "-A", "-o", "pid=,pgid=,stat="]
    assert observed["options"] == {
        "check": True,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX 专用进程组扫描")
def test_run_task_reuses_cleanup_scan_as_final_process_group_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    fixture = _create_run_task_fixture(tmp_path, source_paths=[])
    runner = _runner()
    original_process_group_members = runner["process_group_members"]
    scan_count = 0

    def count_process_group_scans(pgid: int) -> list[int]:
        nonlocal scan_count
        scan_count += 1
        return original_process_group_members(pgid)

    monkeypatch.setitem(
        runner["main"].__globals__, "process_group_members", count_process_group_scans,
    )
    assert _invoke(
        monkeypatch, runner, fixture.profile_path, "single-final-scan", [sys.executable, "-c", "pass"],
    ) == 0

    result_dir = fixture.artifact_root / "runs/single-final-scan"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    assert scan_count == 1
    assert result["remaining_process_group_members"] == []
    assert "final_group_check" not in result["phase_seconds"]


def test_run_task_reaps_children_when_command_root_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    fixture = _create_run_task_fixture(
        tmp_path, source_paths=[],
    )
    profile_path = fixture.profile_path
    artifact_root = fixture.artifact_root
    runner = _runner()
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
    ]

    assert _invoke(monkeypatch, runner, profile_path, "reap-descendant", command) == 0

    result_dir = artifact_root / "runs/reap-descendant"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    if os.name == "posix":
        assert result["cleanup_killed_process_ids"]
        assert result["remaining_process_group_members"] == []
    else:
        assert result["process_tree_cleanup_verified"] is True
        assert result["process_tree_receipt"]["bytes"] > 0

    monkeypatch.syspath_prepend(str(SCRIPTS))
    verify_run = runpy.run_path(str(SCRIPTS / "verify_run.py"), run_name="verify_run_reaped_descendant_test")
    receipt = verify_run["verify_run"](result_dir, RUNNER_PATH)
    assert all(receipt["checks"].values())


def test_run_task_enforces_timeout_and_reaps_command_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    fixture = _create_run_task_fixture(
        tmp_path, source_paths=[],
    )
    profile_path = fixture.profile_path
    artifact_root = fixture.artifact_root
    runner = _runner()
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)",
    ]

    assert _invoke(
        monkeypatch, runner, profile_path, "timeout-process-tree", command, seconds=1,
    ) == 124

    result_dir = artifact_root / "runs/timeout-process-tree"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    assert result["exit_code"] == 124
    command_exit = json.loads((result_dir / "command-exit.json").read_text(encoding="utf-8"))
    assert command_exit["exit_code"] == 124
    if os.name == "posix":
        assert result["remaining_process_group_members"] == []
    else:
        assert result["process_tree_cleanup_verified"] is True

    monkeypatch.syspath_prepend(str(SCRIPTS))
    verify_run = runpy.run_path(str(SCRIPTS / "verify_run.py"), run_name="verify_run_timeout_test")
    receipt = verify_run["verify_run"](result_dir, RUNNER_PATH)
    assert all(receipt["checks"].values())


def test_run_task_records_baseline_bytes_before_prelaunch_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    source = b"source before run\n"
    baseline_payload = {
        "candidate_tree": "candidate-a",
        "files": {"source.py": {
            "sha256": hashlib.sha256(source).hexdigest(), "bytes": len(source),
        }},
    }
    fixture = _create_run_task_fixture(
        tmp_path, source_paths=["source.py"],
    )
    profile_path = fixture.profile_path
    workdir = fixture.workdir
    artifact_root = fixture.artifact_root
    profile = fixture.profile
    source_path = workdir / "source.py"
    source_path.write_bytes(source)
    baseline_path = artifact_root / "source-baseline.json"
    profile["source_baseline"] = str(baseline_path)
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline_payload), encoding="utf-8")
    baseline_bytes = baseline_path.read_bytes()
    runner = _runner()
    original_fingerprints = runner["source_fingerprints"]

    def change_source_before_snapshot(workdir_arg: Path, paths: list[str]) -> dict[str, object]:
        source_path.write_bytes(b"source changed before launch\n")
        changed_baseline = {**baseline_payload, "candidate_tree": "changed-after-parse"}
        baseline_path.write_text(json.dumps(changed_baseline), encoding="utf-8")
        return original_fingerprints(workdir_arg, paths)

    monkeypatch.setitem(runner["main"].__globals__, "source_fingerprints", change_source_before_snapshot)
    with pytest.raises(ValueError, match="源码与固定候选不一致"):
        _invoke(monkeypatch, runner, profile_path, "baseline-blocked", [sys.executable, "-c", "pass"])

    result_dir = artifact_root / "runs/baseline-blocked"
    check = json.loads(
        (result_dir / "source-baseline-check.json").read_text(encoding="utf-8")
    )
    assert check["baseline_sha256"] == hashlib.sha256(baseline_bytes).hexdigest()
    assert check["candidate_tree"] == "candidate-a"
    assert check["mismatched_paths"] == ["source.py"]
    assert not (result_dir / "started.json").exists()
