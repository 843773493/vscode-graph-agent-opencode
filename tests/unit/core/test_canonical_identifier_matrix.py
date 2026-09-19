"""canonical 标识符全矩阵测试（OpenSpec 2.1 / B1）。

覆盖：合法形态、超长、Unicode、分隔符、点段、百分号编码、非 v4 bits、
大小写错误在落盘前失败（不清洗、不截断、无旧 ID 别名）、完整 path
预算与 locator 校验、identifier factory 与协议层形态常量一致性。
"""

from __future__ import annotations

import uuid
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


def make_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


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
    assert uuid.UUID(hex=payload).version == 4


# ----------------------------------------------------------------------
# 非法形态矩阵：全部直接拒绝，不清洗不截断
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "ses_",
        uuid.uuid4().hex,
        "sesx_" + uuid.uuid4().hex,
        "thread_" + uuid.uuid4().hex,
        "SES_" + uuid.uuid4().hex,
        "ses_" + uuid.uuid4().hex.upper(),
        "ses_" + uuid.uuid4().hex[:31],
        "ses_" + uuid.uuid4().hex + "a",
        "ses_" + uuid.uuid4().hex[:31] + "g",
        "ses_" + uuid.uuid4().hex[:31] + "日",
        "ses_" + uuid.uuid4().hex[:31] + "/",
        "ses_" + uuid.uuid4().hex[:31] + "\\",
        "ses_%" + uuid.uuid4().hex[:31],
        "ses_" + uuid.uuid4().hex[:31] + " ",
        "ses_" + uuid.uuid4().hex[:31] + ".",
        "../" + make_session_id(),
        "ses_/" + uuid.uuid4().hex[:27],
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
        "thr_" + uuid.uuid4().hex[:12] + "5" + uuid.uuid4().hex[13:],
        "thr_" + uuid.uuid4().hex[:16] + "0" + uuid.uuid4().hex[17:],
        "thr_" + uuid.uuid4().hex[:16] + "c" + uuid.uuid4().hex[17:],
        "thr_" + uuid.uuid4().hex[:12] + "7" + uuid.uuid4().hex[13:],
    ],
    ids=[
        "version-bit-5",
        "variant-bit-0",
        "variant-bit-c",
        "version-bit-7",
    ],
)
def test_invalid_uuid_v4_bit_matrix_rejected(bad_id: str) -> None:
    """非 v4 version/variant bits 直接拒绝（不归一化为 v4）。"""
    with pytest.raises(ValueError):
        validate_thread_id(bad_id)


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
        bad_thread_id = "thr_" + uuid.uuid4().hex.upper()
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
    locator = "sessions/2026/06/01/" + make_session_id()
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
        "sessions/2026/06/01/" + uuid.uuid4().hex,
        "sessions/2026/06/01/SES_" + uuid.uuid4().hex,
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
    validate_storage_relative_locator("sessions/2024/02/29/" + make_session_id())


def test_thread_relative_locator_matrix() -> None:
    validate_thread_relative_locator("threads/2026/06/01/" + make_thread_id())
    with pytest.raises(ValueError):
        validate_thread_relative_locator(
            "sessions/2026/06/01/" + make_thread_id()
        )
    with pytest.raises(ValueError):
        validate_thread_relative_locator("threads/2026/13/01/" + make_thread_id())


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
