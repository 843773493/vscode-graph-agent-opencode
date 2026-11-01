"""已声明的 URI 输出在复制、嵌套修改和持久化序列化时仍须拒绝原生路径。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from app.core.resource_uri import session_artifact_uri, thread_resource_uri
from app.schemas.event import (
    FileEditPayload,
    ToolCallEndPayload,
    ToolOutputReferencePayload,
)
from app.schemas.gateway.directory import (
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
)
from app.schemas.gateway.skill_sources import (
    GatewaySkillSourceEntryDTO,
    GatewaySkillSourcesDTO,
)
from app.schemas.gateway.ui_settings import (
    WebUILayoutSettingsDTO,
    WebUISettingsDTO,
    WebUIWorkspaceFileTreeSettingsDTO,
)
from app.schemas.internal_v2.artifact import ArtifactDTO
from app.schemas.internal_v2.persistent_task import (
    RuntimeSourceActionDTO,
    TaskEvaluationDTO,
    TaskModelCheckDTO,
    TaskThreadTargetDTO,
    TaskTriggerDTO,
)
from app.schemas.internal_v2.session_changes import (
    SessionFileChangeDTO,
    SessionFileReviewResultDTO,
)
from app.schemas.internal_v2.text_mutation import (
    TextMutationFileDTO,
    TextMutationReceiptDTO,
    TextMutationSnapshotDTO,
)

WORKSPACE = "22222222-2222-4222-8222-222222222222"
SESSION = "ses_019b9dffc29a779e8564d196f78bb0a6"
THREAD = "thr_01a100c4a1897a2d8d40b568d23fcbe6"
GATEWAY = "gateway_" + "a" * 32
FILE = f"boxteam://workspace/{WORKSPACE}/resources/files/root/%E6%96%87%20file.txt"
DIRECTORY = f"boxteam://gateway/{GATEWAY}/resources/files/{THREAD.removeprefix('thr_')}"
SKILL = f"boxteam://gateway/{GATEWAY}/resources/skills/fixture/SKILL.md"
TARGET = thread_resource_uri(WORKSPACE, SESSION, THREAD)
TOOL_RESULT = session_artifact_uri(WORKSPACE, SESSION, "tool-results", "tool_" + "a" * 24 + ".txt")
NATIVE = ("/private/uri-publication", r"C:\private\uri-publication", r"\\target\private\uri-publication", "file:///private/uri-publication")
NAMES = ("artifact", "change", "review", "text-file", "text-receipt", "text-snapshot", "task-trigger", "task-check", "task-action", "task-evaluation", "task-target", "directory-entry", "directory", "directory-parent", "directory-home", "skill", "file-edit", "tool-result", "tool-read", "ui-active", "ui-previews", "ui-expanded")
Mode = Literal["python", "json", "json-text"]


@pytest.fixture
def references() -> dict[str, tuple[BaseModel, str, str]]:
    artifact = ArtifactDTO(artifact_id="a", job_id="j", type="text", name="产物", path=FILE, metadata={"body": NATIVE[0]})
    change = SessionFileChangeDTO(file_path=FILE, kind="edit", latest_edit_id="e", diff_text=NATIVE[0])
    tool = ToolOutputReferencePayload(path=TOOL_RESULT, read_path=TOOL_RESULT, tool_name="fixture", tool_call_id="call", byte_count=1, line_count=1, content_sha256="a" * 64)
    directory = GatewayDirectoryListDTO(name="目录", path=DIRECTORY, parent_path=DIRECTORY, home_path=DIRECTORY, limit=1)
    skill = GatewaySkillSourceEntryDTO(name="fixture", entry_identity="entry", source_id="source", display_uri=SKILL, revision=None, byte_length=1, content=NATIVE[0], available=True)
    layout = WebUILayoutSettingsDTO(workspace_preview_file_paths=[FILE], workspace_preview_active_file_path=FILE)
    return {
        "artifact": (artifact, "path", FILE),
        "change": (change, "file_path", FILE),
        "review": (SessionFileReviewResultDTO(session_id=SESSION, file_path=FILE, reviewed=True), "file_path", FILE),
        "text-file": (TextMutationFileDTO(path=FILE, state="published", expected_version="absent"), "path", FILE),
        "text-receipt": (TextMutationReceiptDTO(status="success", effect="completed", target=FILE), "target", FILE),
        "text-snapshot": (TextMutationSnapshotDTO(uri=FILE, before_exists=False, before_content=NATIVE[0], after_exists=True, after_content=NATIVE[1]), "uri", FILE),
        "task-trigger": (TaskTriggerDTO(version="1", type_id="builtin.execution_end", source_uri=TARGET), "source_uri", TARGET),
        "task-check": (TaskModelCheckDTO(thread_uri=TARGET, instruction=NATIVE[0]), "thread_uri", TARGET),
        "task-action": (RuntimeSourceActionDTO(version="1", type_id="builtin.runtime_source", target_uri=TARGET, delivery_mode="context_only"), "target_uri", TARGET),
        "task-evaluation": (TaskEvaluationDTO(thread_uri=TARGET, source_item_id="item", job_id="job", execution_id="exe"), "thread_uri", TARGET),
        "task-target": (TaskThreadTargetDTO(session_id=SESSION, thread_id=THREAD, uri=TARGET), "uri", TARGET),
        "directory-entry": (GatewayDirectoryEntryDTO(name="目录", path=DIRECTORY), "path", DIRECTORY),
        "directory": (directory, "path", DIRECTORY),
        "directory-parent": (directory, "parent_path", DIRECTORY),
        "directory-home": (directory, "home_path", DIRECTORY),
        "skill": (skill, "display_uri", SKILL),
        "file-edit": (FileEditPayload(edit_id="edit", file_path=FILE, kind="edit"), "file_path", FILE),
        "tool-result": (tool, "path", TOOL_RESULT),
        "tool-read": (tool, "read_path", TOOL_RESULT),
        "ui-active": (layout, "workspace_preview_active_file_path", FILE),
        "ui-previews": (layout, "workspace_preview_file_paths", FILE),
        "ui-expanded": (WebUIWorkspaceFileTreeSettingsDTO(expanded_paths_by_workspace={WORKSPACE: [FILE]}), "expanded_paths_by_workspace", FILE),
    }


def replace_reference(model: BaseModel, field: str, value: str) -> BaseModel:
    replacement: object = value
    if field == "workspace_preview_file_paths":
        replacement = [value]
    elif field == "expanded_paths_by_workspace":
        replacement = {WORKSPACE: [value]}
    return model.model_copy(update={field: replacement})


def publish(model: BaseModel, mode: Mode) -> str | dict[str, object]:
    return model.model_dump_json() if mode == "json-text" else model.model_dump(mode=mode)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_reference_refuses_native_location(
    references: dict[str, tuple[BaseModel, str, str]], name: str, native: str, mode: Mode,
) -> None:
    model, field, _ = references[name]
    before = model.model_dump_json()
    with pytest.raises(PydanticSerializationError) as failure:
        publish(replace_reference(model, field, native), mode)
    assert native not in str(failure.value)
    assert model.model_dump_json() == before


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_valid_copied_reference_keeps_existing_wire_and_body(
    references: dict[str, tuple[BaseModel, str, str]], name: str, mode: Mode,
) -> None:
    model, field, uri = references[name]
    assert publish(replace_reference(model, field, uri), mode) == publish(model, mode)


@pytest.mark.parametrize(("name", "uri"), [
    ("task-target", f"boxteam://workspace/{WORKSPACE}/resources/session/{SESSION}"),
    ("directory", FILE), ("skill", FILE), ("skill", SKILL.replace("SKILL.md", "README.md")),
    ("change", TARGET), ("ui-active", TARGET),
    ("tool-result", session_artifact_uri(WORKSPACE, SESSION, "attachments", "att_" + "a" * 32)),
])
def test_copied_uri_retains_domain_restrictions(
    references: dict[str, tuple[BaseModel, str, str]], name: str, uri: str,
) -> None:
    model, field, _ = references[name]
    with pytest.raises(PydanticSerializationError):
        model = replace_reference(model, field, uri)
        model.model_dump_json()


@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
@pytest.mark.parametrize("container", ["skill-list", "file-edits", "ui-list", "ui-tree"])
def test_nested_reference_mutations_cannot_bypass_publication(
    references: dict[str, tuple[BaseModel, str, str]], mode: Mode, container: str,
) -> None:
    model, field, _ = references["skill" if container == "skill-list" else "file-edit"]
    result: BaseModel
    if container == "skill-list":
        assert isinstance(model, GatewaySkillSourceEntryDTO)
        result = GatewaySkillSourcesDTO(gateway_id=GATEWAY, workspace_id=WORKSPACE, revision="revision", entries=(model,), available=True)
        result = result.model_copy(update={"entries": (replace_reference(model, field, NATIVE[0]),)})
    elif container == "file-edits":
        assert isinstance(model, FileEditPayload)
        result = ToolCallEndPayload(execution_id="exe", tool_call_id="call", tool_name="fixture", file_edits=[model])
        result.file_edits[0] = model.model_copy(update={field: NATIVE[0]})
    else:
        layout, tree = references["ui-previews"][0], references["ui-expanded"][0]
        assert isinstance(layout, WebUILayoutSettingsDTO)
        assert isinstance(tree, WebUIWorkspaceFileTreeSettingsDTO)
        result = WebUISettingsDTO(layout=layout, workspace_file_tree=tree)
        if container == "ui-list":
            assert result.layout.workspace_preview_file_paths is not None
            result.layout.workspace_preview_file_paths.append(NATIVE[0])
        else:
            result.workspace_file_tree.expanded_paths_by_workspace[WORKSPACE].append(NATIVE[0])
    with pytest.raises(PydanticSerializationError):
        publish(result, mode)


@pytest.mark.parametrize("factory", [
    lambda: TextMutationReceiptDTO(status="error", effect="not_executed"),
    lambda: WebUILayoutSettingsDTO(),
    lambda: TaskTriggerDTO(version="1", type_id="builtin.manual"),
])
def test_nullable_references_remain_null(factory: Callable[[], BaseModel]) -> None:
    model = factory()
    assert type(model).model_validate_json(model.model_dump_json()) == model
