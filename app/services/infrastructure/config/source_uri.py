"""配置文件 URI 的唯一构造点；逻辑来源层与实际文件载体分开。

尾段使用不含扩展名的逻辑文件名，统一走公共 grammar。运行时来源仍只为
可寻址文件提供 URI；共享 SQLite carrier 与无文件的 runtime override 不造地址。
离线诊断可以引用实际读取的用户或工作区 JSONC 文件，不改变 ConfigSource 合同。
"""

from __future__ import annotations

from app.core.distribution_identity import load_distribution_id
from app.core.resource_uri import resource_display_uri

__all__ = ["config_file_uri", "extension_configuration_uri", "inline_config_source_uri"]


def config_file_uri(*, scope: str, scope_id: str, logical_name: str) -> str:
    """按真实归属构造单个配置文件的地址，不访问或注册原生文件。"""
    return resource_display_uri(
        scope=scope,
        scope_id=scope_id,
        kind="config",
        tail_segments=(logical_name,),
    )


def inline_config_source_uri(*, logical_name: str) -> str:
    """发行文件沿既有 manifest owner 推导身份；缺失时明确失败。"""
    return config_file_uri(
        scope="inline",
        scope_id=load_distribution_id(),
        logical_name=logical_name,
    )


def extension_configuration_uri(*, gateway_id: str, backend_id: str,
                                profile_id: str | None = None, workspace_id: str | None = None) -> str:
    """目标原生配置的具名地址；不开放任意文件或 BOXTEAM_HOME 路径。"""
    if (profile_id is None) == (workspace_id is None):
        raise ValueError("扩展配置地址必须明确一个 profile 或稳定工作区视图")
    if profile_id is not None:
        tail = ("profiles", profile_id, "settings")
    else:
        assert workspace_id is not None
        tail = ("views", workspace_id, "workspace")
    return resource_display_uri(scope="gateway", scope_id=gateway_id, kind="config",
        tail_segments=("extension-host", backend_id, *tail), gateway_authority=gateway_id)
