from collections.abc import Sequence

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    del config
    for item in items:
        item.add_marker(pytest.mark.contract)
