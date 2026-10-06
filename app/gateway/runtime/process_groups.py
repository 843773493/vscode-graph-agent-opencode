"""owned 进程组的原生身份、成员状态与有界退出核实。"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path


def _linux_process_stat(pid: int) -> tuple[str, int, int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm 可以包含空格与括号；最后一个右括号之后才是 state 起始字段。
    fields = raw.rpartition(")")[2].split()
    if len(fields) < 20:
        raise RuntimeError(f"进程 stat 字段不足，无法核实 owned group: pid={pid}")
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def _capture_posix_group_starttime(pid: int) -> int | None:
    if sys.platform == "linux":
        identity = _linux_process_stat(pid)
        if identity is None:
            return None
        _, group, session, starttime = identity
    else:
        try:
            group, session = os.getpgid(pid), os.getsid(pid)
        except ProcessLookupError:
            return None
        starttime = None
    if group != pid or session != pid:
        raise RuntimeError(
            f"进程不是独立 owned group 的 leader: pid={pid}, pgid={group}, session={session}"
        )
    return starttime


def _posix_group_is_alive(pid: int, leader_starttime: int | None) -> bool:
    if sys.platform != "linux":
        # TODO: 其它 POSIX 缺少成员状态回执，组存在只表示尚不能证明已退出；等待始终有界。
        try:
            if os.getpgid(pid) != pid or os.getsid(pid) != pid:
                raise RuntimeError(f"owned group 的 leader 身份已改变: pid={pid}")
        except ProcessLookupError:
            pass
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        return True
    leader = _linux_process_stat(pid)
    if leader is not None and (
        leader[1:3] != (pid, pid) or leader[3] != leader_starttime
    ):
        raise RuntimeError(f"owned group 的 leader 已被替换: pid={pid}")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_stat(int(entry.name))
        if identity is None:
            continue
        state, group, session, _ = identity
        if group != pid or state in {"Z", "X", "x"}:
            continue
        if leader_starttime is None or session != pid:
            raise RuntimeError(f"无法证明进程属于原 owned group: pid={entry.name}, pgid={pid}")
        return True
    return False


def _wait_until_closed(is_alive: Callable[[], bool], *, deadline: float) -> bool:
    while is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True

