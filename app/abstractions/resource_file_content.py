"""实际资源 owner 返回的只读正文；定位与授权仍由 owner 负责。"""
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceFileContent:
    data: bytes
    content_type: str


ResourceFileReader = Callable[[str, int], ResourceFileContent]
