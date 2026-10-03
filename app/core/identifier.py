from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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


# Unix 纪元与 1 毫秒的 timedelta：把带时区 datetime 折算为整数 Unix 毫秒。
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MILLISECOND = timedelta(milliseconds=1)

def create_uuid_hex() -> str:
    """生成 canonical 身份的 32 位小写 hex payload（UUIDv7）。

    由显式直接依赖 uuid_utils 的 ``uuid7()`` 提供；生成路径 MUST NOT 传显式
    ``timestamp`` —— 显式时间戳会绕过 uuid_utils 的单调时钟上下文（同毫秒内
    改用随机计数器），从而破坏同毫秒单调（实测 8 个同 ``timestamp`` 值按 hex
    序无序）。uuid_utils 默认路径使用进程内单调计数器，同进程同毫秒非递减且
    唯一，并在系统时钟回拨时钳制内嵌时间不后退；跨进程只共享 48 bit 毫秒分辨率。
    """
    value = _resolve_uuid7()()
    return value.hex


def create_prefixed_id(prefix: IdentifierPrefix) -> str:
    """生成带领域前缀的完整 UUID。"""
    return f"{prefix}_{create_uuid_hex()}"


def to_epoch_ms(moment: datetime) -> int:
    """把带时区时刻折算为 Unix 毫秒（整数算术，无浮点误差）。"""
    if moment.tzinfo is None:
        raise ValueError(f"moment 必须带时区: {moment!r}")
    return int((moment.astimezone(UTC) - _EPOCH) // _ONE_MILLISECOND)


def uuid7_datetime_from_hex(payload: str) -> datetime:
    """从已校验的 UUIDv7 hex payload 投影其 UTC 毫秒时刻。"""
    if len(payload) != 32:
        raise ValueError(f"UUIDv7 payload 必须为 32 位 hex: {payload!r}")
    return _EPOCH + timedelta(milliseconds=int(payload[:12], 16))


def create_uuid_hex_at(epoch_ms: int) -> str:
    """按给定的 Unix 毫秒生成 canonical UUIDv7 payload（创建时刻已确定的场景）。

    与 :func:`create_uuid_hex` 的唯一区别：id 内嵌的 48 bit 毫秒时间戳由调用方
    显式指定。用于「创建时刻已冻结、需与既有 ``sessions/YYYY/MM/DD`` 分桶严格
    一致」的场景——存量 v4→v7 重编号 MUST 保持 id 内嵌时间与既有日期桶同日，
    固定日期桶的测试夹具同理。

    显式时间戳路径不参与同毫秒单调计数器，故仅供固定历史 ID 等非实时场景
    使用；实时创建路径 MUST NOT 调用本函数（破坏同毫秒单调，见
    :func:`create_uuid_hex`）。
    """
    if not isinstance(epoch_ms, int) or isinstance(epoch_ms, bool) or epoch_ms < 0:
        raise ValueError(f"epoch_ms 必须是非负整数: {epoch_ms!r}")
    value = _uuid7_at_ms(epoch_ms)
    if int(value.timestamp) != epoch_ms:
        raise RuntimeError(
            "create_uuid_hex_at 生成的内嵌时间戳与请求不一致: "
            f"requested_ms={epoch_ms}, embedded_ms={int(value.timestamp)}"
        )
    return value.hex


def create_prefixed_id_at(prefix: IdentifierPrefix, epoch_ms: int) -> str:
    """按给定的 Unix 毫秒生成带领域前缀的 canonical UUID。

    与 :func:`create_prefixed_id` 的区别仅是内嵌时间戳由调用方指定，语义见
    :func:`create_uuid_hex_at`；用于创建时刻已冻结（如按 ``created_at`` 分配
    身份的创建流）、需与既有日期桶严格一致的场景。
    """
    return f"{prefix}_{create_uuid_hex_at(epoch_ms)}"


def _uuid7_at_ms(epoch_ms: int) -> object:
    """生成内嵌指定 Unix 毫秒的 v7 UUID（uuid_utils 的 timestamp 参数单位为秒）。"""
    seconds, millis = divmod(epoch_ms, 1000)
    return _resolve_uuid7()(timestamp=seconds, nanos=millis * 1_000_000)
