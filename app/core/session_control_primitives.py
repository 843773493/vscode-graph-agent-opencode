"""per-session ``session-control.sqlite`` 各垂直链路共享的形态原语。

本模块只承载跨子包共用的形态约束与校验器，不含任何表的 DDL、行投影或
读写方法：

- ``SHA256_HEX_PATTERN``：sha256 小写 hex 形态（owner binding 的
  ``stable_prefix_hash``、operation lease 的 ``preimage_hash``、thread
  creation 的 ``artifact_manifest_hash`` 与通信 ``payload_hash`` 共用）。
- ``EXECUTION_BINDING_ID_PATTERN`` / ``EXECUTION_JOB_ID_PATTERN``：稳定
  binding/job identity 形态（execution intent 与通信 inbox 绑定共用）。
- ``validate_claim_fields``：claim 领取字段校验（execution intent 与通信
  inbox 的可恢复领取共用同一口径）。
- ``validate_thread_creation_key``：thread creation 幂等键形态校验
  （creation record / collaboration member / execution intent / service
  的 ``.staging/<key>/`` 目录名共用同一口径）。

在 session-control 各子包之间只允许这一份实现；子包一律从此处导入，
禁止各自复制正则（复制会让形态口径分叉）。
"""

from __future__ import annotations

import re

__all__ = [
    "EXECUTION_BINDING_ID_PATTERN",
    "EXECUTION_JOB_ID_PATTERN",
    "SHA256_HEX_PATTERN",
    "validate_claim_fields",
    "validate_thread_creation_key",
]


# sha256 小写 hex 形态（session-control 跨族共同口径）。
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# 稳定 binding/job identity 形态（软件生成、重试不变）：固定前缀 +
# 32 位小写 hex（sha256 派生截断）。
EXECUTION_BINDING_ID_PATTERN = re.compile(r"^tbind_[0-9a-f]{32}$")
EXECUTION_JOB_ID_PATTERN = re.compile(r"^job_[0-9a-f]{32}$")


def validate_claim_fields(claim_owner: str, claim_generation: int) -> None:
    """校验 claim 领取字段：owner 非空、generation 为 >= 1 的整数。"""
    if not isinstance(claim_owner, str) or not claim_owner:
        raise ValueError(f"claim_owner 不能为空: {claim_owner!r}")
    if (
        isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 1
    ):
        raise ValueError(
            f"claim_generation 必须是 >= 1 的整数: {claim_generation!r}"
        )


def validate_thread_creation_key(idempotency_key: str) -> None:
    """校验 thread creation 幂等键：非空且是安全单段路径名。

    幂等键是 session 目录内 ``.staging/<key>/`` staging 目录名（冻结进
    record 的 ``staging_locator``），含分隔符/``.``/``..`` 会破坏定点
    定位，一律拒绝。store 与 thread creation service 共用本实现（服务层
    在状态变更前先行 fail fast，口径必须与 store 完全一致）。

    键的路径组件/总长预算由落盘方（thread creation service）在准入前
    用 ``session_catalog_store.validate_path_budget`` 校验，本模块只做
    形态口径，不复制路径预算常量。
    """
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError(f"idempotency_key 不能为空: {idempotency_key!r}")
    if (
        idempotency_key in (".", "..")
        or "/" in idempotency_key
        or "\\" in idempotency_key
        or "\x00" in idempotency_key
    ):
        raise ValueError(
            "idempotency_key 必须是安全单段路径名（不含分隔符/./..）: "
            f"{idempotency_key!r}"
        )
