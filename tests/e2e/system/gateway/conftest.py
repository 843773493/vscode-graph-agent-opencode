from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support.workspaces import prepare_default_test_workspace

if TYPE_CHECKING:
    from tests.e2e.system.gateway.gateway_target import GatewayTargetE2EPaths


@pytest.fixture(scope="module")
def docker_e2e_paths(request: pytest.FixtureRequest) -> GatewayTargetE2EPaths:
    from tests.e2e.system.gateway.gateway_target import GatewayTargetE2EPaths

    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    root = project_root / "out" / "tests" / relative_test_path
    local_workspace = root / "workspace"
    artifacts = root / "artifacts"

    if root.exists():
        shutil.rmtree(root)
    prepare_default_test_workspace(
        workspace_root=local_workspace,
        template_root=project_root
        / "tests"
        / "fixtures"
        / "workspaces"
        / "default_test_workspace",
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    container_test_root = (
        f"/opt/boxteam-dev/repository/out/tests/{relative_test_path.as_posix()}"
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
def keep_docker_runtime(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--keep-docker-runtime"))
