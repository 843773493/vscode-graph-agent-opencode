"""独立POSIX命令监督器：资源限额、期限与进程组回收不依赖Backend存活。"""

from __future__ import annotations

import argparse
import os
import resource
import signal
import subprocess
import sys
import time


def run_posix_job(command: list[str], *, memory_bytes: int, timeout_seconds: float) -> int:
    if not command or memory_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("POSIX命令保护需要非空命令及正内存/时间限额")
    # 先限制监督器，命令及其后代继承每进程数据段限额。
    resource.setrlimit(resource.RLIMIT_DATA, (memory_bytes, memory_bytes))
    stopped = False

    def interrupted(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(command, start_new_session=True)
    try:
        while True:
            if stopped:
                return 130
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("命令超过执行期限", file=sys.stderr, flush=True)
                return 124
            try:
                return process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        # 即使根命令已退出，也回收仍继承该进程组的后代。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--memory-limit-kb", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    try:
        return run_posix_job(command, memory_bytes=options.memory_limit_kb * 1024, timeout_seconds=options.timeout_seconds)
    except OSError as error:
        # 真实命令参数和cwd不作为原生错误路径回传；调用方已有原始配置。
        print(f"命令启动或监督失败: {type(error).__name__}, errno={error.errno}", file=sys.stderr, flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
