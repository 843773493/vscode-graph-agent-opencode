"""在无外网和开发工具的独立容器内复用完整发行物插件验收。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux Docker 隔离运行环境")


@pytest.fixture
def offline_distribution_inputs(request: pytest.FixtureRequest) -> tuple[TestRunContext, Path, Path, Path]:
    asset_input = os.environ.get("BOXTEAM_TEST_RUNTIME_ASSET")
    launcher_input = os.environ.get("BOXTEAM_TEST_PREPARED_LAUNCHER")
    if not asset_input or not launcher_input:
        pytest.skip("需要已构建的 BOXTEAM_TEST_RUNTIME_ASSET 与已安装依赖的 BOXTEAM_TEST_PREPARED_LAUNCHER")
    if shutil.which("docker") is None:
        pytest.skip("需要可用 Docker；不回退到宿主机验收")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    bootstrap = runtime / "bootstrap"
    bootstrap.mkdir(parents=True)
    asset = Path(asset_input).resolve(strict=True)
    with tarfile.open(asset) as archive:
        archive.extractall(bootstrap, filter="data")
    launcher = Path(launcher_input).resolve(strict=True)
    assert (launcher / "bin/boxteam.mjs").is_file()
    return context, asset, launcher, bootstrap / "package"


def test_offline_launcher_completes_service_plugin_lifecycle(offline_distribution_inputs) -> None:
    context, asset, launcher, bootstrap = offline_distribution_inputs
    project = Path.cwd().resolve()
    # 外层 pytest 持有共享集成闸门；内层只加载原发行测试自身的 fixture。
    # 容器仅挂测试输入和发行物，不挂开发源码、开发 Home 或依赖缓存。
    inner_test = "tests/integration/packaging/test_service_plugin_distribution.py"
    inner_output = project / "out/tests/integration/packaging/test_service_plugin_distribution"
    inner_output.mkdir(parents=True, exist_ok=True)
    command = ["docker", "create", "--name", f"boxteam-plugin-offline-{uuid4().hex}",
        # Browser 服务和操作 Workbench 的 Chromium 同时运行，线程也计入 PID 限额。
        "--pull", "never", "--network", "none", "--init", "--memory", "4g", "--pids-limit", "512",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--ulimit", "data=4294967296:4294967296", "--workdir", "/verification"]
    for source, target, writable in (
        (project / "tests", "/verification/tests", False),
        (project / "configs/tests", "/verification/configs/tests", False),
        (asset, "/inputs/runtime.tgz", False),
        (launcher, "/inputs/launcher", False),
        (bootstrap, "/bootstrap", False),
        (inner_output, "/verification/out/tests/integration/packaging/test_service_plugin_distribution", True),
        (context.artifacts_dir, "/isolation", True),
    ):
        command.extend(["--mount", f"type=bind,source={source},target={target}" + ("" if writable else ",readonly")])
    electron_asset = os.environ.get("BOXTEAM_TEST_ELECTRON_ASSET")
    if electron_asset:
        command.extend(["--mount", f"type=bind,source={Path(electron_asset).resolve(strict=True)},target=/inputs/electron.tgz,readonly",
            "--env", "BOXTEAM_TEST_ELECTRON_ASSET=/inputs/electron.tgz"])
    for name, value in {
        "PYTHONPATH": "/verification:/bootstrap/application",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "BOXTEAM_TEST_RUNTIME_ASSET": "/inputs/runtime.tgz",
        "BOXTEAM_TEST_PREPARED_LAUNCHER": "/inputs/launcher",
        "BOXTEAM_TEST_DISTRIBUTION_WORKBENCH": os.environ.get("BOXTEAM_TEST_DISTRIBUTION_WORKBENCH", "0"),
        "BOXTEAM_TEST_RUN_ID": f"offline-launcher-{uuid4().hex}",
    }.items():
        command.extend(["--env", f"{name}={value}"])
    prelude = """
import json, shutil, socket, sys
from pathlib import Path
import pytest
interfaces = [name for _, name in socket.if_nameindex()]
tools = {name: shutil.which(name) for name in ('node', 'python', 'python3', 'bun', 'uv', 'gcc', 'cc', 'make')}
Path('/isolation/environment.json').write_text(json.dumps({'interfaces': interfaces, 'tools': tools}, indent=2) + '\\n')
assert interfaces == ['lo'], interfaces
assert not any(tools.values()), tools
sys.exit(pytest.main(sys.argv[1:]))
"""
    image = os.environ.get("BOXTEAM_TEST_OFFLINE_IMAGE", "ubuntu:24.04")
    command.extend([image, "/bootstrap/python/bin/python3.12", "-c", prelude,
        "--confcutdir=/verification/tests/integration/packaging", "--rootdir=/verification",
        "--import-mode=importlib", "-p", "no:cacheprovider", "-q", "-x", inner_test])
    (context.artifacts_dir / "command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    created = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    container = created.stdout.strip()
    try:
        with (context.artifacts_dir / "container.stdout.raw").open("w") as stdout, (
            context.artifacts_dir / "container.stderr.raw"
        ).open("w") as stderr:
            result = subprocess.run(["docker", "start", "--attach", container],
                stdout=stdout, stderr=stderr, check=False, timeout=480)
        assert result.returncode == 0, f"断网发行验收失败，见 {context.artifacts_dir}"
        context.write_result("passed", {"image": image, "network": "none", "entrypoint": "launcher"})
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True, check=True, timeout=30)
