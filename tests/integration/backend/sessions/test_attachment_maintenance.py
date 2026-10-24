"""真实 Backend 启动、排空与附件维护互斥，不调用模型或协作功能。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import container as container_module
from app import main
from app.container import AppContainer
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.schemas.internal_v2.session import SessionCreateRequest
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
from tests.support.workspaces import prepare_test_workspace


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
            "terminal_backend_url": None, "browser_backend_url": None,
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
        async with main.lifespan(main.app):
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
    async with main.lifespan(main.app):
        for container in case.containers:
            root = container.workspace_root / ".boxteam/attachments"
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(root, exclusive=True):
                pytest.fail("运行中的后端不能进入离线维护")
    assert len(closed) == 2
    assert_closed(case)
    for root in case.roots.values():
        with AttachmentMaintenanceLock(root / ".boxteam/attachments", exclusive=True):
            pass
