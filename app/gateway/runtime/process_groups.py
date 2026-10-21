"""owned 进程组的原生身份、成员状态与有界退出核实。"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

ProcessStartTime = int | float


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


def _posix_process_stat(pid: int) -> tuple[str, int, int, float] | None:
    import psutil

    try:
        process = psutil.Process(pid)
        starttime = process.create_time()
        status = process.status()
        group, session = os.getpgid(pid), os.getsid(pid)
        if not process.is_running():
            return None
    except psutil.NoSuchProcess:
        return None
    except ProcessLookupError:
        return None
    except psutil.Error as error:
        raise RuntimeError(f"读取 POSIX 进程身份失败: pid={pid}: {error}") from error
    state = (
        "Z"
        if status == psutil.STATUS_ZOMBIE
        else "X"
        if status == psutil.STATUS_DEAD
        else "S"
    )
    return state, group, session, starttime


def _posix_process_group_members(pid: int) -> list[tuple[int, str]]:
    import psutil

    members: list[tuple[int, str]] = []
    for member_pid in psutil.pids():
        try:
            if os.getpgid(member_pid) != pid:
                continue
        except ProcessLookupError:
            continue
        except OSError as error:
            raise RuntimeError(
                f"读取 POSIX 进程组成员身份失败: pgid={pid}, pid={member_pid}: {error}"
            ) from error

        try:
            status = psutil.Process(member_pid).status()
        except psutil.ZombieProcess:
            state = "Z"
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise RuntimeError(
                f"读取 POSIX 进程组成员状态失败: pgid={pid}, pid={member_pid}: {error}"
            ) from error
        else:
            state = (
                "Z"
                if status == psutil.STATUS_ZOMBIE
                else "X"
                if status == psutil.STATUS_DEAD
                else "S"
            )
        members.append((member_pid, state))
    return members


def _capture_posix_group_starttime(pid: int) -> ProcessStartTime | None:
    if sys.platform == "linux":
        identity = _linux_process_stat(pid)
        if identity is None:
            return None
        _, group, session, starttime = identity
    else:
        identity = _posix_process_stat(pid)
        if identity is None:
            return None
        _, group, session, starttime = identity
    if group != pid or session != pid:
        raise RuntimeError(
            f"进程不是独立 owned group 的 leader: pid={pid}, pgid={group}, session={session}"
        )
    return starttime


def _posix_group_is_alive(pid: int, leader_starttime: ProcessStartTime | None) -> bool:
    if sys.platform != "linux":
        leader = _posix_process_stat(pid)
        if leader is not None:
            state, group, session, starttime = leader
            if (group, session) != (pid, pid) or starttime != leader_starttime:
                raise RuntimeError(f"owned group 的 leader 已被替换: pid={pid}")
            if state not in {"Z", "X", "x"}:
                return True
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise RuntimeError(f"核实 owned group 是否存活失败: pid={pid}: {error}") from error
        for member_pid, state in _posix_process_group_members(pid):
            if state in {"Z", "X", "x"}:
                continue
            try:
                group = os.getpgid(member_pid)
                if group != pid:
                    continue
                session = os.getsid(member_pid)
            except ProcessLookupError:
                continue
            if session != pid:
                raise RuntimeError(
                    f"无法证明进程属于原 owned group: pid={member_pid}, pgid={pid}, sid={session}"
                )
            if leader_starttime is None:
                raise RuntimeError(
                    f"无法证明进程属于原 owned group: pid={member_pid}, pgid={pid}"
                )
            return True
        return False
    leader = _linux_process_stat(pid)
    if leader is not None:
        state, group, session, starttime = leader
        if (group, session) != (pid, pid) or starttime != leader_starttime:
            raise RuntimeError(f"owned group 的 leader 已被替换: pid={pid}")
        if state not in {"Z", "X", "x"}:
            return True
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError(f"核实 owned group 是否存活失败: pid={pid}: {error}") from error
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

