"""已安装 SDK 的轻量入口及产品 wire 导入归属合同。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def sdk_import_process(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("BOXTEAM_", "PYTHON"))}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def run(script: str) -> None:
        result = subprocess.run([sys.executable, "-c", script], cwd=Path.cwd(),
                                env=environment, capture_output=True, text=True, timeout=20, check=False)
        (context.artifacts_dir / f"{request.node.name}.stdout.log").write_text(result.stdout)
        (context.artifacts_dir / f"{request.node.name}.stderr.log").write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr

    return run


@pytest.mark.parametrize("in_event_loop", [False, True])
def test_light_sdk_imports_ignore_invalid_runtime_configuration(sdk_import_process, in_event_loop: bool) -> None:
    probe = '''
import importlib, os, sys
os.environ["BOXTEAM_PLUGIN_INSTANCE"] = "invalid-json"
os.environ["BOXTEAM_RUNTIME_LOG_SINK"] = "invalid-json"
os.environ["BOXTEAM_RUNTIME_LOGGING"] = "invalid-json"
for name in ("boxteam_plugin_sdk", "boxteam_plugin_sdk.lifecycle", "boxteam_plugin_sdk.maintenance"):
    module = importlib.import_module(name)
    assert "/site-packages/" in module.__file__.replace("\\\\", "/"), module.__file__
assert not any(name in sys.modules for name in ("fastapi", "psutil", "boxteam_plugin_sdk.commands", "boxteam_plugin_sdk.observation"))
from boxteam_plugin_sdk.commands import plugin_command_router
async def shutdown():
    pass
try:
    plugin_command_router({}, shutdown=shutdown)
except (ValueError, RuntimeError):
    pass
else:
    raise AssertionError("显式初始化必须拒绝非法实例配置")
'''
    if in_event_loop:
        probe = "import asyncio\nasync def main():\n" + "\n".join("    " + line for line in probe.splitlines()) + "\nasyncio.run(main())\n"
    sdk_import_process(probe)


@pytest.mark.parametrize("sdk_first", [False, True])
def test_product_and_sdk_share_installed_wire_in_either_import_order(sdk_import_process, sdk_first: bool) -> None:
    sdk = "import boxteam_plugin_sdk.commands\nfrom boxteam.browser.v1 import browser_pb2 as sdk_wire\n"
    product = "from app.protocol.codecs.browser import browser_pb2 as product_wire\n"
    sdk_import_process((sdk + product if sdk_first else product + sdk) + '''
from google.protobuf import descriptor_pool
assert sdk_wire is product_wire
assert sdk_wire.DESCRIPTOR is descriptor_pool.Default().FindFileByName("boxteam/browser/v1/browser.proto")
assert "/site-packages/" in sdk_wire.__file__.replace("\\\\", "/"), sdk_wire.__file__
page = product_wire.BrowserPage(browser_id="browser-probe", page_id="page-probe", workspace_id="workspace-probe")
assert sdk_wire.BrowserPage.FromString(page.SerializeToString()) == page
print(sdk_wire.__file__)
''')
