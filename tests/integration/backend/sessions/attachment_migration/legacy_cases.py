"""只在完整固定旧源码中显式运行；复用该版本的真实产品入口与测试设施。"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.container import AppContainer
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    BODY,
    BackupCase,
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    online_backup_case as online_backup_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    test_owner_creation_before_ledger_registration_blocks_offline_backup as generate_owner_failure,
)
from tests.integration.backend.sessions.test_attachment_offline_directories import (
    accept_message,
)
from tests.integration.backend.sessions.test_attachment_offline_directories import (
    shared_blob_case as shared_blob_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


@pytest.fixture(scope="session", autouse=True)
def legacy_runtime_paths():
    # 源码归档仍完整隔离；工作区不嵌套在归档内，避免旧 SQLite VFS 的路径长度上限。
    output = Path(os.environ["BOXTEAM_LEGACY_INPUT_OUTPUT"])
    context_for = TestRunContext.from_test_file

    def relocated(cls, test_file, **options):
        context = context_for(test_file, **options)
        return replace(context, workspace_root=output / "workspace")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TestRunContext, "from_test_file", classmethod(relocated))
        yield


def publish(case: BackupCase, **evidence: object) -> None:
    target = Path(os.environ["BOXTEAM_LEGACY_INPUT_RECEIPT"])
    target.write_text(json.dumps({
        "session_id": case.session_id, "thread_id": case.thread_id, "message_id": case.message_id,
        "index": case.index_path.relative_to(case.workspace).as_posix(),
        "original": case.original.relative_to(case.workspace).as_posix(),
        "canonical_items": case.canonical_items, **evidence,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_generate_queued(online_backup_case: BackupCase) -> None:
    publish(online_backup_case)


async def test_generate_shared(shared_blob_case: BackupCase) -> None:
    publish(shared_blob_case)


async def test_generate_preview(online_backup_case: BackupCase, startup_container: AppContainer) -> None:
    content = BytesIO()
    Image.new("RGB", (900, 600), (32, 64, 96)).save(content, format="PNG")
    message = await startup_container.message_service.create(online_backup_case.session_id, MessageCreateRequest(
        content="图片预览", attachments=[AttachmentRef(
            file_id="inline:image.png", name="图像.png", content_type="image/png",
            data_url="data:image/png;base64," + base64.b64encode(content.getvalue()).decode(),
        )],
    ))
    preview = startup_container.attachment_blob_store.read_thumbnail(
        online_backup_case.session_id, message.attachments[0].file_id,
    ).data
    accept_message(startup_container, message)
    publish(online_backup_case, preview_base64=base64.b64encode(preview).decode(),
            image_base64=base64.b64encode(content.getvalue()).decode())


async def test_generate_terminal(online_backup_case, startup_container, monkeypatch) -> None:
    await generate_owner_failure(
        online_backup_case, startup_container, monkeypatch, "terminal",
    )
    publish(online_backup_case)


async def test_generate_browser(online_backup_case, startup_container, monkeypatch) -> None:
    await generate_owner_failure(
        online_backup_case, startup_container, monkeypatch, "browser",
    )
    publish(online_backup_case)


async def test_generate_forks(production_fork_container, fork_execution_message, monkeypatch) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    source = await container.session_service.create(SessionCreateRequest(title="离线目录转换真实来源"))
    # TODO: 团队设计待修订；这里只沿用旧执行入口的初始化，不追加团队专项验收。
    accepted = await fork_execution_message(
        container, source.session_id, content="保全附件历史",
        gateway_id="gateway_6c8b2f9317a5a2c6c4569958b1e23d84", attachments=[AttachmentRef(
            file_id="inline:source.txt", name="source.txt", content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode(BODY).decode(),
        )],
    )
    from app.services.infrastructure.rollout_context.checkpoint import fork_owner

    cleanup = fork_owner.remove_captured_snapshot
    retained = []

    def retain_first_completed_snapshot(sessions_root, record):
        # 仅截断终态后的清理步骤，关键执行、fork 与发布状态仍由旧产品生成。
        if not retained:
            retained.append(record.copy_operation_id)
        else:
            cleanup(sessions_root, record)

    monkeypatch.setattr(fork_owner, "remove_captured_snapshot", retain_first_completed_snapshot)
    keys = []
    for _ in range(2):
        request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
        source = (await context.forks.fork(request)).session
        keys.append(context.forks._creation_key(request.idempotency_key))
    connection = container.attachment_blob_store._catalog.connection
    payloads = []
    for table, kind in (("fork_attachment_claims", "claim"), ("fork_attachment_mappings", "mapping")):
        for row in connection.execute(f"SELECT * FROM {table}"):
            metadata = dict(row)
            payloads.append((kind, metadata, metadata.pop("snapshot_json")))
    captures = [context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key) for key in keys]
    assert all(capture is not None for capture in captures)
    thread = container.session_service.path_resolver.main_thread_id(source.session_id)
    index = container.session_service.path_resolver.resolve_session_node(source.session_id) / "threads" / thread / "rollout/index.sqlite"
    with closing(sqlite3.connect(index)) as database:
        items = tuple(database.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence"))
    locator = connection.execute("SELECT relative_locator FROM attachment_blobs LIMIT 1").fetchone()[0]
    case = BackupCase(container.workspace_root, container.workspace_root / "unused-snapshot", index,
                      container.workspace_root / ".boxteam/attachments" / locator,
                      source.session_id, thread, accepted.message_id, items)
    publish(case, fork_keys=keys, fork_payloads=payloads, captures=[asdict(capture) for capture in captures])
