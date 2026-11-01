"""资源导航只在引用字段拒绝真实路径，构造后复制也不能绕过发布边界。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationMatchDTO,
    ResourceNavigationNodeDTO,
    ResourceNavigationResultDTO,
)

URI = "boxteam://workspace/navigation-publication/resources/files/root/%E4%B8%AD%E6%96%87%20file.txt"
NATIVE = ["/private/navigation-publication", r"C:\private\navigation-publication",
          r"\\target\private\navigation-publication", "file:///private/navigation-publication"]
Field = Literal["node", "entries", "matches"]


@pytest.fixture
def navigation_result() -> ResourceNavigationResultDTO:
    node = ResourceNavigationNodeDTO(uri=URI, resource_shape="file", operations=("describe", "read"))
    match = ResourceNavigationMatchDTO(uri=URI, line=1, column=1, text=NATIVE[0], snippet_start_column=1)
    return ResourceNavigationResultDTO(node=node, entries=(node,), matches=(match,), content=NATIVE[0])


def copied_reference(result: ResourceNavigationResultDTO, field: Field, value: str) -> ResourceNavigationResultDTO:
    if field == "node":
        return result.model_copy(update={"node": result.node.model_copy(update={"uri": value})})
    items = getattr(result, field)
    return result.model_copy(update={field: (items[0].model_copy(update={"uri": value}),)})


@pytest.mark.parametrize("field", ["node", "entries", "matches"])
@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_navigation_reference_is_rejected_at_publication(
    navigation_result: ResourceNavigationResultDTO, field: Field, native: str,
    mode: Literal["python", "json", "json-text"],
) -> None:
    before = navigation_result.model_dump_json()
    copied = copied_reference(navigation_result, field, native)
    with pytest.raises(PydanticSerializationError, match="导航资源引用必须是资源 URI") as error:
        if mode == "json-text":
            copied.model_dump_json()
        else:
            copied.model_dump(mode=mode)
    assert native not in str(error.value)
    assert navigation_result.model_dump_json() == before


@pytest.mark.parametrize("native", NATIVE)
def test_both_navigation_reference_types_reject_native_construction(native: str) -> None:
    for model, payload in (
        (ResourceNavigationNodeDTO, {"resource_shape": "file", "operations": ("read",)}),
        (ResourceNavigationMatchDTO, {"line": 1, "column": 1, "text": native, "snippet_start_column": 1}),
    ):
        with pytest.raises(ValidationError, match="导航资源引用必须是资源 URI") as error:
            model.model_validate({"uri": native, **payload})
        assert native not in str(error.value)


@pytest.mark.parametrize("uri", [URI, URI.replace("file.txt", "%66ile.txt"),
                                 "boxteam://gateway_" + "b" * 32 + "/workspace/navigation-publication/resources/files/root/missing.txt"])
def test_legal_dangling_and_encoded_references_preserve_bytes(
    navigation_result: ResourceNavigationResultDTO, uri: str,
) -> None:
    for field in ("node", "entries", "matches"):
        copied = copied_reference(navigation_result, field, uri)
        dumped = copied.model_dump(mode="json")
        entry = dumped[field] if field == "node" else dumped[field][0]
        assert entry["uri"] == uri
        assert dumped["content"] == dumped["matches"][0]["text"] == NATIVE[0]
