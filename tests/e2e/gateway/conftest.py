from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.gateway.gateway_docker import (
    CONTAINER_PROJECT_PATH,
    GatewaySshDockerTarget,
    docker_daemon_error,
    ensure_gateway_ssh_container,
)


@dataclass(frozen=True, slots=True)
class DockerE2EPaths:
    root: Path
    local_workspace: Path
    remote_workspace: Path
    artifacts: Path
    container_remote_workspace: str


@pytest.fixture(scope="module")
def docker_e2e_paths(request: pytest.FixtureRequest) -> DockerE2EPaths:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    root = project_root / "out" / "docker" / relative_test_path
    local_workspace = root / "workspace"
    remote_workspace = root / "remote-workspace"
    artifacts = root / "artifacts"

    if root.exists():
        shutil.rmtree(root)
    _copy_default_workspace(local_workspace)
    _copy_default_workspace(remote_workspace)
    artifacts.mkdir(parents=True, exist_ok=True)
    container_remote_workspace = (
        f"{CONTAINER_PROJECT_PATH}/"
        f"{remote_workspace.relative_to(project_root).as_posix()}"
    )
    return DockerE2EPaths(
        root=root,
        local_workspace=local_workspace,
        remote_workspace=remote_workspace,
        artifacts=artifacts,
        container_remote_workspace=container_remote_workspace,
    )


@pytest.fixture(scope="module")
def e2e_workspace_root_path(docker_e2e_paths: DockerE2EPaths) -> str:
    return str(docker_e2e_paths.local_workspace)


@pytest.fixture
def gateway_docker_target(
    docker_e2e_paths: DockerE2EPaths,
) -> GatewaySshDockerTarget:
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
