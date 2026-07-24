from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable

import pytest

from app.core.env import load_project_env

# 加载项目.env配置文件
load_project_env(Path.cwd())

# 为测试填充缺失的API密钥（如果为空）
if not os.environ.get("OPENROUTER_API_KEY"):
    os.environ["OPENROUTER_API_KEY"] = "test-key-placeholder"


CONFIGS_DIR = os.path.join(Path.cwd(), "configs")
TEST_CONFIG_PATH = os.path.join(CONFIGS_DIR, "tests", "default.jsonc")



@pytest.fixture
def test_config_path() -> str:
    return TEST_CONFIG_PATH


def use_config(name: str) -> str:
    return os.path.join(CONFIGS_DIR, "tests", f"{name}.jsonc")


@pytest.fixture
def session_bundle_factory() -> Callable[[Path, str], Path]:
    """在显式 sessions 根目录中创建最小合法会话 bundle。"""
    from app.core.path_utils import get_session_path_resolver

    def create(sessions_root: Path, session_id: str) -> Path:
        resolver = get_session_path_resolver(sessions_root)
        resolver.initialize()
        try:
            return resolver.resolve_session_dir(session_id)
        except KeyError:
            pass
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title=f"测试会话 {session_id}",
        )
        now = datetime.now(UTC).isoformat()
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": f"测试会话 {session_id}",
                    "created_at": now,
                    "updated_at": now,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)
        return session_dir

    return create


@pytest.fixture(autouse=True)
def setup_test_config(test_config_path: str):
    from app.services.infrastructure.config_service import set_config_path

    if os.path.exists(test_config_path):
        set_config_path(test_config_path)
    yield
    set_config_path(None)
