from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.gateway.plugins.packages import PluginPackageStore
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import build_developer_tools_package


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--prepared-debug-plugin-package",
        default=None,
        help="已安装并准备依赖的 Debug 插件包目录",
    )


@pytest.fixture(scope="session")
def prepared_debug_plugin_package(request: pytest.FixtureRequest) -> Iterator[Path]:
    """安装一次测试包依赖，供隔离 Gateway Home 复制；不验证产品安装入口。"""
    prepared_root = request.config.getoption("--prepared-debug-plugin-package")
    if prepared_root:
        root = Path(prepared_root).resolve(strict=True)
        installed = PluginPackageStore(root.parent.parent).get("boxteam.developer-tools", "0.1.0")
        if installed.root != root:
            raise ValueError("预备 Debug 插件包身份不匹配")
        manifest_source = Path("resources/plugins/developer-tools")
        code_source = Path.cwd()
        current_manifest = json.loads((manifest_source / "plugin.jsonc").read_text(encoding="utf-8"))
        prepared_manifest = json.loads((root / "plugin.jsonc").read_text(encoding="utf-8"))
        current_debug = next(plugin for plugin in current_manifest["plugins"] if plugin["id"] == "debugging")
        prepared_debug = next(plugin for plugin in prepared_manifest["plugins"] if plugin["id"] == "debugging")
        if current_debug != prepared_debug:
            raise ValueError("预备 Debug 插件 manifest 与当前源码不一致")
        for relative in ("skills/debugging/SKILL.md",):
            if hashlib.sha256((root / relative).read_bytes()).digest() != hashlib.sha256(
                (manifest_source / relative).read_bytes()
            ).digest():
                raise ValueError(f"预备 Debug 插件文件与当前源码不一致: {relative}")
        for relative in (
            "src/shared/serverPluginRuntime.mjs",
            "src/shared/serverRuntimeLogging.mjs",
            "src/workspace-services/debug/server/backend.js",
        ):
            if hashlib.sha256((root / relative).read_bytes()).digest() != hashlib.sha256(
                (code_source / relative).read_bytes()
            ).digest():
                raise ValueError(f"预备 Debug 插件文件与当前源码不一致: {relative}")
        if not (root / "node_modules/node-pty/build/Release/pty.node").is_file():
            raise ValueError("预备 Debug 插件缺少已构建的 node-pty")
        yield root
        return

    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("Debug 插件测试需要 PATH 中存在 node")
    context = TestRunContext.from_test_file(
        Path("tests/integration/backend/agents/test_debug_tools.py")
    ).prepare()
    artifacts_dir = context.run_root / "artifacts" / "prepared-debug-plugin"
    runtime_dir = context.run_root / "runtime" / "prepared-debug-plugin"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    archive = artifacts_dir / "developer-tools.zip"
    build_developer_tools_package(Path.cwd().resolve(), archive)
    previous_node_bin = os.environ.get("BOXTEAM_NODE_BIN")
    os.environ["BOXTEAM_NODE_BIN"] = node_bin
    try:
        store = PluginPackageStore(runtime_dir / "boxteam-home" / "plugins" / "packages")
        installed = store.install_zip(archive)
    finally:
        if previous_node_bin is None:
            os.environ.pop("BOXTEAM_NODE_BIN", None)
        else:
            os.environ["BOXTEAM_NODE_BIN"] = previous_node_bin
    yield installed.root
