"""构造后更新的文件引用仍须在 HTTP、事件和持久化共用的序列化边界拒绝。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2 import workspace as files
from app.schemas.internal_v2 import workspace_file_operations as operations

ROOT = "boxteam://workspace/file-publication/resources/files/root"
NATIVE_PATHS = ["/private/file-publication", r"C:\private\file-publication",
                r"\\target\private\file-publication", "file:///private/file-publication"]
KEY = {"workspace_id": "file-publication", "scope": "workspace", "root": ROOT, "path": ROOT}
LISTING = {"root_path": ROOT, "path": ROOT, "directory_key": KEY, "directory_revision": 1,
           "items": [{"name": "entry.txt", "path": ROOT + "/entry.txt", "kind": "file"}]}
RECEIPT = {"operation_id": "op_" + "1" * 32, "client_sequence": 1, "queue_seq": 1,
           "kind": "create", "state": "queued", "receipt_revision": 1, "effect": "none",
           "created_path": ROOT + "/entry.txt", "snapshots": [LISTING],
           "items": [{"path": ROOT + "/entry.txt", "kind": "file", "state": "pending"}]}
Case = tuple[type[BaseModel], Mapping[str, object], tuple[str | int, ...]]
CASES: list[Case] = [
    (operations.WorkspaceDirectoryKeyDTO, KEY, ("path",)),
    (operations.WorkspaceDirectoryKeyDTO, KEY, ("root",)),
    (operations.WorkspaceFileListDTO, LISTING, ("path",)),
    (operations.WorkspaceFileListDTO, LISTING, ("root_path",)),
    (operations.WorkspaceFileListDTO, LISTING, ("items", 0, "path")),
    (operations.WorkspaceFileListDTO, LISTING, ("directory_key", "path")),
    (operations.WorkspaceFileOperationReceiptDTO, RECEIPT, ("created_path",)),
    (operations.WorkspaceFileOperationReceiptDTO, RECEIPT, ("items", 0, "path")),
    (operations.WorkspaceFileOperationReceiptDTO, RECEIPT, ("snapshots", 0, "items", 0, "path")),
    (files.WorkspaceFileRootsDTO, {"workspace_uri": ROOT, "filesystem_uri": ROOT, "filesystem_label": "目录"}, ("filesystem_uri",)),
    (files.WorkspaceFileReferencesDTO, {"uris": [ROOT]}, ("uris", 0)),
    (files.WorkspaceFileContentDTO, {"root_path": ROOT, "path": ROOT + "/entry.txt", "name": "entry.txt",
                                   "content": "正文", "language": "text", "size": 6, "revision": "1" * 64}, ("path",)),
    (files.WorkspaceFileChangeBatchDTO, {"changes": [{"kind": "create", "path": ROOT}], "overflow": False}, ("changes", 0, "path")),
    (files.WorkspaceFileChangeBatchDTO, {"changes": [], "overflow": True, "invalidated_roots": [ROOT]}, ("invalidated_roots", 0)),
    (files.FileTreeShortcutDTO, {"path": ROOT, "label": "目录", "source": "session"}, ("path",)),
    (files.WorkspaceFileRevealDTO, {"path": ROOT}, ("path",)),
]


def _replace_reference(value: object, path: tuple[str | int, ...], replacement: str) -> object:
    if not path:
        return replacement
    head, *tail = path
    if isinstance(value, BaseModel) and isinstance(head, str):
        return value.model_copy(update={head: _replace_reference(getattr(value, head), tuple(tail), replacement)})
    if isinstance(value, list) and isinstance(head, int):
        result = value.copy()
        result[head] = _replace_reference(result[head], tuple(tail), replacement)
        return result
    raise AssertionError(f"测试引用坐标与模型不符: {path}")


@pytest.fixture(params=CASES, ids=[f"{model.__name__}-{'-'.join(map(str, path))}" for model, _, path in CASES])
def file_reference_case(request: pytest.FixtureRequest) -> tuple[BaseModel, tuple[str | int, ...]]:
    model, payload, path = cast(Case, request.param)
    return model.model_validate(payload), path


@pytest.mark.parametrize("native", NATIVE_PATHS)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_updated_reference_cannot_be_serialized(
    file_reference_case: tuple[BaseModel, tuple[str | int, ...]], native: str,
    mode: Literal["python", "json", "json-text"],
) -> None:
    original, path = file_reference_case
    before = original.model_dump_json()
    damaged = _replace_reference(original, path, native)
    assert isinstance(damaged, BaseModel)
    with pytest.raises(PydanticSerializationError):
        if mode == "json-text":
            damaged.model_dump_json()
        else:
            damaged.model_dump(mode=mode)
    assert original.model_dump_json() == before


def test_mutating_reference_list_is_rejected_without_rewriting_it() -> None:
    references = files.WorkspaceFileReferencesDTO(uris=[ROOT])
    references.uris.append(NATIVE_PATHS[0])
    with pytest.raises(PydanticSerializationError):
        references.model_dump_json()
    assert references.uris == [ROOT, NATIVE_PATHS[0]]


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_file_body_and_user_label_preserve_native_text(native: str) -> None:
    content = files.WorkspaceFileContentDTO(root_path=ROOT, path=ROOT + "/entry.txt", name=native,
                                          content=native, language="text", size=len(native), revision="1" * 64)
    shortcut = files.FileTreeShortcutDTO(path=ROOT, label=native, source="session")
    assert content.model_dump(mode="json")["content"] == native
    assert content.model_dump(mode="json")["name"] == native
    assert shortcut.model_dump(mode="json")["label"] == native
    # 普通原生输入由目标端 owner 解释；输出校验不能反向取消输入许可。
    assert files.WorkspaceFileReferencesRequest(paths=[native]).paths == [native]
    assert files.FileTreeShortcutRequest(path=native).path == native


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_reference_construction_still_rejects_native_paths(native: str) -> None:
    with pytest.raises(ValidationError):
        files.WorkspaceFileReferencesDTO(uris=[native])


def test_updated_valid_alias_uses_the_same_canonical_uri_as_construction() -> None:
    alias = ROOT + "/%65ntry.txt"
    canonical = ROOT + "/entry.txt"
    original = operations.WorkspaceFileNodeDTO(name="entry.txt", path=canonical, kind="file")
    copied = original.model_copy(update={"path": alias})
    constructed = operations.WorkspaceFileNodeDTO(name="entry.txt", path=alias, kind="file")
    assert copied.model_dump(mode="json") == constructed.model_dump(mode="json")
    assert copied.model_dump()["path"] == canonical
    assert copied.path == alias and original.path == canonical


def test_directory_key_projection_still_validates_the_complete_original_key() -> None:
    key = operations.WorkspaceDirectoryKeyDTO.model_validate(KEY)
    assert key.model_dump(include={"path"}) == {"path": ROOT}
    damaged = key.model_copy(update={"workspace_id": "another-workspace"})
    with pytest.raises(PydanticSerializationError, match="工作区 URI 与执行 owner 不一致"):
        damaged.model_dump(include={"path"})
