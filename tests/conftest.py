from __future__ import annotations

import os
import pytest

from app.core.env import load_project_env

# 加载项目.env配置文件
load_project_env(__file__)

# 为测试填充缺失的API密钥（如果为空）
if not os.environ.get("OPENROUTER_API_KEY"):
    os.environ["OPENROUTER_API_KEY"] = "test-key-placeholder"

from scripts.setup_test_env import setup_test_environment

CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "configs")
TEST_CONFIG_PATH = os.path.join(CONFIGS_DIR, "tests", "default.jsonc")

_test_config_path: str = TEST_CONFIG_PATH


def set_test_config_path(path: str) -> None:
    global _test_config_path
    _test_config_path = path


@pytest.fixture(scope="session")
def workspace_root_path():
    workspace_root = setup_test_environment()
    print(f"pytest 测试工作区路径: {workspace_root}")
    return str(workspace_root)


@pytest.fixture(autouse=True)
def inject_workspace_root(monkeypatch: pytest.MonkeyPatch, workspace_root_path: str):
    monkeypatch.setenv("WORKSPACE_ROOT", workspace_root_path)


def use_config(name: str) -> None:
    global _test_config_path
    _test_config_path = os.path.join(CONFIGS_DIR, "tests", f"{name}.jsonc")


def get_test_config_path() -> str:
    global _test_config_path
    return _test_config_path


@pytest.fixture(autouse=True)
def setup_test_config():
    from app.services.config_service import ConfigService, set_config_path

    config_path = get_test_config_path()
    if os.path.exists(config_path):
        set_config_path(config_path)
    yield
    set_config_path(None)
    global _test_config_path
    _test_config_path = TEST_CONFIG_PATH