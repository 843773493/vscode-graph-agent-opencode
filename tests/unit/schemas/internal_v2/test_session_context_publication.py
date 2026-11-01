"""上下文引用在复制或嵌套修改后仍须遵守发布合同，正文不作为地址校验。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextProvenanceRefDTO,
    SessionContextReadResultDTO,
    SessionContextResourceDTO,
    SessionContextSearchMatchDTO,
    SessionContextSearchResultDTO,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceIdentity,
)

URI = "boxteam://workspace/context-publication/resources/files/root/%E4%B8%AD%E6%96%87%20file.txt"
IDENTITY = ResourceIdentity("context-publication")
REVISION = "sha256:jcs:v1:" + "a" * 64
NATIVE = ["/private/context-publication", r"C:\private\context-publication",
          r"\\target\private\context-publication", "file:///private/context-publication"]
Field = Literal["resource", "item", "read", "match", "search", "read.item", "read.resource", "search.match"]
FIELDS: tuple[Field, ...] = ("resource", "item", "read", "match", "search", "read.item", "read.resource", "search.match")


@pytest.fixture
def context_results() -> tuple[SessionContextReadResultDTO, SessionContextSearchResultDTO]:
    resource = SessionContextResourceDTO(
        display_uri=URI, resource_kind="file", owner_scope="workspace", facet="content",
        revision=REVISION, availability="available", provenance_ref=SessionContextProvenanceRefDTO(token=REVISION),
    )
    item = SessionContextItemDTO(
        kind="assembly", uri=URI, text=NATIVE[0], resource_refs=[resource],
        tool_calls=[{"args": {"path": NATIVE[1]}}], tool_results=[{"content": NATIVE[2]}],
    )
    match = SessionContextSearchMatchDTO(
        resource_identity=IDENTITY, uri=URI, scope="workspace", preview=NATIVE[0],
        source="effective_context", revision=REVISION, match_start=0, match_end=1,
    )
    return (
        SessionContextReadResultDTO(resource_identity=IDENTITY, uri=URI, scope="workspace", view="assembly", revision=REVISION, items=[item]),
        SessionContextSearchResultDTO(resource_identity=IDENTITY, uri=URI, scope="workspace", query=NATIVE[0], match_mode="literal", revision=REVISION, matches=[match]),
    )


def copied_reference(
    results: tuple[SessionContextReadResultDTO, SessionContextSearchResultDTO], field: Field, value: str,
) -> BaseModel:
    read, search = results
    item = read.items[0]
    resource = item.resource_refs[0]
    match = search.matches[0]
    if field == "resource":
        return resource.model_copy(update={"display_uri": value})
    if field == "item":
        return item.model_copy(update={"uri": value})
    if field == "read":
        return read.model_copy(update={"uri": value})
    if field == "match":
        return match.model_copy(update={"uri": value})
    if field == "search":
        return search.model_copy(update={"uri": value})
    if field == "search.match":
        return search.model_copy(update={"matches": [match.model_copy(update={"uri": value})]})
    if field == "read.resource":
        item = item.model_copy(update={"resource_refs": [resource.model_copy(update={"display_uri": value})]})
    else:
        item = item.model_copy(update={"uri": value})
    return read.model_copy(update={"items": [item]})


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_context_reference_cannot_publish_native_paths(
    context_results: tuple[SessionContextReadResultDTO, SessionContextSearchResultDTO],
    field: Field, native: str, mode: Literal["python", "json", "json-text"],
) -> None:
    before = tuple(result.model_dump_json() for result in context_results)
    copied = copied_reference(context_results, field, native)
    with pytest.raises(PydanticSerializationError) as error:
        if mode == "json-text":
            copied.model_dump_json()
        else:
            copied.model_dump(mode=mode)
    assert native not in str(error.value)
    assert tuple(result.model_dump_json() for result in context_results) == before


@pytest.mark.parametrize("uri", [URI, URI.replace("file.txt", "%66ile.txt"),
                                 "boxteam://gateway_" + "b" * 32 + "/workspace/context-publication/resources/files/root/missing.txt"])
def test_legal_copied_context_references_preserve_original_bytes(
    context_results: tuple[SessionContextReadResultDTO, SessionContextSearchResultDTO], uri: str,
) -> None:
    for field in FIELDS:
        dumped = copied_reference(context_results, field, uri).model_dump(mode="json")
        owner = dumped
        if field.startswith("read."):
            owner = owner["items"][0]
            if field == "read.resource":
                owner = owner["resource_refs"][0]
        elif field == "search.match":
            owner = owner["matches"][0]
        assert owner["display_uri" if field in {"resource", "read.resource"} else "uri"] == uri
    read, search = (result.model_dump(mode="json") for result in context_results)
    assert read["items"][0]["text"] == search["matches"][0]["preview"] == search["query"] == NATIVE[0]
    assert read["items"][0]["tool_calls"][0]["args"]["path"] == NATIVE[1]
    assert read["items"][0]["tool_results"][0]["content"] == NATIVE[2]


def test_mutated_nested_reference_is_rejected_and_nullable_fields_keep_their_contract(
    context_results: tuple[SessionContextReadResultDTO, SessionContextSearchResultDTO],
) -> None:
    read, _ = context_results
    resource = read.items[0].resource_refs[0]
    resource.display_uri = None
    read.items[0].uri = None
    assert read.model_dump(mode="json")["items"][0]["resource_refs"][0]["display_uri"] is None
    assert read.model_dump(mode="json")["items"][0]["uri"] is None
    missing = resource.model_dump(exclude={"display_uri"})
    with pytest.raises(ValidationError, match="Field required"):
        SessionContextResourceDTO.model_validate(missing)
    resource.display_uri = NATIVE[0]
    with pytest.raises(PydanticSerializationError):
        read.model_dump_json()
