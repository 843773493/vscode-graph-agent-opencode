from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import commentjson
import pytest
from dotenv import load_dotenv

# 测试进程显式加载仓库环境；产品运行时只读取 BOXTEAM_HOME/config/.env。
load_dotenv(Path.cwd() / ".env", override=False)

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
            return resolver.resolve_session_node(session_id)
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
def setup_test_config(
    test_config_path: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """每个测试通过独立 BOXTEAM_HOME 使用标准 Workspace 配置路径。"""
    boxteam_home = tmp_path_factory.mktemp("boxteam-home")
    config_root = boxteam_home / "config"
    config_root.mkdir(parents=True)
    if os.path.exists(test_config_path):
        payload = commentjson.loads(Path(test_config_path).read_text(encoding="utf-8"))
        payload["$schema"] = "./workspace_schema.jsonc"
        payload["config_version"] = 1
        (config_root / "workspace.jsonc").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (config_root / "workspace_schema.jsonc").write_bytes(
            (Path(CONFIGS_DIR) / "workspace_schema.jsonc").read_bytes()
        )
        (config_root / "gateway.jsonc").write_bytes(
            (Path(CONFIGS_DIR) / "gateway_inline.jsonc").read_bytes()
        )
        (config_root / "gateway_schema.jsonc").write_bytes(
            (Path(CONFIGS_DIR) / "gateway_schema.jsonc").read_bytes()
        )
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
