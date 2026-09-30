from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

IdentifierPrefix = Literal[
    "attempt",
    "bgm",
    "bgt",
    "chan",
    "comm",
    "dbgcfg",
    "evt",
    "gen",
    "goal",
    "grun",
    "gwn",
    "intr",
    "job",
    "lease",
    "msg",
    "node-bp",
    "node-debug-action",
    "node-debug-proc",
    "op",
    "part",
    "patch",
    "req",
    "robs",
    "ses",
    "snapshot",
    "src",
    "strm",
    "sub",
    "team",
    "tevt",
    "thr",
    "tooltest",
    "ttask",
]


def _resolve_uuid7() -> Callable[[], object]:
    """解析 `uuid_utils.uuid7`，缺失即 fail-closed。

    canonical 身份 MUST 由显式直接依赖 `uuid-utils>=0.16` 的 `uuid7()` 生成。
    导入失败或 API 不可用时 MUST 抛出详细错误并停止，**MUST NOT 回退
    `uuid.uuid4()`** 或任何虚假默认值（AGENTS.md：永不返回虚假的默认值）。
    """
    try:
        import uuid_utils
    except ImportError as error:
        raise RuntimeError(
            "canonical id 生成失败：缺少显式直接依赖 uuid-utils（>=0.16）。"
            "请在 pyproject.toml 声明并安装 uuid-utils；"
            "禁止回退 uuid.uuid4()。"
        ) from error
    uuid7 = getattr(uuid_utils, "uuid7", None)
    if not callable(uuid7):
        # 这是「环境能力缺失」而非调用方传入类型错误，故 RuntimeError，
        # 而非 TypeError（TRY004 不适用）。
        raise RuntimeError(  # noqa: TRY004
            "canonical id 生成失败：uuid_utils 未提供可调用的 uuid7()。"
            f"已安装版本={getattr(uuid_utils, '__version__', 'unknown')!r}；"
            "禁止回退 uuid.uuid4()。"
        )
    return uuid7


# 可注入的墙钟毫秒源（测试用）。生产绑定真实系统时钟。
_wall_clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000

# 进程内上一次已发放的 Unix 毫秒（时钟回拨钳制下界，design D4）。
_last_issued_ms: int | None = None


def effective_now_ms() -> int:
    """返回进程内单调钳制的「创建时刻」Unix 毫秒（design D4）。

    新值 = ``max(墙钟毫秒, 上次已发放毫秒)``：系统时钟回拨（NTP 校时）时不
    后退。``_last_issued_ms`` 由 ``create_uuid_hex`` 与上一次本函数调用共同
    抬高，故钳制值恒不小于最近一次发放的 id 内嵌时间戳。

    ``sessions/YYYY/MM/DD`` 分桶日期 MUST 由本函数返回值推出，而 id 内嵌
    时间戳由 uuid_utils 的同一进程内单调时钟提供；两者同源且均不回退，故
    分桶与 id 内嵌时间戳的 UTC 日期恒一致。
    """
    global _last_issued_ms
    now_ms = _wall_clock_ms()
    effective = now_ms if _last_issued_ms is None else max(now_ms, _last_issued_ms)
    _last_issued_ms = effective
    return effective


def create_uuid_hex() -> str:
    """生成 canonical 身份的 32 位小写 hex payload（UUIDv7）。

    由显式直接依赖 uuid_utils 的 ``uuid7()`` 提供；生成路径 MUST NOT 传显式
    ``timestamp`` —— 显式时间戳会绕过 uuid_utils 的单调时钟上下文（同毫秒内
    改用随机计数器），从而破坏同毫秒单调（实测 8 个同 ``timestamp`` 值按 hex
    序无序）。uuid_utils 默认路径使用进程内单调计数器，同进程同毫秒非递减且
    唯一，并在系统时钟回拨时保持时间戳不后退；跨进程只共享 48 bit 毫秒分辨率。
    """
    global _last_issued_ms
    value = _resolve_uuid7()()
    issued_ms = int(value.timestamp)
    if _last_issued_ms is None or issued_ms > _last_issued_ms:
        _last_issued_ms = issued_ms
    return value.hex


def create_prefixed_id(prefix: IdentifierPrefix) -> str:
    """生成带领域前缀的完整 UUID。"""
    return f"{prefix}_{create_uuid_hex()}"
