from __future__ import annotations

import hashlib
import json
import runpy
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="run_task 依赖 Linux /proc、os.killpg、GNU timeout 和 bash",
)

PROJECT_ROOT = Path.cwd().resolve()
SCRIPTS = PROJECT_ROOT / ".codex/skills/team-collaboration-gpt/scripts"
RUNNER_PATH = SCRIPTS / "run_task.py"


def _write_profile(
    tmp_path: Path,
    *,
    source_paths: list[str],
    extra: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, dict[str, object]]:
    project = tmp_path / "project"
    workdir = project / "workspace"
    artifact_root = project / "out/tests/temp/run-task-source-snapshot/artifacts"
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
    return profile_path, workdir, artifact_root, profile


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    runner: dict[str, object],
    profile_path: Path,
    attempt: str,
    command: list[str],
) -> int:
    monkeypatch.setattr(sys, "argv", [
        str(RUNNER_PATH), "--profile", str(profile_path), "--attempt", attempt,
        "--seconds", "10", "--", *command,
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
    profile_path, workdir, artifact_root, profile = _write_profile(
        tmp_path, source_paths=["source.py"],
    )
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
    assert _invoke(monkeypatch, runner, profile_path, "success", [sys.executable, "-c", "pass"]) == 0

    result_dir = artifact_root / "runs/success"
    result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    baseline_check = json.loads(
        (result_dir / "source-baseline-check.json").read_text(encoding="utf-8")
    )
    assert result["profile_sha256"] == hashlib.sha256(profile_bytes).hexdigest()
    assert baseline_check["baseline_sha256"] == hashlib.sha256(baseline_bytes).hexdigest()

    monkeypatch.syspath_prepend(str(SCRIPTS))
    verify_run = runpy.run_path(str(SCRIPTS / "verify_run.py"), run_name="verify_run_snapshot_test")
    with pytest.raises(ValueError, match="profile_matches"):
        verify_run["verify_run"](result_dir, RUNNER_PATH)


def test_run_task_uses_parsed_profile_when_pytest_selection_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None,
) -> None:
    selector = "tests/unit/test_allowed.py"
    allowed_node = f"{selector}::test_allowed"
    profile_path, workdir, artifact_root, _profile = _write_profile(
        tmp_path, source_paths=[], extra={"pytest_node_ids": {selector: [allowed_node]}},
    )
    test_file = workdir / selector
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
    profile_path, workdir, artifact_root, profile = _write_profile(
        tmp_path, source_paths=["source.py"],
    )
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
