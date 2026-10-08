from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def workspace_scope(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path]]:
    """需要 workspace 上下文的单测显式启用；不给无归属调用补默认值。"""
    root = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    prepare_default_test_workspace(
        workspace_root=root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
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
