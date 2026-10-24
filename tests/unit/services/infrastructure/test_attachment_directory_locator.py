"""目录 locator 使用冻结 UTC 日期和不透明 ID，不推测内容或上传时间。"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    resolve_attachment_directory,
    validate_attachment_directory_relative_locator,
)

ATTACHMENT_ID = "att_" + "a" * 32


def test_directory_locator_uses_explicit_utc_date_and_preserves_legacy_identity(tmp_path: Path) -> None:
    stamp = datetime(2024, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=14)))
    locator = attachment_directory_relative_locator(ATTACHMENT_ID, stamp)
    assert locator == f"2024/02/29/{ATTACHMENT_ID}"
    root = tmp_path / "附件目录"
    assert resolve_attachment_directory(root, locator) == root / locator
    assert not root.exists()
    assert attachment_directory_relative_locator(ATTACHMENT_ID, stamp.astimezone(UTC)) == locator


def test_early_year_is_zero_padded() -> None:
    assert attachment_directory_relative_locator(ATTACHMENT_ID, datetime(1, 1, 1, tzinfo=UTC)) == f"0001/01/01/{ATTACHMENT_ID}"


@pytest.mark.parametrize("attachment_id", ["", "att_" + "A" * 32, "att_" + "a" * 31, "../" + ATTACHMENT_ID, ATTACHMENT_ID + "/derived"])
def test_directory_identity_is_never_cleaned_or_reinterpreted(attachment_id: str) -> None:
    with pytest.raises(ValueError):
        attachment_directory_relative_locator(attachment_id, datetime.now(UTC))


def test_directory_identity_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        attachment_directory_relative_locator(None, datetime.now(UTC))  # type: ignore[arg-type]


def test_directory_requires_explicit_creation_timezone() -> None:
    with pytest.raises(ValueError, match="显式时区"):
        attachment_directory_relative_locator(ATTACHMENT_ID, datetime(2024, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize("prefix", ["2023/02/29", "2024/02/30", "0000/01/01", "2024/13/01", "2024/01/00", "2024/1/01", "２０２４/01/01", "../2024/01/01", "/2024/01/01", "2024\\01\\01"])
def test_directory_locator_rejects_invalid_dates_and_noncanonical_paths(prefix: str) -> None:
    with pytest.raises(ValueError):
        validate_attachment_directory_relative_locator(f"{prefix}/{ATTACHMENT_ID}")


def test_directory_locator_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        validate_attachment_directory_relative_locator(None)  # type: ignore[arg-type]


def test_resolver_checks_full_path_budget_without_truncation(tmp_path: Path) -> None:
    root = tmp_path / ("中" * 86)
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="组件超出预算"):
        resolve_attachment_directory(root, f"2024/01/01/{ATTACHMENT_ID}")
    assert list(tmp_path.iterdir()) == before
