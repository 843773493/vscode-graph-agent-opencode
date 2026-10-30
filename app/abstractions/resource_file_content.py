"""实际资源 owner 返回的只读正文；定位与授权仍由 owner 负责。

明确的权限拒绝在构造处用 mark_public_failure 登记公开说明；未登记的
PermissionError 保持原生失败，不能凭 errno 是否缺失取得原文公开许可。
"""
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceFileContent:
    data: bytes
    content_type: str


ResourceFileReader = Callable[[str, int], ResourceFileContent]
