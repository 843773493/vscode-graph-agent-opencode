"""Node 调试进程的 OS 起始身份读取与比对。

启动登记必须以“PID + 进程起始身份”作为可验证属性，绝不能用 PID 或 Inspector 端口
单独认领/停止进程：PID 会被复用，端口会被重新分配。本模块只负责读取事实，不做任何
生命周期决策。

身份来源优先级：
1. Linux ``/proc/<pid>/stat`` 的 ``starttime`` 字段 + ``/proc/sys/kernel/random/boot_id``；
   ``/proc`` 条目存在时它是权威来源，``Z``（zombie）视为已终结，不再回落下一级探测；
2. 环境已自带 ``psutil`` 时使用 ``Process.create_time()``（未声明为项目依赖，仅兜底）；
3. 两者都不可用时只能做存在性探测，``start_marker`` 为 ``None``，调用方必须按
   “不可核实”处理（fail-closed），不得据此认领或停止进程。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: 当前实例与登记身份的比对结果（三态，缺一不可）：
#: ``incomparable`` 表示事实不足，调用方必须 fail-closed，不得当成"已终结"。
IdentityComparison = Literal["match", "mismatch", "incomparable"]

IDENTITY_SOURCE_LINUX_PROC = "linux_proc_stat"
IDENTITY_SOURCE_PSUTIL = "psutil_create_time"
#: 只能证明“存在/不存在”，无法证明是不是同一实例。
IDENTITY_SOURCE_UNAVAILABLE = "unavailable"

_PROC_ROOT = Path("/proc")


@dataclass(frozen=True, slots=True)
class NodeDebugProcessIdentity:
    """某 PID 当前实例的起始身份事实。"""

    pid: int
    source: str
    #: 起始标记；``None`` 表示该平台无法核实起始身份。
    start_marker: str | None
    #: 进程状态字符（Linux），用于把 zombie 视为已终结。
    state: str | None = None

    @property
    def verifiable(self) -> bool:
        return self.start_marker is not None

    def compare(
        self,
        *,
        recorded_source: str | None,
        recorded_start_marker: str | None,
    ) -> IdentityComparison:
        """把当前实例与登记身份比对，区分三种**事实**（调用方必须分别处理）。

        - ``match``：同来源且起始标记相同 ⇒ 就是登记的那一个实例，可按 owner 策略处置；
        - ``mismatch``：同来源但起始标记不同 ⇒ 该 PID 已被回收复用，登记的那一个已终结；
        - ``incomparable``：任一方缺标记、或来源不同构（``boot_id+ticks`` 与 epoch 秒无法
          对齐）⇒ 事实不足。调用方必须 fail-closed：既不能认领/停止该 PID，
          **也不能据此判定原实例已终结**（否则就是虚报终态、错误解除阻断）。
        """
        if self.start_marker is None or recorded_start_marker is None:
            return "incomparable"
        if recorded_source is None or recorded_source != self.source:
            # 来源缺失或跨来源：标记不同构，"不相等"推不出"不是同一实例"。
            return "incomparable"
        return "match" if self.start_marker == recorded_start_marker else "mismatch"


def probe_process_identity(pid: int) -> NodeDebugProcessIdentity | None:
    """返回 PID 当前实例的身份；``None`` 表示该 PID 当前不存在（或已终结）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError(f"进程 PID 必须是正整数: {pid!r}")
    if (_PROC_ROOT / str(pid) / "stat").is_file():
        # /proc 里有该条目时它就是权威来源，其结论（含 zombie＝已终结只剩退出码待回收）
        # 不得再回落到 psutil 或 kill(0) 存在性兜底：否则刚被我们停止、尚未被 reap 的
        # 子进程会被判成"存活但不可核实"，让已成功的停止永远卡在 reconcile_required。
        return _probe_linux_proc_identity(pid)
    psutil_identity = _probe_psutil_identity(pid)
    if psutil_identity is not None:
        return psutil_identity
    if _pid_exists(pid):
        # TODO: 非 Linux 且无 psutil 的宿主缺少起始身份来源；接入跨平台身份库后
        # 删除该分支，让所有平台都能核实“同一实例”。
        return NodeDebugProcessIdentity(
            pid=pid,
            source=IDENTITY_SOURCE_UNAVAILABLE,
            start_marker=None,
        )
    return None


def _probe_linux_proc_identity(pid: int) -> NodeDebugProcessIdentity | None:
    stat_path = _PROC_ROOT / str(pid) / "stat"
    # 单次 read_text 取代"先 is_file 再 read"：两步之间进程可能恰好消失（TOCTOU，
    # R3b 建议 4），读不到条目与"条目不存在"是同一事实 ⇒ 返回 None = 已核实该 PID
    # 当前不存在；除 FileNotFoundError 外的读取错误仍然显式抛出，绝不默默吞掉。
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    # comm 字段可能包含空格与括号，必须从最后一个 ')' 之后再切分。
    closing = raw.rfind(")")
    if closing < 0:
        raise RuntimeError(f"/proc/{pid}/stat 格式异常，无法解析进程起始身份: {raw!r}")
    fields = raw[closing + 2 :].split()
    # 剩余字段从 state 开始：state 是第 3 个字段，starttime 是第 22 个字段。
    if len(fields) < 20:
        raise RuntimeError(f"/proc/{pid}/stat 字段不足，无法解析进程起始身份: {raw!r}")
    state = fields[0]
    starttime_ticks = fields[19]
    if state == "Z":
        # zombie 已经不再执行，视为已终结。
        return None
    boot_id_path = _PROC_ROOT / "sys" / "kernel" / "random" / "boot_id"
    boot_id = (
        boot_id_path.read_text(encoding="utf-8").strip()
        if boot_id_path.is_file()
        else "unknown-boot"
    )
    return NodeDebugProcessIdentity(
        pid=pid,
        source=IDENTITY_SOURCE_LINUX_PROC,
        start_marker=f"{boot_id}:{starttime_ticks}",
        state=state,
    )


def _probe_psutil_identity(pid: int) -> NodeDebugProcessIdentity | None:
    if importlib.util.find_spec("psutil") is None:
        return None
    import psutil

    try:
        process = psutil.Process(pid)
        create_time = process.create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as error:
        raise RuntimeError(f"读取进程起始身份失败: pid={pid}: {error}") from error
    return NodeDebugProcessIdentity(
        pid=pid,
        source=IDENTITY_SOURCE_PSUTIL,
        start_marker=f"{create_time:.6f}",
    )


def _pid_exists(pid: int) -> bool:
    """不带信号的存在性探测；仅用于缺少起始身份来源的宿主。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        raise RuntimeError(f"进程存在性探测失败: pid={pid}: {error}") from error
    return True


__all__ = [
    "IDENTITY_SOURCE_LINUX_PROC",
    "IDENTITY_SOURCE_PSUTIL",
    "IDENTITY_SOURCE_UNAVAILABLE",
    "IdentityComparison",
    "NodeDebugProcessIdentity",
    "probe_process_identity",
]
