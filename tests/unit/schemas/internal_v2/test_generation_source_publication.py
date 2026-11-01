"""会话生成来源在构造、复制和嵌套发布时保持同一 URI 合同。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.persistent_task import PersistentTaskConfigurationDTO
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationActionConfigDTO,
)

THREAD = thread_resource_uri("ws_source", "ses_source", "thr_source")
SESSION = THREAD.split("/threads/")[0]
INVALID = ("/private/generation-source", r"C:\private\generation-source",
           r"\\target\private\generation-source", "file:///private/generation-source",
           "relative/source", "", THREAD + "?revision=1",
           "boxteam://workspace/ws_source/resources/files/root/source.txt")


@pytest.fixture
def generation() -> SessionGenerationActionConfigDTO:
    return SessionGenerationActionConfigDTO.model_validate({
        "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
        "config": {"prompt": INVALID[0]},
        "placement": {"kind": "workspace", "workspace_id": "ws_source"},
        "context_source": {"kind": "fresh"}, "session_strategy": {"mode": "new_per_run"},
        "created_from_uri": THREAD,
    })


@pytest.mark.parametrize("source", INVALID)
def test_source_construction_rejects_native_and_other_resources(
    generation: SessionGenerationActionConfigDTO, source: str,
) -> None:
    with pytest.raises(ValidationError) as rejected:
        type(generation).model_validate({**generation.model_dump(), "created_from_uri": source})
    assert "generation-source" not in str(rejected.value)


@pytest.mark.parametrize("source", INVALID)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_source_cannot_bypass_nested_publication(
    generation: SessionGenerationActionConfigDTO, source: str,
    mode: Literal["python", "json", "json-text"],
) -> None:
    configuration = PersistentTaskConfigurationDTO.model_validate({
        "name": "生成任务", "trigger": {"type_id": "builtin.manual", "version": "1"},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": INVALID[1]},
        "action": {"type_id": "builtin.session_generation", "version": "1", "generation": generation},
    })
    before = configuration.model_dump_json()
    configuration = configuration.model_copy(update={"action": configuration.action.model_copy(update={
        "generation": generation.model_copy(update={"created_from_uri": source}),
    })})
    with pytest.raises(PydanticSerializationError) as rejected:
        if mode == "json-text":
            configuration.model_dump_json()
        else:
            configuration.model_dump(mode=mode)
    assert "generation-source" not in str(rejected.value)
    assert generation.created_from_uri == THREAD
    assert INVALID[0] in before


@pytest.mark.parametrize("source", [None, SESSION, THREAD, THREAD.replace("boxteam://", "boxteam://gateway_remote/")])
def test_dangling_source_and_user_body_are_preserved(
    generation: SessionGenerationActionConfigDTO, source: str | None,
) -> None:
    value = type(generation).model_validate({**generation.model_dump(), "created_from_uri": source})
    restored = type(value).model_validate_json(value.model_copy().model_dump_json())
    assert restored == value
    assert restored.created_from_uri == source and restored.config["prompt"] == INVALID[0]
