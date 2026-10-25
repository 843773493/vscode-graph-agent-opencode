"""按主代理固定的任务配置运行命令，并保留不可覆盖的原始结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

POSIX_PROCESS_GROUP = "posix_process_group"
WINDOWS_JOB_OBJECT = "windows_job_object"
POSIX_DATA_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
WINDOWS_JOB_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
WINDOWS_MATRIX_JOB_MEMORY_LIMIT_BYTES = 16 * 1024 * 1024 * 1024


_WINDOWS_JOB_WRAPPER = """import json
import sys
from pathlib import Path

repository_root = Path(sys.argv[1])
sys.path.insert(0, str(repository_root))
from tests.support.processes.windows_job import run_windows_job

command = json.loads(sys.argv[2])
memory_bytes = int(sys.argv[3])
timeout_seconds = int(sys.argv[4])
receipt_path = Path(sys.argv[5])
exit_code = run_windows_job(
    command,
    memory_bytes=memory_bytes,
    timeout_seconds=timeout_seconds,
)
with receipt_path.open("x", encoding="utf-8") as stream:
    json.dump(
        {"exit_code": exit_code, "process_tree_empty": True},
        stream,
        ensure_ascii=False,
        indent=2,
    )
    stream.write("\\n")
raise SystemExit(exit_code)
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def source_fingerprints(workdir: Path, paths: list[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for relative in paths:
        target = (workdir / relative).resolve()
        if not target.is_relative_to(workdir):
            raise ValueError(f"源码清单越界：{relative}")
        if not target.exists():
            result[relative] = {"missing": True}
        else:
            data = target.read_bytes()
            result[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return result


def pytest_node_id_rules(profile: dict[str, object], workdir: Path) -> dict[Path, set[str]]:
    configured = profile.get("pytest_node_ids")
    if configured is None:
        return {}
    if not isinstance(configured, dict):
        raise TypeError("profile.pytest_node_ids 必须是文件路径到 node ID 列表的对象")
    rules: dict[Path, set[str]] = {}
    for raw_path, raw_node_ids in configured.items():
        if not isinstance(raw_path, str) or not raw_path or "::" in raw_path:
            raise ValueError("profile.pytest_node_ids 的键必须是 pytest 文件路径")
        if not isinstance(raw_node_ids, list) or not raw_node_ids:
            raise ValueError(f"profile.pytest_node_ids[{raw_path!r}] 必须是非空 node ID 列表")
        file_path = Path(raw_path)
        if not file_path.is_absolute():
            file_path = workdir / file_path
        file_path = file_path.resolve()
        if not file_path.is_relative_to(workdir):
            raise ValueError(f"profile.pytest_node_ids 文件路径不在 workdir 内: {raw_path}")
        allowed: set[str] = set()
        for raw_node_id in raw_node_ids:
            if not isinstance(raw_node_id, str) or not raw_node_id:
                raise ValueError(f"profile.pytest_node_ids[{raw_path!r}] 含非法 node ID")
            node_path, separator, registered_node = raw_node_id.partition("::")
            resolved_node_path = Path(node_path)
            if not resolved_node_path.is_absolute():
                resolved_node_path = workdir / resolved_node_path
            if not separator or not registered_node or resolved_node_path.resolve() != file_path:
                raise ValueError(
                    f"profile.pytest_node_ids node ID 必须属于登记文件: {raw_node_id}"
                )
            allowed.add(registered_node)
        if not allowed:
            raise ValueError(f"profile.pytest_node_ids[{raw_path!r}] 没有有效 node ID")
        rules.setdefault(file_path, set()).update(allowed)
    return rules


def pytest_selectors(command: list[str]) -> list[str]:
    """只识别本入口已有的 pytest 前缀和显式文件参数，不解析完整 CLI。"""
    if command[:3] == ["uv", "run", "pytest"]:
        selector_arguments = command[3:]
    elif command[:4] == ["uv", "run", "--no-sync", "pytest"]:
        selector_arguments = command[4:]
    else:
        return []
    selectors = []
    skip_option_value = False
    for raw_selector in selector_arguments:
        if skip_option_value:
            skip_option_value = False
            continue
        if raw_selector in {"--ignore", "--deselect"}:
            skip_option_value = True
            continue
        if raw_selector.startswith("-"):
            continue
        selectors.append(raw_selector)
    return selectors


def matrix_runner_owns_memory_limit(command: list[str]) -> bool:
    """正式单 suite matrix runner 自己设置其 suite 数据段上限。"""
    return (
        len(command) == 5
        and command[:4] == ["bun", "run", "test:matrix", "--"]
        and command[4].startswith("--suite=")
        and len(command[4]) > len("--suite=")
    )


def enforce_pytest_node_selection(
    command: list[str], profile: dict[str, object], workdir: Path, result_dir: Path,
) -> str | None:
    selectors = pytest_selectors(command)
    if not selectors:
        return None
    rules = pytest_node_id_rules(profile, workdir)
    if not rules:
        return None
    selected: list[dict[str, object]] = []
    failures: list[str] = []
    for raw_selector in selectors:
        raw_path, separator, node_id = raw_selector.partition("::")
        file_path = Path(raw_path)
        if not file_path.is_absolute():
            file_path = workdir / file_path
        file_path = file_path.resolve()
        allowed_node_ids = rules.get(file_path)
        if allowed_node_ids is None:
            continue
        selected.append({
            "raw": raw_selector,
            "file": str(file_path),
            "node_id": node_id if separator else None,
        })
        if not separator:
            failures.append(f"裸文件选择器会运行整个测试文件: {raw_selector}")
        elif node_id not in allowed_node_ids:
            failures.append(f"profile 未登记该 node ID: {raw_selector}")
    if not selected:
        return None
    check_path = result_dir / "command-selection-check.json"
    write_json(check_path, {
        "checked_at": utc_now(),
        "command": command,
        "selected": selected,
        "allowed_node_ids": {
            str(path): sorted(node_ids) for path, node_ids in rules.items()
        },
        "passed": not failures,
        "launch_blocked": bool(failures),
        "failures": failures,
    })
    if failures:
        return (
            "pytest 命令选择违反 profile node ID 合同；未启动 child："
            f"{'; '.join(failures)}；原件：{check_path}\n"
        )
    return None


def process_group_members(pgid: int) -> list[int]:
    if os.name != "posix":
        raise RuntimeError(f"POSIX process group 不适用于当前平台: {sys.platform}")
    if sys.platform == "linux":
        members = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                members.append(int(entry.name))
        return members

    process_listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,stat="],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    members = []
    for line in process_listing.splitlines():
        pid_text, group_text, state = line.split(None, 2)
        if int(group_text) == pgid and not state.startswith(("Z", "X")):
            members.append(int(pid_text))
    return members


def _set_posix_data_limit() -> None:
    import resource

    _, hard_limit = resource.getrlimit(resource.RLIMIT_DATA)
    if hard_limit == resource.RLIM_INFINITY:
        hard_limit = POSIX_DATA_LIMIT_BYTES
    else:
        hard_limit = min(hard_limit, POSIX_DATA_LIMIT_BYTES)
    resource.setrlimit(resource.RLIMIT_DATA, (hard_limit, hard_limit))


def _windows_job_command(
    command: list[str], *, receipt_path: Path, memory_limit_bytes: int,
    timeout_seconds: int, repository_root: Path,
) -> list[str]:
    return [
        sys.executable,
        "-c",
        _WINDOWS_JOB_WRAPPER,
        str(repository_root),
        json.dumps(command, ensure_ascii=False),
        str(memory_limit_bytes),
        str(timeout_seconds),
        str(receipt_path),
    ]


def _request_process_termination(
    child: subprocess.Popen[bytes], process_guard: str,
) -> None:
    if child.poll() is not None:
        return
    if process_guard == POSIX_PROCESS_GROUP:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        child.terminate()


def _force_process_termination(
    child: subprocess.Popen[bytes], process_guard: str,
) -> None:
    if child.poll() is not None:
        return
    if process_guard == POSIX_PROCESS_GROUP:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        child.kill()


def _wait_for_runner_process(
    child: subprocess.Popen[bytes], *, seconds: int, process_guard: str,
    interrupted_signal: Callable[[], int | None],
) -> int:
    started = time.monotonic()
    command_deadline = started + seconds
    # Windows Job Object runner 负责命令期限和整树回收；外层期限用于发现监督器挂起。
    outer_deadline = command_deadline + (20 if process_guard == WINDOWS_JOB_OBJECT else 0)
    termination_started: float | None = None
    while child.poll() is None:
        now = time.monotonic()
        signum = interrupted_signal()
        if termination_started is None and (
            signum is not None or now >= outer_deadline
        ):
            _request_process_termination(child, process_guard)
            termination_started = now
        elif termination_started is not None and now - termination_started >= 10:
            _force_process_termination(child, process_guard)
            termination_started = now
        try:
            child.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            continue

    if termination_started is not None and interrupted_signal() is None:
        return 124
    if interrupted_signal() is not None:
        return 128 + int(interrupted_signal())
    return int(child.returncode)


def _cleanup_posix_process_group(pgid: int) -> list[int]:
    remaining_process_ids = process_group_members(pgid)
    if not remaining_process_ids:
        return []
    killed_process_ids = remaining_process_ids.copy()
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while remaining_process_ids and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining_process_ids = process_group_members(pgid)
    if remaining_process_ids:
        raise RuntimeError(
            "POSIX 进程组强制回收后仍有成员: "
            f"pgid={pgid}, pids={remaining_process_ids}"
        )
    return killed_process_ids


def _windows_job_receipt(path: Path, exit_code: int) -> dict[str, object] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    receipt = json.loads(raw)
    if receipt != {"exit_code": exit_code, "process_tree_empty": True}:
        return None
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--seconds", required=True, type=int)
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    profile_path = args.profile.resolve(strict=True)
    profile_bytes = profile_path.read_bytes()
    profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    profile = json.loads(profile_bytes)
    workdir = Path(profile["workdir"]).resolve(strict=True)
    project = Path(profile["project_root"]).resolve(strict=True)
    artifact_root = Path(profile["artifacts"]).resolve(strict=True)
    if not artifact_root.is_relative_to(project / "out/tests/temp"):
        raise ValueError("运行原件必须位于主代理分配的 out/tests/temp 目录")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.attempt):
        raise ValueError("attempt 只能使用名称，不能包含目录")
    if not 1 <= args.seconds <= profile["max_seconds"]:
        raise ValueError("超时超出本任务运行配置")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("缺少运行命令")
    environment = os.environ.copy()
    environment.update({
        "UV_PROJECT_ENVIRONMENT": str(project / ".venv"),
        "UV_NO_SYNC": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(workdir), "GIT_OPTIONAL_LOCKS": "0",
        "GIT_INDEX_FILE": os.devnull,
    })
    environment.update(profile.get("environment", {}))
    overrides = {}
    for setting in args.set:
        name, separator, value = setting.partition("=")
        if not separator or name not in profile.get("allowed_options", []):
            raise ValueError(f"不允许覆盖运行参数：{name}")
        overrides[name] = value
    environment.update(overrides)
    for name in ("TMPDIR", "TMP", "TEMP", "BOXTEAM_HOME"):
        if name in environment:
            target = Path(environment[name]).resolve(strict=True)
            if not target.is_relative_to(project / "out"):
                raise ValueError(f"{name} 不在分配的 out 目录")
    paths = profile.get("source_paths", [])
    if "source_manifest" in profile:
        paths = sorted(set(paths) | set(json.loads(Path(profile["source_manifest"]).read_text())["files"]))
    # 新增具名回归不依赖人工把实际测试文件抄入 profile；fixture/callee 仍由 owner 登记。
    for selector in pytest_selectors(command):
        target = (workdir / selector.partition("::")[0]).resolve()
        if target.suffix == ".py" and target.is_file():
            paths = sorted(set(paths) | {target.relative_to(workdir).as_posix()})
    baseline_path = None
    baseline = None
    baseline_sha256 = None
    if "source_baseline" in profile:
        baseline_path = Path(profile["source_baseline"]).resolve(strict=True)
        baseline_bytes = baseline_path.read_bytes()
        baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
        baseline = json.loads(baseline_bytes)
        paths = sorted(set(paths) | set(baseline["files"]))
    source_before_started = time.monotonic()
    fingerprints = source_fingerprints(workdir, paths)
    source_before_seconds = time.monotonic() - source_before_started
    # mkdir 不允许复用 attempt；失败原件不会被下一次同名运行覆盖。
    attempts = artifact_root / "runs"
    attempts.mkdir(exist_ok=True)
    result_dir = attempts / args.attempt
    result_dir.mkdir()
    write_json(result_dir / "source-before.json", fingerprints)
    if baseline is not None:
        # 固定验收在启动进程前比较源码，不能仅记录错配后的指纹。
        mismatches = [relative for relative, expected in baseline["files"].items()
                      if fingerprints[relative] != expected]
        write_json(result_dir / "source-baseline-check.json", {
            "baseline": str(baseline_path), "candidate_tree": baseline["candidate_tree"],
            "baseline_sha256": baseline_sha256,
            "source_count": len(baseline["files"]), "mismatched_paths": mismatches,
            "passed": not mismatches,
        })
        if mismatches:
            raise ValueError(f"源码与固定候选不一致，未启动命令：{result_dir / 'source-baseline-check.json'}")
    started = time.monotonic()
    selection_error = enforce_pytest_node_selection(
        command, profile, workdir, result_dir
    )
    if selection_error is not None:
        (result_dir / "stdout.raw").write_bytes(b"")
        (result_dir / "stderr.raw").write_text(selection_error, encoding="utf-8")
        logs = {}
        for name in ("stdout", "stderr"):
            path = result_dir / f"{name}.raw"
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            logs[name] = {
                "path": str(path), "sha256": digest, "bytes": path.stat().st_size,
            }
        source_after = source_fingerprints(workdir, paths)
        write_json(result_dir / "source-after.json", source_after)
        result_path = result_dir / "result.json"
        write_json(result_path, {
            "ended_utc": utc_now(), "duration_seconds": time.monotonic() - started,
            "exit_code": 2, "child_started": False, "launch_blocked": True,
            "cwd": str(workdir), "profile": str(profile_path),
            "profile_sha256": profile_sha256,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "requested_command": command, "logs": logs,
            "source_before": fingerprints, "source_after": source_after,
            "command_selection_check": str(result_dir / "command-selection-check.json"),
        })
        print(json.dumps({
            "result": str(result_path), "exit_code": 2, "launch_blocked": True,
        }), flush=True)
        return 2
    matrix_manages_memory = matrix_runner_owns_memory_limit(command)
    process_guard = WINDOWS_JOB_OBJECT if os.name == "nt" else POSIX_PROCESS_GROUP
    windows_job_runner: Path | None = None
    windows_job_runner_sha256: str | None = None
    windows_job_receipt_path: Path | None = None
    if process_guard == WINDOWS_JOB_OBJECT:
        repository_root = Path.cwd().resolve()
        windows_job_runner = repository_root / "tests/support/processes/windows_job.py"
        if not windows_job_runner.is_file():
            raise FileNotFoundError(f"Windows Job Object runner 不存在: {windows_job_runner}")
        windows_job_runner_sha256 = hashlib.sha256(
            windows_job_runner.read_bytes()
        ).hexdigest()
        windows_job_receipt_path = result_dir / "windows-job-receipt.json"
        # Matrix suite 单独限制测试树；16GiB 外层留出 runner 与回收开销。
        memory_limit = (
            WINDOWS_MATRIX_JOB_MEMORY_LIMIT_BYTES
            if matrix_manages_memory
            else WINDOWS_JOB_MEMORY_LIMIT_BYTES
        )
        argv = _windows_job_command(
            command,
            receipt_path=windows_job_receipt_path,
            memory_limit_bytes=memory_limit,
            timeout_seconds=args.seconds,
            repository_root=repository_root,
        )
        popen_options: dict[str, object] = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        argv = command
        popen_options = {"start_new_session": True}
        if not matrix_manages_memory:
            # 限额在进程创建前由 Python 设置，保留可执行命令的原始 argv。
            popen_options["preexec_fn"] = _set_posix_data_limit
    start = {"started_utc": utc_now(), "runner_pid": os.getpid(),
             "cwd": str(workdir), "profile": str(profile_path),
             "profile_sha256": profile_sha256,
             "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "argv": argv, "options": overrides, "process_guard": process_guard}
    if windows_job_runner is not None:
        start["process_tree_runner"] = str(windows_job_runner)
        start["process_tree_runner_sha256"] = windows_job_runner_sha256
    interrupted_signal: int | None = None
    child: subprocess.Popen[bytes] | None = None

    def interrupt_run(signum: int, _frame: object) -> None:
        nonlocal interrupted_signal
        interrupted_signal = signum

    signal.signal(signal.SIGTERM, interrupt_run)
    signal.signal(signal.SIGINT, interrupt_run)
    with (result_dir / "stdout.raw").open("xb") as stdout, (result_dir / "stderr.raw").open("xb") as stderr:
        command_started = time.monotonic()
        child = subprocess.Popen(argv, cwd=workdir, env=environment,
                                 stdout=stdout, stderr=stderr, **popen_options)
        try:
            start["pid"] = child.pid
            if process_guard == POSIX_PROCESS_GROUP:
                start["pgid"] = child.pid
                if sys.platform == "linux":
                    try:
                        start["start_time_ticks"] = (
                            Path(f"/proc/{child.pid}/stat")
                            .read_text()
                            .rsplit(")", 1)[1]
                            .split()[19]
                        )
                    except FileNotFoundError:
                        start["start_time_ticks"] = None
                else:
                    start["start_time_ticks"] = None
            else:
                start["process_tree_receipt_path"] = str(windows_job_receipt_path)
            write_json(result_dir / "started.json", start)
            print(json.dumps({"result_dir": str(result_dir), "pid": child.pid}), flush=True)
            command_exit_code = _wait_for_runner_process(
                child,
                seconds=args.seconds,
                process_guard=process_guard,
                interrupted_signal=lambda: interrupted_signal,
            )
            command_seconds = time.monotonic() - command_started
            command_exit = {
                "stage": "command_exited", "ended_utc": utc_now(),
                "pid": child.pid, "exit_code": command_exit_code,
                "command_seconds": command_seconds, "process_guard": process_guard,
            }
            if process_guard == POSIX_PROCESS_GROUP:
                command_exit["pgid"] = child.pid
            write_json(result_dir / "command-exit.json", command_exit)
        finally:
            cleanup_started = time.monotonic()
            child.wait()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            cleanup_seconds = time.monotonic() - cleanup_started
    cleanup_killed_process_ids: list[int] = []
    windows_job_receipt: dict[str, object] | None = None
    if process_guard == POSIX_PROCESS_GROUP:
        cleanup_started = time.monotonic()
        cleanup_killed_process_ids = _cleanup_posix_process_group(child.pid)
        cleanup_seconds += time.monotonic() - cleanup_started
    else:
        assert windows_job_receipt_path is not None
        windows_job_receipt = _windows_job_receipt(
            windows_job_receipt_path, command_exit_code
        )
    log_hash_started = time.monotonic()
    logs = {}
    for name in ("stdout", "stderr"):
        path = result_dir / f"{name}.raw"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        logs[name] = {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}
    log_hash_seconds = time.monotonic() - log_hash_started
    source_after_started = time.monotonic()
    write_json(result_dir / "source-after.json", source_fingerprints(workdir, paths))
    source_after_seconds = time.monotonic() - source_after_started
    result = {**start, "ended_utc": utc_now(), "duration_seconds": time.monotonic() - started,
              "exit_code": command_exit_code, "interrupted_signal": interrupted_signal,
              "logs": logs,
              "phase_seconds": {
                  "source_before": source_before_seconds, "command": command_seconds,
                  "cleanup": cleanup_seconds, "log_hash": log_hash_seconds,
                  "source_after": source_after_seconds,
              }}
    if process_guard == POSIX_PROCESS_GROUP:
        result["cleanup_killed_process_ids"] = cleanup_killed_process_ids
        # 清理函数只会在最后一次进程组扫描为空后返回。
        result["remaining_process_group_members"] = []
    else:
        result["process_tree_receipt"] = windows_job_receipt
        result["process_tree_cleanup_verified"] = windows_job_receipt is not None
    write_json(result_dir / "result.json", result)
    print(json.dumps({"result": str(result_dir / "result.json"), "exit_code": command_exit_code}), flush=True)
    if (
        process_guard == WINDOWS_JOB_OBJECT
        and windows_job_receipt is None
        and command_exit_code == 0
    ):
        return 1
    if interrupted_signal is not None:
        return 128 + interrupted_signal
    return command_exit_code if command_exit_code >= 0 else 128 - command_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
