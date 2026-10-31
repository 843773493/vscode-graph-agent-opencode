"""创建测试沿正式 owner 入口准入；仅原生存储边界设置屏障。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.abstractions.workspace_resources import ResourceSnapshot
from app.services.business.browser_resources.operations import BrowserOperations
from app.services.business.terminal_resources.operations import TerminalOperations
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    resource_owner as resource_owner,  # noqa: PLC0414
)

Operation = TerminalOperations | BrowserOperations


@pytest.fixture
async def operation(resource_owner: OwnerFixture) -> AsyncIterator[Operation]:
    owner = resource_owner.owner
    assert owner is not None
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="turn:creation", operation_id="create:original", created_by_turn_id="origin-turn",
    ) as instance:
        yield instance


async def create(operation: Operation) -> ResourceSnapshot:
    if isinstance(operation, TerminalOperations):
        return await operation.create_terminal(retention_scope="thread", title="原创建")
    return await operation.create_browser(url="about:blank", title="原创建")


@dataclass
class CreationWrite:
    phase: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    failure: OSError | None = None
    writers: list[int] = field(default_factory=list)


@pytest.fixture
async def creation_write(
    resource_owner: OwnerFixture, operation: Operation,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[CreationWrite]:
    assert resource_owner.owner is not None
    ledger_path = resource_owner.leases._state_path
    assert ledger_path is not None
    phase = request.param
    target_path = resource_owner.owner._store.path if phase == "intent" else ledger_path
    ordinal = {"intent": 1, "register": 1, "acquire": 2, "prepare": 3}[phase]
    gate = CreationWrite(phase)
    loop, replace = asyncio.get_running_loop(), Path.replace
    writes = 0

    def publishing(source: Path, target: Path) -> Path:
        nonlocal writes
        if target == target_path:
            writes += 1
            if writes == ordinal:
                gate.writers.append(get_ident())
                loop.call_soon_threadsafe(gate.entered.set)
                assert gate.release.wait(5), "测试未释放原创建写入"
                if gate.failure is not None:
                    raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publishing)
    try:
        yield gate
    finally:
        gate.release.set()
