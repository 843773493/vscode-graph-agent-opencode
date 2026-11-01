"""文件回执更新的序列化失败必须回滚原SQLite领取事务。"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure.workspace_files.store import WorkspaceFileStore


@pytest.fixture
def accepted_file_store(empty_workspace_scope: tuple[str, Path]) -> Iterator[WorkspaceFileStore]:
    workspace_id, workspace = empty_workspace_scope
    root = f"boxteam://workspace/{workspace_id}/resources/files/root"
    store = WorkspaceFileStore(workspace, workspace_id)
    try:
        intent = WorkspaceFileMutationIntentDTO(
            client_operation_id="op_" + "1" * 32, client_sequence=1, expected_directory_revision=1,
            directory_key=WorkspaceDirectoryKeyDTO(workspace_id=workspace_id, scope="workspace", root=root, path=root),
        )
        store.accept(actor="local", kind="create", intent=intent, request_hash="1" * 64,
                     payload={"created_path": root + "/entry.txt", "workspace_id": workspace_id,
                              "actor": "local", "intent": intent.model_dump(), "request_hash": "1" * 64})
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("native", ["/private/file-publication", r"C:\private\file-publication",
                                   r"\\target\private\file-publication", "file:///private/file-publication"])
def test_corrupt_in_memory_receipt_cannot_be_claimed_or_persisted(
    accepted_file_store: WorkspaceFileStore, native: str,
) -> None:
    store = accepted_file_store
    original = store.get("op_" + "1" * 32, "local")
    before = tuple(store._db.execute("SELECT * FROM operations").fetchall())
    damaged = original.receipt.model_copy(update={"created_path": native})
    with pytest.raises(PydanticSerializationError):
        store.claim(replace(original, receipt=damaged))
    assert tuple(store._db.execute("SELECT * FROM operations").fetchall()) == before
    assert store.get(original.operation_id, "local") == original
    # 明确修复原记录后仍由同一领取链执行，未添加回退writer。
    claimed = store.claim(original)
    assert claimed.state == "running" and claimed.token == original.token + 1
    assert claimed.receipt.created_path == original.receipt.created_path
