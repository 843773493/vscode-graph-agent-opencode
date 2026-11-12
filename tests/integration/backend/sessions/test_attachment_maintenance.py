"""真实 Backend 启动、排空与附件维护互斥，不调用模型或协作功能。"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from pathlib import Path
from threading import Event, get_ident
from typing import BinaryIO

import httpx
import pytest

from app import container as container_module
from app import main
from app.container import AppContainer
from app.core.path_utils import workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog import (
    maintenance as maintenance_module,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    assert_closed,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414
)
from tests.support.workspaces import (
    prepare_test_workspace,
    render_test_workspace_config,
)


@pytest.fixture
def maintenance_roots(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    roots = [context.workspace_root / name for name in ("first", "second")]
    mounts = []
    for root in roots:
        prepare_test_workspace(
            workspace_root=root,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
            template_items=(),
        )
        mounts.append({
            "workspace_id": load_or_create_workspace_id(root), "workspace_root": str(root),
            "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None,
        })
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps(mounts))
    monkeypatch.setattr(main, "_model_stream_controller", None)
    return roots


@pytest.mark.parametrize("blocked_mount", [0, 1])
async def test_backend_maintenance_conflict_precedes_all_business_initialization(
    maintenance_roots: list[Path], monkeypatch: pytest.MonkeyPatch, blocked_mount: int,
) -> None:
    roots = maintenance_roots
    calls: list[str] = []

    def unexpected_initialization(*_args: object, **_kwargs: object) -> None:
        calls.append("initialized")
        raise AssertionError("维护占用时不应触发业务初始化")

    monkeypatch.setattr(main, "initialize_directories", unexpected_initialization)
    monkeypatch.setattr(container_module, "build_app_container", unexpected_initialization)
    with (
        AttachmentMaintenanceLock(roots[blocked_mount] / ".boxteam/attachments", exclusive=True),
        pytest.raises(AttachmentMaintenanceBusyError, match="在线访问"),
    ):
        async with main.lifespan(main.app) as _main_lifespan_state:
            await _main_lifespan_state["after_http_startup"]()
            pytest.fail("维护占用时后端不能就绪")
    assert not calls
    assert main.app.state.workspace_container_registry is None
    for root in roots:
        assert not (root / ".boxteam/sessions").exists()
        assert not (root / ".boxteam/attachments").exists()
        # 后一个 mount 冲突时，先前已取得的共享锁也必须归还。
        with AttachmentMaintenanceLock(root / ".boxteam/attachments", exclusive=True):
            pass


def test_direct_service_graph_construction_respects_maintenance(
    maintenance_roots: list[Path],
) -> None:
    root = maintenance_roots[0]
    with (
        AttachmentMaintenanceLock(root / ".boxteam/attachments", exclusive=True),
        pytest.raises(AttachmentMaintenanceBusyError),
    ):
        container_module.build_app_container(workspace_root=root, project_root=Path.cwd())
    assert not (root / ".boxteam/sessions").exists()
    assert not (root / ".boxteam/attachments").exists()


async def test_upload_refuses_maintenance_before_creating_pin_or_ingest(
    startup_container: AppContainer,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="附件维护互斥"))
    resolver = container.session_service.path_resolver
    database_path = resolver.resolve_session_node(session.session_id) / "session-control.sqlite"
    control = SessionControlStore(database_path)
    try:
        before = tuple(control.connection.iterdump())
        with (
            AttachmentMaintenanceLock(container.workspace_root / ".boxteam/attachments", exclusive=True),
            pytest.raises(AttachmentMaintenanceBusyError),
        ):
            await container.message_service.create(session.session_id, MessageCreateRequest(
                content="上传原件", attachments=[AttachmentRef(
                    file_id="inline:maintenance.txt", name="maintenance.txt", content_type="text/plain",
                    data_url="data:text/plain;base64,bWFpbnRlbmFuY2U=",
                )],
            ))
        assert tuple(control.connection.iterdump()) == before
        assert not (container.workspace_root / ".boxteam/attachments/catalog.sqlite").exists()
    finally:
        control.close()


async def test_real_double_mount_holds_access_through_writer_shutdown(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = mounted_case
    closed: list[str] = []
    close = AttachmentBlobStore.close

    def checked_close(store: AttachmentBlobStore) -> None:
        close(store)
        # 即使附件连接已关，完整 Backend 生命周期仍保护其它历史 writer。
        root = store._attachments_root
        with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(root, exclusive=True):
            pytest.fail("历史 writer 排空前不能进入离线维护")
        closed.append(str(root))

    # lifespan 登记的是绑定方法，必须在进入真实 lifespan 前观察关闭入口。
    monkeypatch.setattr(AttachmentBlobStore, "close", checked_close)
    async with main.lifespan(main.app) as _main_lifespan_state:
        await _main_lifespan_state["after_http_startup"]()
        for container in case.containers:
            root = container.workspace_root / ".boxteam/attachments"
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(root, exclusive=True):
                pytest.fail("运行中的后端不能进入离线维护")
    assert len(closed) == 2
    assert_closed(case)
    for root in case.roots.values():
        with AttachmentMaintenanceLock(root / ".boxteam/attachments", exclusive=True):
            pass


@pytest.fixture
def maintenance_case(mounted_case: MountedCase) -> MountedCase:
    # 关闭验收只创建Session，不调用模型或读取个人Provider凭据。
    config = json.loads(render_test_workspace_config(
        (Path.cwd() / "configs/tests/workspace/default.jsonc").read_text(encoding="utf-8"),
    ))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "maintenance-contract-unused-key"
        provider.pop("auth", None)
    for root in mounted_case.roots.values():
        (root / ".boxteam/workspace.jsonc").write_text(json.dumps(config, ensure_ascii=False) + "\n")
    return mounted_case


@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
async def test_maintenance_native_release_keeps_close_owner_and_diagnostics_responsive(
    maintenance_case: MountedCase, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    deadlines: list[asyncio.Timeout] = []
    acquired: list[AttachmentMaintenanceLock] = []
    acquire = AttachmentMaintenanceLock.acquire

    def capture_acquire(lock: AttachmentMaintenanceLock) -> None:
        acquire(lock)
        if lock not in acquired:
            acquired.append(lock)

    def capture_deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(AttachmentMaintenanceLock, "acquire", capture_acquire)
    monkeypatch.setattr(inspect.getmodule(main.BackendLifespan), "timeout", capture_deadline)
    token = secrets.token_hex(32)
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    lifetime = main.lifespan(main.app)
    _main_lifespan_state = await lifetime.__aenter__()
    await _main_lifespan_state["after_http_startup"]()
    backend = main.app.state.workspace_lifespan
    registry = backend.registry
    selected = maintenance_case.containers[-1]
    # Backend先取得全部维护锁，再初始化各挂载的业务owner。
    access = acquired[:len(maintenance_case.containers)]
    selected_access = next(lock for lock in access if lock.path == (
        selected.workspace_root / ".boxteam/.attachments-maintenance.lock"
    ))
    fd = selected_access._fd
    assert fd is not None
    entered, release = Event(), Event()
    attempts: list[int] = []
    event_loop_thread = get_ident()
    original_unlock = maintenance_module.portalocker.unlock

    def unlock(current: int | BinaryIO) -> None:
        if current == fd:
            attempts.append(get_ident())
            entered.set()
            if not release.wait(8):
                raise RuntimeError("维护锁原生释放阻塞事件循环，测试保护期限已到")
        original_unlock(current)

    closing = None
    try:
        with workspace_context(selected.workspace_id, selected.workspace_root, session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="维护锁关闭"))
        locator = selected.session_catalog.resolver.resolve_session_node(session.session_id)
        manifest = (locator / "session.json").read_bytes()
        monkeypatch.setattr(maintenance_module.portalocker, "unlock", unlock)
        closing = asyncio.create_task(backend.close())
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        owner = backend._close_owner
        assert owner is not None and not owner.done() and len(deadlines) == 1
        assert all(workspace.closed for workspace in backend.workspaces)
        assert backend._steps[0][0] == "maintenance"
        assert len(attempts) == 1 and attempts[0] != event_loop_thread
        if interruption == "deadline":
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError, match="stage=maintenance"):
                await asyncio.wait_for(closing, 2)
        else:
            closing.cancel("取消维护锁关闭观察")
            with pytest.raises(asyncio.CancelledError, match="取消维护锁关闭观察"):
                await asyncio.wait_for(closing, 2)
        assert not owner.done() and backend._close_owner is owner
        assert main.app.state.workspace_container_registry is registry
        assert main.app.state.workspace_lifespan is backend
        # 原生unlock尚未执行，实际跨进程锁仍排斥离线维护。
        for container in maintenance_case.containers:
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                container.workspace_root / ".boxteam/attachments", exclusive=True,
            ):
                pytest.fail("原维护锁释放未结算时不应允许离线维护")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test") as client:
            response = await asyncio.wait_for(client.get(
                "/api/v1/internal/runtime-logging", headers={"X-Local-Token": token},
            ), 2)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        retry = asyncio.create_task(backend.close())
        await asyncio.sleep(0)
        assert backend._close_owner is owner and not retry.done()
        release.set()
        await asyncio.wait_for(retry, 5)
        assert backend.closed and backend._close_owner is owner
        assert len(attempts) == 1 and attempts[0] != event_loop_thread
        assert main.app.state.workspace_lifespan is None
        assert (locator / "session.json").read_bytes() == manifest
        for container in maintenance_case.containers:
            with AttachmentMaintenanceLock(container.workspace_root / ".boxteam/attachments", exclusive=True):
                pass
    finally:
        release.set()
        monkeypatch.setattr(maintenance_module.portalocker, "unlock", original_unlock)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
