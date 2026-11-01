"""原生目录控制回执只能发布无 authority 的 Gateway files 根 URI。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.schemas.gateway.native_directories import (
    NativeDirectoryBindingDTO,
    NativeDirectoryBindRequest,
    NativeDirectoryResolveDTO,
)

ROOT = "boxteam://gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001"
NATIVE = "/private-native-root/selected"


@pytest.fixture(params=[NativeDirectoryBindingDTO, NativeDirectoryResolveDTO])
def reply_type(request: pytest.FixtureRequest) -> type[NativeDirectoryBindingDTO | NativeDirectoryResolveDTO]:
    return request.param


@pytest.fixture(params=[
    NATIVE, r"C:\private-native-root\selected", r"\\host\private-native-root\selected",
    "file:///private-native-root/selected", "relative/selected", "",
    ROOT + "/child", ROOT + "?revision=1",
    "boxteam://workspace/workspace_a/resources/files/root",
    "boxteam://gateway_remote/gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001",
    "boxteam://gateway/gateway_01900000000070008000000000000001/resources/config/gateway",
])
def invalid_uri(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def valid_body(reply_type: type[NativeDirectoryBindingDTO | NativeDirectoryResolveDTO]) -> dict[str, str]:
    body = {"uri": ROOT}
    if reply_type is NativeDirectoryResolveDTO:
        body["native_root"] = NATIVE
    return body


def test_native_reply_rejects_invalid_root_without_echoing_input(
    reply_type: type[NativeDirectoryBindingDTO | NativeDirectoryResolveDTO],
    valid_body: dict[str, str], invalid_uri: str,
) -> None:
    with pytest.raises(ValidationError) as rejected:
        reply_type.model_validate({**valid_body, "uri": invalid_uri})
    assert rejected.value.errors(include_input=False)[0]["loc"] == ("uri",)
    assert "input_value=" not in str(rejected.value)
    if invalid_uri:
        assert invalid_uri not in str(rejected.value)


@pytest.mark.parametrize("mutation", ["copy", "assign"])
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_native_reply_rechecks_mutated_uri_at_publication(
    reply_type: type[NativeDirectoryBindingDTO | NativeDirectoryResolveDTO],
    valid_body: dict[str, str], invalid_uri: str, mutation: str,
    mode: Literal["python", "json", "json-text"],
) -> None:
    original = reply_type.model_validate(valid_body)
    broken = original.model_copy(update={"uri": invalid_uri}) if mutation == "copy" else original.model_copy()
    if mutation == "assign":
        broken.uri = invalid_uri
    with pytest.raises(PydanticSerializationError):
        if mode == "json-text":
            broken.model_dump_json()
        else:
            broken.model_dump(mode=mode)
    assert original.model_dump() == valid_body
    assert broken.uri == invalid_uri


def test_native_reply_preserves_root_and_named_control_input(
    reply_type: type[NativeDirectoryBindingDTO | NativeDirectoryResolveDTO], valid_body: dict[str, str],
) -> None:
    result = reply_type.model_validate(valid_body)
    assert result.model_dump(mode="json") == valid_body
    assert reply_type.model_validate_json(result.model_dump_json()) == result
    assert result.model_copy(update={"uri": ROOT}).model_dump() == valid_body
    assert NativeDirectoryBindRequest(native_root=NATIVE).model_dump() == {"native_root": NATIVE}
