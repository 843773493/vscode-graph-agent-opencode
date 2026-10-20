from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.path_utils import SessionCatalogComponents
from tests.support import session_catalog_owner


@pytest.fixture
def catalog_components_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Mock]:
    components: list[Mock] = []

    def create() -> Mock:
        value = Mock(spec=SessionCatalogComponents)
        value.get_catalog_components.return_value = value
        components.append(value)
        return value

    monkeypatch.setattr(
        session_catalog_owner,
        "build_session_catalog_components",
        lambda *_args: components.pop(0),
    )
    return create


@pytest.mark.parametrize("interruption", [KeyboardInterrupt("停止"), SystemExit(3)])
def test_catalog_release_interruption_still_closes_other_components(
    interruption: BaseException,
    catalog_components_factory: Callable[[], Mock],
    tmp_path: Path,
) -> None:
    first = catalog_components_factory()
    second = catalog_components_factory()
    first.close.side_effect = interruption
    owner = session_catalog_owner.FixtureCatalogOwner()
    owner.get_catalog_components(tmp_path / "one", "workspace-one")
    owner.get_catalog_components(tmp_path / "two", "workspace-two")

    failure = None
    try:
        owner.release()
    except BaseException as error:  # noqa: BLE001 - 捕获被测中断，避免中断整轮 pytest。
        failure = error

    second.close.assert_called_once_with()
    assert isinstance(failure, BaseExceptionGroup)
    assert failure.exceptions == (interruption,)
    owner.release()
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()


def test_catalog_release_collects_ordinary_failures_without_repeating_close(
    catalog_components_factory: Callable[[], Mock],
    tmp_path: Path,
) -> None:
    first = catalog_components_factory()
    second = catalog_components_factory()
    first_error = OSError("第一个 catalog 关闭失败")
    second_error = ValueError("第二个 catalog 关闭失败")
    first.close.side_effect = first_error
    second.close.side_effect = second_error
    owner = session_catalog_owner.FixtureCatalogOwner()
    owner.get_catalog_components(tmp_path / "one", "workspace-one")
    owner.get_catalog_components(tmp_path / "two", "workspace-two")

    with pytest.raises(ExceptionGroup) as failure:
        owner.release()

    assert failure.value.exceptions == (first_error, second_error)
    owner.release()
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt("停止"), SystemExit(3)])
def test_catalog_scope_keeps_body_error_when_cleanup_is_interrupted(
    interruption: BaseException,
    catalog_components_factory: Callable[[], Mock],
    tmp_path: Path,
) -> None:
    first = catalog_components_factory()
    second = catalog_components_factory()
    first.close.side_effect = interruption
    body_error = ValueError("fixture 主体失败")

    failure = None
    try:
        with session_catalog_owner.fixture_catalog_scope() as owner:
            owner.get_catalog_components(tmp_path / "one", "workspace-one")
            owner.get_catalog_components(tmp_path / "two", "workspace-two")
            raise body_error
    except BaseException as error:  # noqa: BLE001 - 捕获被测中断，核对两种原始错误。
        failure = error

    second.close.assert_called_once_with()
    assert isinstance(failure, BaseExceptionGroup)
    assert failure.exceptions[0] is body_error
    cleanup_failure = failure.exceptions[1]
    assert isinstance(cleanup_failure, BaseExceptionGroup)
    assert cleanup_failure.exceptions == (interruption,)
