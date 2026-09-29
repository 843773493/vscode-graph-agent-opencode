"""``app/core/user_identity.py`` 专属单测：user scope 约定值是唯一来源。"""

from __future__ import annotations

from app.core.user_identity import user_scope_id
from app.services.infrastructure.resource_platform.virtual_resources import (
    ResolutionContext,
    parse_vrn,
    resource_display_uri,
)


def test_user_scope_id_is_single_user_convention_local() -> None:
    # owner change 定稿：``user`` scope 的 scope_id 是「单用户本地程序的约定值」
    # ``local``，MUST NOT 虚构用户名或 hostname 等细分身份。
    assert user_scope_id() == "local"


def test_user_scope_id_yields_grammar_valid_scope_id() -> None:
    # 输出必须能作为真实 VRN scope_id 被 parse_vrn 解析（接上真实 grammar）。
    scope_id = user_scope_id()
    uri = resource_display_uri(
        scope="user",
        scope_id=scope_id,
        kind="config",
        tail_segments=("workspace_mutable_override",),
    )
    parsed = parse_vrn(uri)
    assert parsed.scope == "user"
    assert parsed.scope_id == scope_id


def test_resolution_context_accepts_user_scope_id() -> None:
    context = ResolutionContext(user_scope_id=user_scope_id())
    assert context.user_scope_id == "local"
