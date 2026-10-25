"""owned 进程组的原生身份、成员状态与有界退出核实。"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

ProcessStartMarker = int | float


class _ProcessIdentity(NamedTuple):
    state: str
    process_group_id: int
    process_session_id: int
    process_start_marker: ProcessStartMarker


def _read_linux_process_identity(pid: int) -> _ProcessIdentity | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm 可以包含空格与括号；最后一个右括号之后才是 state 起始字段。
    fields = raw.rpartition(")")[2].split()
    if len(fields) < 20:
        raise RuntimeError(f"进程 stat 字段不足，无法核实 owned group: pid={pid}")
    return _ProcessIdentity(
        state=fields[0],
        process_group_id=int(fields[2]),
        process_session_id=int(fields[3]),
        process_start_marker=int(fields[19]),
    )


def _read_posix_process_identity(pid: int) -> _ProcessIdentity | None:
    import psutil

    try:
        process = psutil.Process(pid)
        process_start_marker = process.create_time()
        status = process.status()
        process_group_id = os.getpgid(pid)
        process_session_id = os.getsid(pid)
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
    return _ProcessIdentity(
        state=state,
        process_group_id=process_group_id,
        process_session_id=process_session_id,
        process_start_marker=process_start_marker,
    )


def _posix_process_group_members(pgid: int) -> list[tuple[int, str]]:
    import psutil

    members: list[tuple[int, str]] = []
    for member_pid in psutil.pids():
        try:
            if os.getpgid(member_pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        except OSError as error:
            raise RuntimeError(
                f"读取 POSIX 进程组成员身份失败: pgid={pgid}, pid={member_pid}: {error}"
            ) from error

        try:
            status = psutil.Process(member_pid).status()
        except psutil.ZombieProcess:
            state = "Z"
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise RuntimeError(
                f"读取 POSIX 进程组成员状态失败: pgid={pgid}, pid={member_pid}: {error}"
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


def _capture_posix_group_leader_start_marker(pid: int) -> ProcessStartMarker | None:
    if sys.platform == "linux":
        identity = _read_linux_process_identity(pid)
        if identity is None:
            return None
    else:
        identity = _read_posix_process_identity(pid)
        if identity is None:
            return None
    if identity.process_group_id != pid or identity.process_session_id != pid:
        raise RuntimeError(
            f"进程不是独立 owned group 的 leader: pid={pid}, "
            f"pgid={identity.process_group_id}, session={identity.process_session_id}"
        )
    return identity.process_start_marker


def _posix_group_is_alive(
    pid: int, leader_start_marker: ProcessStartMarker | None
) -> bool:
    if sys.platform != "linux":
        leader = _read_posix_process_identity(pid)
        if leader is not None:
            if (
                leader.process_group_id != pid
                or leader.process_session_id != pid
                or leader.process_start_marker != leader_start_marker
            ):
                raise RuntimeError(f"owned group 的 leader 已被替换: pid={pid}")
            if leader.state not in {"Z", "X", "x"}:
                return True
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise RuntimeError(
                f"核实 owned group 是否存活失败: pid={pid}: {error}"
            ) from error
        for member_pid, member_state in _posix_process_group_members(pid):
            if member_state in {"Z", "X", "x"}:
                continue
            try:
                member_process_group_id = os.getpgid(member_pid)
                if member_process_group_id != pid:
                    continue
                member_process_session_id = os.getsid(member_pid)
            except ProcessLookupError:
                continue
            if member_process_session_id != pid:
                raise RuntimeError(
                    f"无法证明进程属于原 owned group: pid={member_pid}, "
                    f"pgid={pid}, sid={member_process_session_id}"
                )
            if leader_start_marker is None:
                raise RuntimeError(
                    f"无法证明进程属于原 owned group: pid={member_pid}, pgid={pid}"
                )
            return True
        return False
    leader = _read_linux_process_identity(pid)
    if leader is not None:
        if (
            leader.process_group_id != pid
            or leader.process_session_id != pid
            or leader.process_start_marker != leader_start_marker
        ):
            raise RuntimeError(f"owned group 的 leader 已被替换: pid={pid}")
        if leader.state not in {"Z", "X", "x"}:
            return True
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError(
            f"核实 owned group 是否存活失败: pid={pid}: {error}"
        ) from error
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        identity = _read_linux_process_identity(int(entry.name))
        if identity is None:
            continue
        if identity.process_group_id != pid or identity.state in {"Z", "X", "x"}:
            continue
        if leader_start_marker is None or identity.process_session_id != pid:
            raise RuntimeError(
                f"无法证明进程属于原 owned group: pid={entry.name}, pgid={pid}"
            )
        return True
    return False


def _wait_until_closed(is_alive: Callable[[], bool], *, deadline: float) -> bool:
    while is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True
