"""附件输入保留原生路径，目录与历史引用在发布时沿唯一grammar核验。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.attachment import AttachmentRef
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageCreateRequest, MessageDTO
from app.schemas.internal_v2.turn import TurnAttachmentDTO, TurnUserMessageDTO

DIRECTORY = "boxteam://workspace/absent-workspace/resources/attachments/att_" + "a" * 32
FILE = DIRECTORY + "/original/content"
NATIVE = ("/private/attachment-publication", r"C:\private\attachment-publication",
          r"\\target\private\attachment-publication", "file:///private/attachment-publication")
Mode = Literal["python", "json", "json-text"]


@pytest.fixture(params=["input-directory", "history-directory", "history-file"])
def reference(request: pytest.FixtureRequest) -> tuple[BaseModel, str, str]:
    if request.param == "input-directory":
        return AttachmentRef(file_id=FILE, directory_uri=DIRECTORY), "directory_uri", DIRECTORY
    field = "directory_uri" if request.param == "history-directory" else "file_id"
    return TurnAttachmentDTO(file_id=FILE, directory_uri=DIRECTORY), field, DIRECTORY if field == "directory_uri" else FILE


@pytest.mark.parametrize("native", NATIVE)
def test_construction_rejects_native_output_reference(reference: tuple[BaseModel, str, str], native: str) -> None:
    model, field, _ = reference
    with pytest.raises(ValidationError):
        type(model).model_validate({**model.model_dump(), field: native})


@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
@pytest.mark.parametrize("native", NATIVE)
def test_copied_reference_cannot_publish_native_path(
    reference: tuple[BaseModel, str, str], mode: Mode, native: str,
) -> None:
    model, field, _ = reference
    before = model.model_dump_json()
    copied = model.model_copy(update={field: native})
    with pytest.raises(PydanticSerializationError) as failure:
        if mode == "json-text":
            copied.model_dump_json()
        else:
            copied.model_dump(mode=mode)
    assert native not in str(failure.value)
    assert model.model_dump_json() == before


@pytest.mark.parametrize("field", ["file_id", "directory_uri"])
def test_nested_history_mutation_is_checked(field: str) -> None:
    result = TurnUserMessageDTO(message_id="message", content=NATIVE[0], created_at=datetime(2026, 11, 1, tzinfo=UTC),
                                attachments=[TurnAttachmentDTO(file_id=FILE, directory_uri=DIRECTORY)])
    result.attachments[0] = result.attachments[0].model_copy(update={field: NATIVE[1]})
    with pytest.raises(PydanticSerializationError):
        result.model_dump_json()


@pytest.mark.parametrize("uri", [FILE, DIRECTORY + "/derived/report.txt",
    "boxteam://workspace/missing/resources/files/root/%e4%b8%ad.txt",
    "boxteam://gateway_remote/workspace/missing/resources/files/root/file.txt",
    "boxteam://workspace/missing/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_" + "b" * 32])
def test_history_accepts_dangling_remote_and_frozen_legacy_files(uri: str) -> None:
    ref = TurnAttachmentDTO(file_id=uri)
    published = ref.model_copy().model_dump(mode="json")
    assert published["file_id"] == uri.replace("%e4%b8%ad", "%E4%B8%AD")
    assert published["directory_uri"] is None


@pytest.mark.parametrize("uri", [DIRECTORY,
    "boxteam://workspace/missing/resources/files/root",
    "boxteam://workspace/missing/resources/config/workspace",
    "boxteam://workspace/missing/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/tool-results/tool_" + "b" * 24 + ".txt"])
def test_history_does_not_accept_directory_or_other_resource_kinds(uri: str) -> None:
    with pytest.raises(ValidationError):
        TurnAttachmentDTO(file_id=uri)
    copied = TurnAttachmentDTO(file_id=FILE).model_copy(update={"file_id": uri})
    with pytest.raises(PydanticSerializationError):
        copied.model_dump_json()


@pytest.mark.parametrize("uri", [FILE, "boxteam://workspace/missing/resources/files/root"])
def test_directory_reference_must_be_managed_attachment_root(uri: str) -> None:
    with pytest.raises(ValidationError):
        AttachmentRef(file_id=FILE, directory_uri=uri)


@pytest.mark.parametrize("file_id", [*NATIVE, "relative/file.txt", "inline:upload"])
def test_native_and_inline_input_remains_for_original_attachment_owner(file_id: str) -> None:
    result = MessageCreateRequest(content=NATIVE[0], attachments=[AttachmentRef(file_id=file_id)])
    assert result.model_dump(mode="json")["attachments"][0]["file_id"] == file_id
    assert result.model_dump(mode="json")["content"] == NATIVE[0]


def test_nested_input_directory_cannot_bypass_publication() -> None:
    result = MessageCreateRequest(content="正文", attachments=[AttachmentRef(file_id=FILE, directory_uri=DIRECTORY)])
    result.attachments[0] = result.attachments[0].model_copy(update={"directory_uri": NATIVE[0]})
    with pytest.raises(PydanticSerializationError):
        result.model_dump_json()


@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("field", ["file_id", "directory_uri"])
def test_message_output_validates_resolved_attachment_without_changing_input(native: str, field: str) -> None:
    original = AttachmentRef(file_id=FILE, directory_uri=DIRECTORY)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    arguments = {"message_id": "message", "session_id": "session", "thread_id": "thread",
                 "role": MessageRole.user, "content": native, "created_at": now, "updated_at": now}
    with pytest.raises(ValidationError):
        MessageDTO.model_validate(arguments | {"attachments": [original.model_copy(update={field: native})]})
    result = MessageDTO.model_validate(arguments | {"attachments": [original]})
    result.attachments[0] = original.model_copy(update={field: native})
    with pytest.raises(PydanticSerializationError):
        result.model_dump_json()
    assert original.file_id == FILE and original.directory_uri == DIRECTORY
