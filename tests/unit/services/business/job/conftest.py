"""Job 控制单测显式注入端口，不构造第二套生产存储。"""
import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from tests.support.workspaces import prepare_test_workspace
from tests.unit.services.business.job.ports import (
    job_service_factory as job_service_factory,  # noqa: PLC0414
)


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
    """仅为 Job 显式端口单测校验伪 identity；真实 catalog 由集成装具保护。"""
    return _TestSessionLifecycleGuard()


@pytest.fixture
def empty_workspace_scope(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """排空入口显式绑定本用例的工作区身份，不借用宿主默认工作区。"""
    workspace_root = tmp_path / "workspace"
    prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    workspace_id = load_or_create_workspace_id(workspace_root)
    with workspace_context(workspace_id, workspace_root):
        yield workspace_id, workspace_root


@pytest.fixture
def boundary_executor():
    class Executor:
        def __init__(self):
            self.started = asyncio.Event()
            self.finish_first = asyncio.Event()
            self.first_finished = asyncio.Event()
            self.continued = asyncio.Event()
            self.finish_job = asyncio.Event()
            self.runtimes = []

        async def run(self, job):
            self.runtimes.append(job)
            if len(self.runtimes) == 1:
                async with job.execution_pause.boundary():
                    self.started.set()
                    await self.finish_first.wait()
                self.first_finished.set()
                async with job.execution_pause.boundary():
                    self.continued.set()
                    await self.finish_job.wait()
            return "done"

    return Executor()


@pytest.fixture
def terminal_writer():
    return Mock(mark_job_terminal_status=Mock(return_value=True))
