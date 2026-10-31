"""原生 cwd 解析等待不阻塞事件循环，取消不释放仍在工作的 Thread holder。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.resource_uri import ParsedResourceUri, resource_display_uri
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.services.business.terminal_resources import owner as owner_module
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A, WORKSPACE_ID
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture
def terminal_owner(tmp_path: Path) -> TerminalOwnerFixture:
    return build_terminal_owner_fixture(tmp_path)


@pytest.mark.parametrize("input_kind", ["relative", "absolute", "uri", "gateway_uri", "external"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
async def test_workdir_wait_preserves_loop_holder_input_and_original_failure(
    terminal_owner: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    input_kind: str, outcome: str,
) -> None:
    case = terminal_owner
    owner = case.owner
    peer = await case.create(scope="workspace")
    target = (case.root.parent if input_kind == "external" else case.root) / "waiting-cwd"
    target.mkdir()
    gateway_id = "gateway_00000000000040008000000000000099"
    request_id = "req-cwd-worker"

    class NativeBindings:
        def bind(self, root: Path) -> str:
            raise AssertionError("cwd URI 解析不应新建目录绑定")

        def resolve(self, parsed: ParsedResourceUri) -> Path:
            assert parsed.scope_id == get_current_gateway_id() == gateway_id
            assert get_current_request_id() == request_id
            return case.root

    if input_kind == "gateway_uri":
        monkeypatch.setattr(owner._file_paths, "bindings", NativeBindings())
        cwd = resource_display_uri(gateway_authority=gateway_id, scope="gateway", scope_id=gateway_id,
                                   kind="files", tail_segments=("01900000000070008000000000000099", target.name))
    elif input_kind == "uri":
        cwd = resource_display_uri(gateway_authority=gateway_id, scope="workspace", scope_id=WORKSPACE_ID,
                                   kind="files", tail_segments=("root", target.name))
    else:
        cwd = target.name if input_kind == "relative" else str(target)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    timed_out: list[bool] = []
    failure = OSError(5, "原生解析失败", str(target))
    resolve = Path.resolve

    def waiting(path: Path, strict: bool = False) -> Path:
        if path == target:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                timed_out.append(True)
                raise TimeoutError("事件循环未能响应原生解析等待")
            if outcome == "failure":
                raise failure
        return resolve(path, strict=strict)

    token = set_current_gateway_id(gateway_id)
    request_token = set_current_request_id(request_id)
    pending: asyncio.Task[dict[str, object]] | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", waiting)
            pending = asyncio.create_task(owner.create_terminal(
                session_id=SESSION_A, thread_id=THREAD_A, cwd=cwd,
                holder_id="request:cwd-wait", operation_id="cwd-wait",
            ))
            await asyncio.wait_for(entered.wait(), 5)
            assert not timed_out, "cwd 解析阻塞了事件循环"
            assert not pending.done()
            current = await owner.get_resource(str(peer["resource_id"]))
            assert current["status"] == "running"
            assert tuple(owner._store.ids()) == (peer["resource_id"],)
            assert case.transport.creates == 1
            assert not case.leases.leases_for_resource(str(peer["resource_id"]))
            async with owner.deletion_holder.lock_for(SESSION_A, wait=False) as probe:
                assert not probe.acquired
            if outcome == "cancel":
                assert pending.cancel("cwd 调用取消")
                await asyncio.sleep(0)
                assert pending.cancel("再次取消不能遗弃解析")
                await asyncio.sleep(0)
                assert not pending.done()
                async with owner.deletion_holder.lock_for(SESSION_A, wait=False) as probe:
                    assert not probe.acquired
            release.set()
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError, match="cwd 调用取消"):
                    await pending
            elif outcome == "failure":
                with pytest.raises(RuntimeError if input_kind in {"uri", "gateway_uri"} else OSError) as caught:
                    await pending
                original: BaseException | None = caught.value
                while original is not failure and original is not None:
                    original = original.__cause__
                assert original is failure
            else:
                created = await pending
                assert created["cwd_relative"] == os.path.relpath(target, case.root)
                assert created["source_thread"] == {"session_id": SESSION_A, "thread_id": THREAD_A}
                assert created["status"] == "running" and case.transport.creates == 2
            if outcome != "success":
                assert tuple(owner._store.ids()) == (peer["resource_id"],)
                assert case.transport.creates == 1
            async with owner.deletion_holder.lock_for(SESSION_A, wait=False) as probe:
                assert probe.acquired
            assert str(target) not in json.dumps(json.loads(owner._store.path.read_text()))
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        reset_current_gateway_id(token)
        reset_current_request_id(request_token)


async def test_cancelled_queued_workdir_keeps_holder_without_creating_resources(
    terminal_owner: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = terminal_owner
    owner = case.owner
    peer = await case.create(scope="workspace")
    loop = asyncio.get_running_loop()
    entered, queued, release = asyncio.Event(), asyncio.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-cwd")
    occupying: asyncio.Future[None] | None = None
    resolved: list[str | None] = []
    resolve = owner._resolve_workdir

    def occupy() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("cwd 线程池屏障未释放")

    def resolving(cwd: str | None) -> str:
        resolved.append(cwd)
        return resolve(cwd)

    async def queue(operation, *args, **kwargs):
        nonlocal occupying
        occupying = loop.run_in_executor(None, occupy)
        await entered.wait()
        queued.set()
        return await run_sync_shielded(operation, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        patch.setattr(owner, "_resolve_workdir", resolving)
        patch.setattr(owner_module, "run_sync_shielded", queue, raising=False)
        creating = asyncio.create_task(case.create())
        try:
            await asyncio.wait_for(queued.wait(), 2)
            await asyncio.sleep(0)
            creating.cancel("排队 cwd 取消")
            await asyncio.sleep(0)
            creating.cancel("重复取消")
            await asyncio.sleep(0)
            assert not creating.done() and resolved == []
            assert tuple(owner._store.ids()) == (peer["resource_id"],) and case.transport.creates == 1
            async with owner.deletion_holder.lock_for(SESSION_A, wait=False) as probe:
                assert not probe.acquired
            release.set()
            with pytest.raises(asyncio.CancelledError, match="排队 cwd 取消"):
                await asyncio.wait_for(creating, 3)
            assert resolved == [None] and case.transport.creates == 1
            assert tuple(owner._store.ids()) == (peer["resource_id"],)
            async with owner.deletion_holder.lock_for(SESSION_A, wait=False) as probe:
                assert probe.acquired
        finally:
            release.set()
            await asyncio.gather(creating, return_exceptions=True)
            if occupying is not None:
                await occupying
            executor.shutdown(wait=True)
