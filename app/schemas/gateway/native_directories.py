"""原生目录控制链；native_root 仅在认证同机 owner 之间临时传递。"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer

from app.core.resource_uri import parse_resource_uri


def _native_directory_root_uri(value: str) -> str:
    parsed = parse_resource_uri(value)
    if (parsed.scope != "gateway" or parsed.kind != "files"
            or parsed.gateway_authority is not None or len(parsed.tail_segments) != 1):
        raise ValueError("原生目录控制回执必须是本机 Gateway files 根 URI")
    return parsed.display_uri


NativeDirectoryRootUri = Annotated[
    str, AfterValidator(_native_directory_root_uri), PlainSerializer(_native_directory_root_uri),
]


class NativeDirectoryBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    native_root: str


class NativeDirectoryBindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    uri: NativeDirectoryRootUri


class NativeDirectoryResolveDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    uri: NativeDirectoryRootUri
    native_root: str
