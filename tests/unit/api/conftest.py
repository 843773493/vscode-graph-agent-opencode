from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.deps import (
    get_browser_resource_owner,
    get_terminal_resource_owner,
    verify_local_token,
)
from app.api.resources import router
from app.core.path_utils import workspace_context
from app.core.trace_middleware import TraceMiddleware
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import WORKSPACE_ID
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


def _request_workspace_id(request: Request) -> str:
    workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    if workspace_id != WORKSPACE_ID:
        raise RuntimeError("资源 API 测试请求缺少显式工作区身份")
    return workspace_id


@pytest.fixture
def terminal_owner_bundle(tmp_path: Path) -> TerminalOwnerFixture:
    return build_terminal_owner_fixture(tmp_path)


@pytest.fixture
def browser_owner_bundle(tmp_path: Path) -> BrowserOwnerFixture:
    root = tmp_path / "browser"
    root.mkdir()
    return build_browser_owner_fixture(root)


@pytest.fixture
def resource_api_client(
    terminal_owner_bundle: TerminalOwnerFixture,
    browser_owner_bundle: BrowserOwnerFixture,
) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")

    async def terminal_owner_for_request(request: Request) -> AsyncIterator[object]:
        workspace_id = _request_workspace_id(request)
        with workspace_context(workspace_id, terminal_owner_bundle.root):
            yield terminal_owner_bundle.owner

    async def browser_owner_for_request(request: Request) -> AsyncIterator[object]:
        workspace_id = _request_workspace_id(request)
        if browser_owner_bundle.owner is None:
            raise RuntimeError("Browser owner 测试装具尚未初始化")
        with workspace_context(workspace_id, browser_owner_bundle.root):
            yield browser_owner_bundle.owner

    app.dependency_overrides[get_terminal_resource_owner] = terminal_owner_for_request
    app.dependency_overrides[get_browser_resource_owner] = browser_owner_for_request
    app.dependency_overrides[verify_local_token] = lambda: "authenticated-test"
    try:
        with TestClient(app, headers={"X-BoxTeam-Workspace-Id": WORKSPACE_ID}) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
