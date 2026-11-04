"""真实附件 catalog 与文件列表组合时，根解析成本不随 child 数量增长。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.identifier import create_prefixed_id
from app.core.resource_uri import parse_resource_uri
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger
from app.services.infrastructure.workspace_service import WorkspaceService


@pytest.fixture
async def attachment_case(tmp_path: Path, session_bundle_factory):
    session_id = create_prefixed_id("ses")
    session_bundle_factory(tmp_path / ".boxteam/sessions", session_id)
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    store = AttachmentBlobStore(
        tmp_path, leases=ExternalResourceLeaseLedger(state_path=tmp_path / ".boxteam/resources.json"),
    )
    service = WorkspaceService(config_service=config, workspace_root=tmp_path, attachments=store)
    try:
        ref, = await store.persist_inline(
            session_id,
            [AttachmentRef(file_id="inline:scan.txt", name="scan.txt", content_type="text/plain",
                           data_url="data:text/plain;base64,c2Nhbg==")],
            thread_id=store._path_resolver.main_thread_id(session_id), message_id="scan-upload",
        )
        assert ref.directory_uri is not None
        root = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
        yield service, store, ref.directory_uri, root
    finally:
        service.close()
        store.close()


async def test_attachment_listing_resolves_catalog_root_per_scan(
    attachment_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, uri, root = attachment_case
    derived = root / "derived"
    for index in range(256):
        (derived / f"file-{index:03}.txt").write_text("scan", encoding="utf-8")
    (derived / "outside-alias").symlink_to(tmp_path, target_is_directory=True)
    (derived / ".boxteam").mkdir()
    original_resolve = store.resolve_directory_uri
    resolutions = 0

    def observed_resolve(*args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(store, "resolve_directory_uri", observed_resolve)
    listed = await service.list_files(path=uri + "/derived", limit=500)
    assert len(listed.items) == 256
    assert all(item.name not in {"outside-alias", ".boxteam"} for item in listed.items)
    assert resolutions <= 16, f"附件根不应为每个 child 重查 catalog: {resolutions}"
    print(f"attachment directory resolutions: {resolutions}")
