from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

IdentifierPrefix = Literal[
    "adm",
    "attempt",
    "chan",
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
    "ptask",
    "ptrun",
    "req",
    "robs",
    "ses",
    "snapshot",
    "strm",
    "sub",
    "team",
    "tevt",
    "text",
    "thr",
    "turn",
    "tooltest",
    "ttask",
]

_GATEWAY_ID_PATTERN = re.compile(r"gateway_[0-9a-f]{32}")


# Session/Thread 形态是纯身份合同，不依赖 catalog、SQL 或业务运行时。
_SESSION_ID_PATTERN = re.compile(r"ses_[0-9a-f]{32}")
_THREAD_ID_PATTERN = re.compile(r"thr_[0-9a-f]{32}")
_UUID_VERSION_HEX_INDEX = 12
_UUID_VARIANT_HEX_INDEX = 16
_UUID_VARIANT_HEX_CHARS = "89ab"


def validate_session_id(value: str) -> None:
    """校验 canonical session_id：``ses_`` 前缀 + 32 位小写 hex + UUIDv7 位 profile。

    斜杠、反斜杠、Unicode、``.``/``..``、大小写错误、前缀错误、长度错误及
    非 v7 位 profile 一律直接拒绝，不得清洗或截断。
    """
    if not isinstance(value, str):
        raise TypeError(f"session_id 必须是字符串: {value!r}")
    if _SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"session_id 形态非法: {value!r}")
    _validate_uuid_payload(value[4:])


def validate_thread_id(value: str) -> None:
    """校验 canonical thread_id：``thr_`` 前缀 + 32 位小写 hex + UUIDv7 位 profile。"""
    if not isinstance(value, str):
        raise TypeError(f"thread_id 必须是字符串: {value!r}")
    if _THREAD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"thread_id 形态非法: {value!r}")
    _validate_uuid_payload(value[4:])


def _validate_uuid_payload(payload: str) -> None:
    """校验 32 位 hex payload 的 UUIDv7 version/variant 位。"""
    if payload[_UUID_VERSION_HEX_INDEX] != "7":
        raise ValueError(f"ID payload 的 UUID version 位非法: {payload!r}")
    if payload[_UUID_VARIANT_HEX_INDEX] not in _UUID_VARIANT_HEX_CHARS:
        raise ValueError(f"ID payload 的 UUID variant 位非法: {payload!r}")


def validate_gateway_id(value: str) -> str:
    """Gateway 持久身份只接受随机不透明 ID，不接受地址、端口或通道标识。"""
    if not isinstance(value, str) or _GATEWAY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"非法 gateway_id（要求 gateway_<32位小写hex>）: {value!r}")
    return value


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


class IdentifierFactory(Protocol):
    """装配侧唯一 ID 生成边界；测试 factory 不读写业务存储。"""

    def create(self, *, namespace: str, epoch_ms: int | None) -> str: ...


class RandomIdentifierFactory:
    def create(self, *, namespace: str, epoch_ms: int | None) -> str:
        del namespace
        return (_resolve_uuid7()() if epoch_ms is None else _uuid7_at_ms(epoch_ms)).hex


_identifier_factory: IdentifierFactory = RandomIdentifierFactory()


def bind_identifier_factory(factory: IdentifierFactory) -> None:
    """由显式进程 composition 绑定；生产使用模块内随机 factory。"""
    global _identifier_factory
    _identifier_factory = factory


def create_uuid_hex(*, namespace: str = "uuid") -> str:
    """生成 canonical 身份的 32 位小写 hex payload（UUIDv7）。

    由显式直接依赖 uuid_utils 的 ``uuid7()`` 提供；生成路径 MUST NOT 传显式
    ``timestamp`` —— 显式时间戳会绕过 uuid_utils 的单调时钟上下文（同毫秒内
    改用随机计数器），从而破坏同毫秒单调（实测 8 个同 ``timestamp`` 值按 hex
    序无序）。uuid_utils 默认路径使用进程内单调计数器，同进程同毫秒非递减且
    唯一，并在系统时钟回拨时钳制内嵌时间不后退；跨进程只共享 48 bit 毫秒分辨率。
    """
    return _identifier_factory.create(namespace=namespace, epoch_ms=None)


def create_prefixed_id(prefix: IdentifierPrefix) -> str:
    """生成带领域前缀的完整 UUID。"""
    return f"{prefix}_{create_uuid_hex(namespace=prefix)}"


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
    return _create_uuid_hex_at(namespace="uuid", epoch_ms=epoch_ms)


def _create_uuid_hex_at(*, namespace: str, epoch_ms: int) -> str:
    if not isinstance(epoch_ms, int) or isinstance(epoch_ms, bool) or epoch_ms < 0:
        raise ValueError(f"epoch_ms 必须是非负整数: {epoch_ms!r}")
    payload = _identifier_factory.create(namespace=namespace, epoch_ms=epoch_ms)
    embedded_ms = int(payload[:12], 16)
    if embedded_ms != epoch_ms:
        raise RuntimeError(
            "create_uuid_hex_at 生成的内嵌时间戳与请求不一致: "
            f"requested_ms={epoch_ms}, embedded_ms={embedded_ms}"
        )
    return payload


def create_prefixed_id_at(prefix: IdentifierPrefix, epoch_ms: int) -> str:
    """按给定的 Unix 毫秒生成带领域前缀的 canonical UUID。

    与 :func:`create_prefixed_id` 的区别仅是内嵌时间戳由调用方指定，语义见
    :func:`create_uuid_hex_at`；用于创建时刻已冻结（如按 ``created_at`` 分配
    身份的创建流）、需与既有日期桶严格一致的场景。
    """
    payload = _create_uuid_hex_at(namespace=prefix, epoch_ms=epoch_ms)
    return f"{prefix}_{payload}"


def _uuid7_at_ms(epoch_ms: int) -> object:
    """生成内嵌指定 Unix 毫秒的 v7 UUID（uuid_utils 的 timestamp 参数单位为秒）。"""
    seconds, millis = divmod(epoch_ms, 1000)
    return _resolve_uuid7()(timestamp=seconds, nanos=millis * 1_000_000)
