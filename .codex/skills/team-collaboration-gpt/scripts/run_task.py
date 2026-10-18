"""按主代理固定的任务配置运行命令，并保留不可覆盖的原始结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


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


def group_members(pgid: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            members.append(int(entry.name))
    return members


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--seconds", required=True, type=int)
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    profile_path = args.profile.resolve(strict=True)
    profile = json.loads(profile_path.read_text())
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
    if "source_baseline" in profile:
        baseline_path = Path(profile["source_baseline"]).resolve(strict=True)
        baseline = json.loads(baseline_path.read_text())
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
            "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
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
            "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "requested_command": command, "logs": logs,
            "source_before": fingerprints, "source_after": source_after,
            "command_selection_check": str(result_dir / "command-selection-check.json"),
        })
        print(json.dumps({
            "result": str(result_path), "exit_code": 2, "launch_blocked": True,
        }), flush=True)
        return 2
    argv = ["timeout", "--kill-after=10", str(args.seconds)]
    if matrix_runner_owns_memory_limit(command):
        # 正式 suite runner 按矩阵配置设硬上限，外层不能提前压低 hard limit。
        argv.extend(command)
    else:
        argv.extend([
            "bash", "-c",
            'ulimit -d 4194304 || { printf "run_task: 无法设置数据段内存上限，未启动命令\\n" >&2; exit 97; }; exec "$@"',
            "bash", *command,
        ])
    start = {"started_utc": utc_now(), "runner_pid": os.getpid(),
             "cwd": str(workdir), "profile": str(profile_path),
             "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
             "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "argv": argv, "options": overrides}
    interrupted_signal: int | None = None
    child: subprocess.Popen[bytes] | None = None

    def interrupt_run(signum: int, _frame: object) -> None:
        nonlocal interrupted_signal
        interrupted_signal = signum
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, interrupt_run)
    signal.signal(signal.SIGINT, interrupt_run)
    with (result_dir / "stdout.raw").open("xb") as stdout, (result_dir / "stderr.raw").open("xb") as stderr:
        command_started = time.monotonic()
        child = subprocess.Popen(argv, cwd=workdir, env=environment,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            start["pid"] = child.pid
            start["pgid"] = child.pid
            try:
                start["start_time_ticks"] = Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            except FileNotFoundError:
                start["start_time_ticks"] = None
            write_json(result_dir / "started.json", start)
            print(json.dumps({"result_dir": str(result_dir), "pid": child.pid}), flush=True)
            if interrupted_signal is not None:
                interrupt_run(interrupted_signal, None)
            actual = child.wait()
            command_seconds = time.monotonic() - command_started
            # 命令终态先落盘，后续清理和指纹采集不能伪装成仍在执行。
            write_json(result_dir / "command-exit.json", {
                "stage": "command_exited", "ended_utc": utc_now(),
                "pid": child.pid, "pgid": child.pid, "exit_code": actual,
                "command_seconds": command_seconds,
            })
        finally:
            cleanup_started = time.monotonic()
            remaining = group_members(child.pid)
            if remaining:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            cleanup_seconds = time.monotonic() - cleanup_started
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
    final_group_check_started = time.monotonic()
    final_group_members = group_members(child.pid)
    final_group_check_seconds = time.monotonic() - final_group_check_started
    result = {**start, "ended_utc": utc_now(), "duration_seconds": time.monotonic() - started,
              "exit_code": actual, "interrupted_signal": interrupted_signal, "logs": logs,
              "cleanup_killed_members": remaining, "remaining_group_members": final_group_members,
              "phase_seconds": {
                  "source_before": source_before_seconds, "command": command_seconds,
                  "cleanup": cleanup_seconds, "log_hash": log_hash_seconds,
                  "source_after": source_after_seconds, "final_group_check": final_group_check_seconds,
              }}
    write_json(result_dir / "result.json", result)
    print(json.dumps({"result": str(result_dir / "result.json"), "exit_code": actual}), flush=True)
    return actual if actual >= 0 else 128 - actual


if __name__ == "__main__":
    raise SystemExit(main())
