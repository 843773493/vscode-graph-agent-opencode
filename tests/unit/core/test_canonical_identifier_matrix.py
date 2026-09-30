"""canonical 标识符全矩阵测试（OpenSpec 2.1 / B1）。

覆盖：合法形态、超长、Unicode、分隔符、点段、百分号编码、非 v7 bits、
大小写错误在落盘前失败（不清洗、不截断、无旧 ID 别名）、完整 path
预算与 locator 校验、identifier factory 与协议层形态常量一致性。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.session_catalog_store import (
    validate_path_budget,
    validate_session_id,
    validate_storage_relative_locator,
    validate_thread_id,
)
from app.core.session_control_store import (
    SessionControlStore,
    validate_thread_relative_locator,
)
from app.protocol.canonical import (
    CANONICAL_ID_HEX_PAYLOAD_LENGTH,
    CANONICAL_ID_PREFIX_LENGTH,
    CANONICAL_ID_TOTAL_LENGTH,
    CANONICAL_SESSION_ID_PREFIX,
    CANONICAL_THREAD_ID_PREFIX,
)
from tests.support.canonical_id_at import session_id_at, thread_id_at


def make_session_id() -> str:
    return f"ses_{create_uuid_hex()}"


def make_thread_id() -> str:
    return f"thr_{create_uuid_hex()}"


# 固定日期桶 locator 的测试时刻：id 内嵌时间 MUST 与桶同日（§4.1/§4.3）。
JUNE_1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LEAP_DAY = datetime(2024, 2, 29, 12, 0, tzinfo=UTC)


def _v7_hex_with(index: int, char: str) -> str:
    """把合法 UUIDv7 payload 的指定 hex 位替换成给定字符。"""
    payload = list(create_uuid_hex())
    payload[index] = char
    return "".join(payload)


# ----------------------------------------------------------------------
# 合法形态与 IdentifierFactory
# ----------------------------------------------------------------------


def test_valid_session_and_thread_ids_pass() -> None:
    validate_session_id(make_session_id())
    validate_thread_id(make_thread_id())


def test_identifier_factories_produce_canonical_profile() -> None:
    """identifier factory 输出必须能通过唯一 canonical 验证器。"""
    validate_session_id("ses_" + create_uuid_hex())
    validate_thread_id("thr_" + create_uuid_hex())


def test_lease_id_factory_profile() -> None:
    lease_id = create_prefixed_id("lease")
    prefix, payload = lease_id.split("_", maxsplit=1)
    assert prefix == "lease"
    assert len(payload) == 32
    assert payload[12] == "7"
    assert payload[16] in "89ab"


# ----------------------------------------------------------------------
# 非法形态矩阵：全部直接拒绝，不清洗不截断
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "ses_",
        create_uuid_hex(),
        "sesx_" + create_uuid_hex(),
        "thread_" + create_uuid_hex(),
        "SES_" + create_uuid_hex(),
        "ses_" + create_uuid_hex().upper(),
        "ses_" + create_uuid_hex()[:31],
        "ses_" + create_uuid_hex() + "a",
        "ses_" + create_uuid_hex()[:31] + "g",
        "ses_" + create_uuid_hex()[:31] + "日",
        "ses_" + create_uuid_hex()[:31] + "/",
        "ses_" + create_uuid_hex()[:31] + "\\",
        "ses_%" + create_uuid_hex()[:31],
        "ses_" + create_uuid_hex()[:31] + " ",
        "ses_" + create_uuid_hex()[:31] + ".",
        "../" + make_session_id(),
        "ses_/" + create_uuid_hex()[:27],
    ],
    ids=[
        "empty",
        "prefix-only",
        "no-prefix",
        "wrong-prefix",
        "other-prefix",
        "uppercase-prefix",
        "uppercase-hex",
        "short-payload",
        "overlong",
        "non-hex-char",
        "unicode-char",
        "slash",
        "backslash",
        "percent-encoding",
        "space",
        "dot",
        "dotdot-traversal",
        "slash-after-prefix",
    ],
)
def test_invalid_session_id_matrix_rejected(bad_id: str) -> None:
    with pytest.raises(ValueError):
        validate_session_id(bad_id)


@pytest.mark.parametrize(
    "bad_id",
    [
        "thr_" + _v7_hex_with(12, "4"),
        "thr_" + _v7_hex_with(12, "6"),
        "thr_" + _v7_hex_with(16, "0"),
        "thr_" + _v7_hex_with(16, "c"),
    ],
    ids=[
        "version-bit-4",
        "version-bit-6",
        "variant-bit-0",
        "variant-bit-c",
    ],
)
def test_invalid_uuid_v7_bit_matrix_rejected(bad_id: str) -> None:
    """非 v7 version/variant bits 直接拒绝（不归一化为 v7）。"""
    with pytest.raises(ValueError):
        validate_thread_id(bad_id)


def test_v4_bit_profile_session_id_rejected() -> None:
    """第 13 个 hex 为 4 的 UUIDv4 位 profile MUST 被拒绝（§3.3 负向）。"""
    with pytest.raises(ValueError):
        validate_session_id("ses_" + _v7_hex_with(12, "4"))
    with pytest.raises(ValueError):
        validate_thread_id("thr_" + _v7_hex_with(12, "4"))


@pytest.mark.parametrize("bad_value", [None, 123, b"ses_abc", ["ses_abc"]])
def test_non_string_input_rejected_with_type_error(bad_value: object) -> None:
    with pytest.raises(TypeError):
        validate_session_id(bad_value)
    with pytest.raises(TypeError):
        validate_thread_id(bad_value)


# ----------------------------------------------------------------------
# 落盘前失败
# ----------------------------------------------------------------------


def test_invalid_thread_id_rejected_before_persistence(tmp_path: Path) -> None:
    """非法 ID 必须在写库之前失败，thread_catalog 保持零行。"""
    store = SessionControlStore(tmp_path / "session-control.sqlite")
    try:
        bad_thread_id = "thr_" + create_uuid_hex().upper()
        with pytest.raises(ValueError):
            store.initialize_main_thread(bad_thread_id, datetime.now(UTC))
        count = store.connection.execute(
            "SELECT COUNT(*) FROM thread_catalog"
        ).fetchone()[0]
        assert count == 0
    finally:
        store.close()


# ----------------------------------------------------------------------
# path 预算与 locator
# ----------------------------------------------------------------------


def test_path_budget_accepts_canonical_locator(tmp_path: Path) -> None:
    locator = "sessions/2026/06/01/" + session_id_at(JUNE_1)
    validate_storage_relative_locator(locator)
    validate_path_budget(tmp_path, locator)


def test_path_budget_rejects_component_over_255_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="路径组件超出预算"):
        validate_path_budget(tmp_path, "a" * 256)


def test_path_budget_accepts_component_at_255_bytes(tmp_path: Path) -> None:
    validate_path_budget(tmp_path, "a" * 255)


def test_path_budget_rejects_total_over_4096_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="路径总长超出预算"):
        validate_path_budget(tmp_path, "a/" * 2100)


def test_path_budget_rejects_unicode_component_over_budget(
    tmp_path: Path,
) -> None:
    # 85 个 3 字节 CJK 字符 = 255 bytes（边界内）；86 个 = 258 bytes 超限。
    validate_path_budget(tmp_path, "日" * 85)
    with pytest.raises(ValueError, match="路径组件超出预算"):
        validate_path_budget(tmp_path, "日" * 86)


@pytest.mark.parametrize(
    "bad_locator",
    [
        "sessions/2026/13/01/" + make_session_id(),
        "sessions/2026/00/01/" + make_session_id(),
        "sessions/2026/02/30/" + make_session_id(),
        "sessions/2023/02/29/" + make_session_id(),
        "sessions/2026/06/01/" + create_uuid_hex(),
        "sessions/2026/06/01/SES_" + create_uuid_hex(),
    ],
    ids=[
        "month-13",
        "month-00",
        "day-30-feb",
        "leap-day-non-leap-year",
        "leaf-not-canonical",
        "leaf-uppercase",
    ],
)
def test_storage_locator_matrix_rejected(bad_locator: str) -> None:
    with pytest.raises(ValueError):
        validate_storage_relative_locator(bad_locator)


def test_storage_locator_leap_day_accepted() -> None:
    validate_storage_relative_locator("sessions/2024/02/29/" + session_id_at(LEAP_DAY))


def test_thread_relative_locator_matrix() -> None:
    validate_thread_relative_locator("threads/2026/06/01/" + thread_id_at(JUNE_1))
    with pytest.raises(ValueError):
        validate_thread_relative_locator(
            "sessions/2026/06/01/" + thread_id_at(JUNE_1)
        )
    with pytest.raises(ValueError):
        validate_thread_relative_locator("threads/2026/13/01/" + thread_id_at(JUNE_1))


def test_storage_locator_date_drift_from_embedded_time_rejected() -> None:
    """§4.2 负向：分桶日期与 session_id 内嵌 48 bit 毫秒 UTC 日期漂移 MUST
    fail-closed，且只凭 locator 字符串判定（不依赖另存 created_at）。"""
    session_id = session_id_at(JUNE_1)
    # 同日桶通过。
    validate_storage_relative_locator(f"sessions/2026/06/01/{session_id}")
    # 相邻日桶、异年同月日桶均为漂移，一律 fail-closed。
    with pytest.raises(ValueError, match="分桶与 id 漂移"):
        validate_storage_relative_locator(f"sessions/2026/06/02/{session_id}")
    with pytest.raises(ValueError, match="分桶与 id 漂移"):
        validate_storage_relative_locator(f"sessions/2025/06/01/{session_id}")


def test_thread_locator_date_drift_from_embedded_time_rejected() -> None:
    """§4.3 负向：thread 分桶日期与 thread_id 内嵌 48 bit 毫秒 UTC 日期漂移
    MUST fail-closed。"""
    thread_id = thread_id_at(JUNE_1)
    validate_thread_relative_locator(f"threads/2026/06/01/{thread_id}")
    with pytest.raises(ValueError, match="分桶与 id 漂移"):
        validate_thread_relative_locator(f"threads/2026/06/02/{thread_id}")
    with pytest.raises(ValueError, match="分桶与 id 漂移"):
        validate_thread_relative_locator(f"threads/2025/06/01/{thread_id}")


# ----------------------------------------------------------------------
# 协议层形态常量一致性
# ----------------------------------------------------------------------


def test_protocol_canonical_constants_match_validator() -> None:
    session_id = make_session_id()
    thread_id = make_thread_id()
    assert len(session_id) == CANONICAL_ID_TOTAL_LENGTH
    assert len(thread_id) == CANONICAL_ID_TOTAL_LENGTH
    assert session_id.startswith(CANONICAL_SESSION_ID_PREFIX)
    assert thread_id.startswith(CANONICAL_THREAD_ID_PREFIX)
    assert len(CANONICAL_SESSION_ID_PREFIX) == CANONICAL_ID_PREFIX_LENGTH
    payload = session_id[CANONICAL_ID_PREFIX_LENGTH:]
    assert len(payload) == CANONICAL_ID_HEX_PAYLOAD_LENGTH
    validate_session_id(session_id)
    validate_thread_id(thread_id)
