"""协议层 canonical ID 形态常量（OpenSpec 2.1）。

本模块只冻结线格式常量。携带 Session/Thread ID 的 wire 字段统一使用
app.core.identifier 的验证器，不在协议层再导出或复制校验规则。
"""

from __future__ import annotations

__all__ = [
    "CANONICAL_ID_HEX_PAYLOAD_LENGTH",
    "CANONICAL_ID_PREFIX_LENGTH",
    "CANONICAL_ID_TOTAL_LENGTH",
    "CANONICAL_SESSION_ID_PREFIX",
    "CANONICAL_THREAD_ID_PREFIX",
]

# 36-byte ASCII 形态：4 字节前缀（ses_/thr_）+ 32 位小写 hex；
# payload 第 13 个 hex 位为 7（UUIDv7 version），第 17 个 hex 位属于
# 8|9|a|b（UUIDv7 variant）。
CANONICAL_ID_PREFIX_LENGTH = 4
CANONICAL_ID_HEX_PAYLOAD_LENGTH = 32
CANONICAL_ID_TOTAL_LENGTH = 36
CANONICAL_SESSION_ID_PREFIX = "ses_"
CANONICAL_THREAD_ID_PREFIX = "thr_"
