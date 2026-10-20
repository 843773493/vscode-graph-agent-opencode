"""原生目录控制链；native_root 仅在认证同机 owner 之间临时传递。"""

from pydantic import BaseModel, ConfigDict


class NativeDirectoryBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    native_root: str


class NativeDirectoryBindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str


class NativeDirectoryResolveDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str
    native_root: str
