"""协议层 canonical ID 形态常量（OpenSpec 2.1）。

唯一实现在 app.core.session_catalog_store（验证器与形态正则）；本模块是
协议层（API/Proto 编解码）的复用面，只冻结形态常量并再导出验证器，
不建立第二套正则或清洗逻辑。新增携带 session/thread ID 的 wire 字段时
必须经本表面校验，不得在 codec 内自行拼接规则。
"""

from __future__ import annotations

from app.core.session_catalog_store import validate_session_id, validate_thread_id

__all__ = [
    "CANONICAL_ID_HEX_PAYLOAD_LENGTH",
    "CANONICAL_ID_PREFIX_LENGTH",
    "CANONICAL_ID_TOTAL_LENGTH",
    "CANONICAL_SESSION_ID_PREFIX",
    "CANONICAL_THREAD_ID_PREFIX",
    "validate_session_id",
    "validate_thread_id",
]

# 36-byte ASCII 形态：4 字节前缀（ses_/thr_）+ 32 位小写 hex；
# payload 第 13 个 hex 位为 4（UUIDv4 version），第 17 个 hex 位属于
# 8|9|a|b（UUIDv4 variant）。
CANONICAL_ID_PREFIX_LENGTH = 4
CANONICAL_ID_HEX_PAYLOAD_LENGTH = 32
CANONICAL_ID_TOTAL_LENGTH = 36
CANONICAL_SESSION_ID_PREFIX = "ses_"
CANONICAL_THREAD_ID_PREFIX = "thr_"
