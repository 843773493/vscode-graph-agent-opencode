"""VRN ``user`` scope 的身份推导：单用户本地程序约定值。

``user`` scope 的 ``scope_id`` 由 owner change
``add-unified-virtual-resource-addressing`` 的 requirement「scope 必须取自定稿闭集且
scope_id 对所有 scope 必填」定稿为**单用户本地程序的约定值** ``local``：本仓库按
AGENTS.md 明确无云服务、无多租户，用户身份不细分，MUST NOT 虚构用户名或 hostname 等
细分身份（``add-unified-virtual-resource-addressing`` 的 Scenario「user scope 的
scope_id 是单用户约定值 local」）。

该约定值集中在此**唯一一处**：与 ``inline`` 的 ``app/core/distribution_identity.py::
load_distribution_id``、``gateway`` 的真实 gateway_id 推导同族，调用方引用本函数而非
各自散写 ``"local"`` 字面量（owner 裁定 U-3）。
"""

from __future__ import annotations

# 单用户本地程序约定值；与 ``gateway`` 的实际取值为 ``local`` 不构成冲突——scope 是
# 不同命名空间，MUST NOT 因同名而合并两个 scope（见 resolver 的 ``require_scope_binding``）。
_USER_SCOPE_ID: str = "local"

__all__ = ["user_scope_id"]


def user_scope_id() -> str:
    """返回 ``user`` scope 的 ``scope_id``（单用户本地程序约定值 ``local``）。"""
    return _USER_SCOPE_ID
