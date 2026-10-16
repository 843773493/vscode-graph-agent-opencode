from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import commentjson
import pytest
from dotenv import load_dotenv

from tests.harness.python.run_context import TestRunContext
from tests.support.process_scope import capture_test_processes
from tests.support.session_catalog_owner import (
    FixtureCatalogOwner,
    fixture_catalog_scope,
)

# 测试进程显式加载仓库环境；产品运行时只读取 BOXTEAM_HOME/config/.env。
load_dotenv(Path.cwd() / ".env", override=False)

# 为测试填充缺失的API密钥（如果为空）
if not os.environ.get("OPENROUTER_API_KEY"):
    os.environ["OPENROUTER_API_KEY"] = "test-key-placeholder"


CONFIGS_DIR = os.path.join(Path.cwd(), "configs")
TEST_CONFIG_PATH = os.path.join(CONFIGS_DIR, "tests", "workspace", "default.jsonc")


@pytest.fixture
def test_config_path() -> str:
    return TEST_CONFIG_PATH


@pytest.fixture
def frozen_provider_wire() -> Callable[[str, str], dict[str, object]]:
    """为不发送 HTTP 的 Saver 用例显式提供冻结 profile 与语义参数。"""
    from tests.support.frozen_provider import frozen_provider_wire

    return frozen_provider_wire
@pytest.fixture
def frozen_provider_activation() -> Callable[..., dict[str, object]]:
    """正式 prepare 的显式冻结前置；只消费测试已建立的真实 admission。"""
    from tests.support.frozen_provider import create_frozen_provider_activation

    return create_frozen_provider_activation()


@pytest.fixture
def frozen_provider_prepare(frozen_provider_wire, frozen_provider_activation):
    """历史 fixture 经真实封存和 ModelCall 登记激活当前 admission，不发送 Provider 请求。"""
    from tests.support.frozen_provider import create_frozen_provider_prepare

    return create_frozen_provider_prepare(frozen_provider_wire, frozen_provider_activation)


@pytest.fixture
def registered_tool_set_producer_factory(tmp_path):
    """按测试运行目录建立真实 selection owner，schema 仍由正式 producer 冻结。"""
    from app.agents.policy import ToolMetadata, ToolPolicyResolver
    from app.agents.tool_identity import tool_definition_name
    from app.agents.tool_set_producer import RegisteredToolSetProducer
    from app.services.infrastructure.tool_selection_store import ToolSelectionStore

    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    def create(tools=()):
        return RegisteredToolSetProducer(
            agent_id="dispatch-test", selection_reader=store,
            policy_resolver=ToolPolicyResolver(
                policy_defaults={}, policy_rules={}, restrictions={},
            ),
            tools=tools, metadata={tool_definition_name(tool): ToolMetadata(
                tool_id=tool_definition_name(tool), origin="builtin", kind="default",
                group_id="filesystem",
            ) for tool in tools},
        )

    return create


def use_config(name: str) -> str:
    return os.path.join(CONFIGS_DIR, "tests", "workspace", f"{name}.jsonc")


@pytest.fixture(autouse=True)
def test_process_lifetime() -> Iterator[None]:
    with capture_test_processes():
        yield


@pytest.fixture(autouse=True)
def session_catalog_owner(
    test_process_lifetime: None,
) -> Iterator[FixtureCatalogOwner]:
    """standalone helper 显式委派本用例 owner；生产 registry scope 会覆盖它。"""
    del test_process_lifetime
    with fixture_catalog_scope() as owner:
        yield owner


@pytest.fixture
def session_bundle_factory() -> Callable[[Path, str], Path]:
    """在显式 sessions 根目录中创建最小合法会话 bundle。"""
    from tests.support.catalog_session_bundle import seed_catalog_session_bundle

    def create(sessions_root: Path, session_id: str) -> Path:
        return seed_catalog_session_bundle(sessions_root, session_id).directory

    return create


@pytest.fixture(autouse=True)
def setup_test_config(
    test_config_path: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    test_process_lifetime: None,
) -> None:
    """每个测试通过独立 BOXTEAM_HOME 使用标准 Workspace 配置路径。"""
    del test_process_lifetime
    run_context = TestRunContext.from_test_file(Path(request.node.path))
    boxteam_home = run_context.boxteam_home_for_node(request.node.nodeid)
    if boxteam_home.exists():
        shutil.rmtree(boxteam_home)
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


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    """把 pytest 临时文件隔离到当前正式测试文件的输出目录。"""
    run_context = TestRunContext.from_test_file(Path(request.node.path))
    temp_root = run_context.runtime_root_for_node(request.node.nodeid) / "tmp"
    if temp_root.exists():
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True)
    return temp_root


@pytest.fixture(scope="session", autouse=True)
def runtime_manifest_path() -> Iterator[Path]:
    """构造稳定的 runtime manifest，供 distribution_id 推导（inline scope_id）。

    路径 MUST NOT 落在 pytest 的 basetemp（``/tmp/pytest-of-<$USER>/pytest-<N>``）：
    本仓库 ``tmp_path_retention_policy = "none"``，任何一次会话退出都会以 ``keep=0``
    注册的 atexit 清理调用 ``cleanup_candidates``，把**共享 basetemp 根目录下的每一个
    ``pytest-<N>`` 都收进候选并 rmtree**——包括仍在运行的别的会话所用的那个。实测：
    嵌套或并发的第二个 pytest 会话一退出，第一个会话的 basetemp（含此处 manifest）即被
    删除，其后触达 config 来源构造的用例全部撞上 ``load_distribution_id`` 的 fail-closed。
    故 manifest MUST 写入确定性、不属于任何 basetemp 的位置：本仓库 ``out/`` 已在
    ``.gitignore`` 中，且没有任何测试清理 ``out/tests/`` 本身（各用例只清各自的
    ``out/tests/<镜像路径>/`` 叶子目录）。
    """
    manifest = Path.cwd() / "out" / "tests" / "runtime-manifest" / "runtime-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "distribution": "source-development",
                "version": "0.0.2",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # 会话级注入先于模块级服务启动；个别用例仍可显式移除以验证缺失配置。
    previous_manifest = os.environ.get("BOXTEAM_RUNTIME_MANIFEST")
    os.environ["BOXTEAM_RUNTIME_MANIFEST"] = str(manifest)
    try:
        yield manifest
    finally:
        if previous_manifest is None:
            os.environ.pop("BOXTEAM_RUNTIME_MANIFEST", None)
        else:
            os.environ["BOXTEAM_RUNTIME_MANIFEST"] = previous_manifest
