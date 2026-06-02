from __future__ import annotations

import os
import pytest

from app.core.env import load_project_env

# 加载项目.env配置文件
load_project_env(__file__)

# 为测试填充缺失的API密钥（如果为空）
if not os.environ.get("OPENROUTER_API_KEY"):
    os.environ["OPENROUTER_API_KEY"] = "test-key-placeholder"


CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "configs")
TEST_CONFIG_PATH = os.path.join(CONFIGS_DIR, "tests", "default.jsonc")



@pytest.fixture
def test_config_path() -> str:
    return TEST_CONFIG_PATH


def use_config(name: str) -> str:
    return os.path.join(CONFIGS_DIR, "tests", f"{name}.jsonc")


@pytest.fixture(autouse=True)
def setup_test_config(test_config_path: str):
    from app.services.config_service import set_config_path

    if os.path.exists(test_config_path):
        set_config_path(test_config_path)
    yield
    set_config_path(None)
