from pathlib import Path

import pytest


@pytest.fixture
def workspace_root(empty_workspace_scope: tuple[str, Path]) -> Path:
    """提供当前测试对应的空隔离工作区根目录。"""
    return empty_workspace_scope[1]
