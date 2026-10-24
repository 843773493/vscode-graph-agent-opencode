from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import httpx
import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from tests.harness.python.run_context import TestRunContext
from tests.support.communication import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    CommunicationProbe,
)
from tests.support.workspaces import (
    prepare_default_test_workspace,
    prepare_test_workspace,
)


@pytest.fixture
def workspace_scope(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path]]:
    """需要 workspace 上下文的单测显式启用；不给无归属调用补默认值。"""
    yield from _workspace_scope(request, template_items=None)


@pytest.fixture
def empty_workspace_scope(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, Path]]:
    """创建带 identity/context 的空 workspace，不复制模板项目文件。"""
    yield from _workspace_scope(request, template_items=())


def _workspace_scope(
    request: pytest.FixtureRequest,
    *,
    template_items: tuple[str, ...] | None,
) -> Iterator[tuple[str, Path]]:
    root = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    if template_items is None:
        prepare_default_test_workspace(
            workspace_root=root,
            template_root=template_root,
        )
    else:
        prepare_test_workspace(
            workspace_root=root,
            template_root=template_root,
            template_items=template_items,
        )
    workspace_id = load_or_create_workspace_id(root)
    with workspace_context(workspace_id, root):
        yield workspace_id, root


class _TestSessionLifecycleGuard:
    def __init__(self) -> None:
        self.checked_session_ids: list[str] = []

    def __call__(self, session_id: str) -> None:
        prefix, separator, identity = session_id.partition("_")
        if prefix not in {"ses", "session"} or not separator or not identity:
            raise AssertionError(f"测试传入了无效的伪 Session identity: {session_id!r}")
        self.checked_session_ids.append(session_id)


@pytest.fixture
def session_lifecycle_guard() -> _TestSessionLifecycleGuard:
    """校验并记录单元测试使用的伪 Session identity。"""
    return _TestSessionLifecycleGuard()


@pytest.fixture
def communication_probe() -> CommunicationProbe:
    return CommunicationProbe()


@pytest.fixture
def communication_invocation() -> Iterator[ToolInvocationContext]:
    context = ToolInvocationContext(thread_binding=ThreadRuntimeBinding(SOURCE_SESSION, SOURCE_THREAD))
    token = context.set_tool_call_id("call-communication-test", projection_identity=ItemProjectionIdentity(model_call_id="019c390d-6a70-7c85-8709-afddc785bf93"))
    yield context
    context.reset_tool_call_id(token)


@pytest.fixture
def resource_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """两类资源客户端共用正式 HTTPX 编解码，仅替换单次网络边界。"""
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.services.infrastructure.terminal_manager_client.AsyncClient", client)
        monkeypatch.setattr("app.services.infrastructure.browser_manager_client.AsyncClient", client)

    return install
