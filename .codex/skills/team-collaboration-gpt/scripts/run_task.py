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
    fingerprints = source_fingerprints(workdir, paths)
    # mkdir 不允许复用 attempt；失败原件不会被下一次同名运行覆盖。
    attempts = artifact_root / "runs"
    attempts.mkdir(exist_ok=True)
    result_dir = attempts / args.attempt
    result_dir.mkdir()
    write_json(result_dir / "source-before.json", fingerprints)
    argv = ["timeout", "--kill-after=10", str(args.seconds), "bash", "-c",
            'ulimit -d 4194304; exec "$@"', "bash", *command]
    started = time.monotonic()
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
        finally:
            remaining = group_members(child.pid)
            if remaining:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
    logs = {}
    for name in ("stdout", "stderr"):
        path = result_dir / f"{name}.raw"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        logs[name] = {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}
    write_json(result_dir / "source-after.json", source_fingerprints(workdir, paths))
    result = {**start, "ended_utc": utc_now(), "duration_seconds": time.monotonic() - started,
              "exit_code": actual, "interrupted_signal": interrupted_signal, "logs": logs,
              "cleanup_killed_members": remaining, "remaining_group_members": group_members(child.pid)}
    write_json(result_dir / "result.json", result)
    print(json.dumps({"result": str(result_dir / "result.json"), "exit_code": actual}), flush=True)
    return actual if actual >= 0 else 128 - actual


if __name__ == "__main__":
    raise SystemExit(main())
