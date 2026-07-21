from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.e2e.gateway.gateway_docker import (
    docker_daemon_error,
    ensure_gateway_ssh_container,
)
from tests.e2e.gateway.gateway_target import GatewaySshTarget, GatewayTargetE2EPaths


@pytest.fixture(scope="module")
def docker_e2e_paths(request: pytest.FixtureRequest) -> GatewayTargetE2EPaths:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    root = project_root / "out" / "tests" / relative_test_path
    local_workspace = root / "workspace"
    artifacts = root / "artifacts"

    if root.exists():
        shutil.rmtree(root)
    _copy_default_workspace(local_workspace)
    artifacts.mkdir(parents=True, exist_ok=True)
    container_test_root = (
        "/opt/boxteam-dev/repository/out/tests/"
        f"{relative_test_path.as_posix()}"
    )
    container_remote_workspace = f"{container_test_root}/workspace"
    container_boxteam_home = f"{container_test_root}/artifacts/home"
    return GatewayTargetE2EPaths(
        root=root,
        local_workspace=local_workspace,
        artifacts=artifacts,
        remote_workspace=container_remote_workspace,
        remote_boxteam_home=container_boxteam_home,
    )


@pytest.fixture(scope="module")
def e2e_workspace_root_path(docker_e2e_paths: GatewayTargetE2EPaths) -> str:
    return str(docker_e2e_paths.local_workspace)


@pytest.fixture
def gateway_docker_target(
    docker_e2e_paths: GatewayTargetE2EPaths,
) -> GatewaySshTarget:
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH E2E")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")
    return ensure_gateway_ssh_container(
        known_hosts_path=docker_e2e_paths.artifacts / "gateway_ssh_known_hosts"
    )


@pytest.fixture
def keep_docker_runtime(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--keep-docker-runtime"))


def _copy_default_workspace(target: Path) -> None:
    source = Path.cwd().resolve() / "asset" / "default_test_workspace"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
