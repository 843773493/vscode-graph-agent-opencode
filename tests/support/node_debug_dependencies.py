"""Node 调试测试的显式依赖替身。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugThreadOwner,
    normalize_node_debug_owner,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

TEST_THREAD_ID = "thr_00000000000070008000000000000000"


def node_debug_file_paths(workspace_root: Path) -> WorkspaceFilePaths:
    """为测试显式注入工作区文件 owner，不伪造 URI 身份或原生根绑定。"""
    root = workspace_root.resolve()
    return WorkspaceFilePaths(
        workspace=root, workspace_id=load_or_create_workspace_id(root), bindings=None,
    )


class PermissiveNodeDebugSessionAdmission:
    """只归一 owner 的测试准入替身。

    不涉及 Session 生命周期的测试使用此替身，避免把测试场景隐式改成真实目录
    注册流程；验证生命周期的测试应直接注入 ``NodeDebugSessionAdmission``。
    """

    @asynccontextmanager
    async def admit(
        self,
        session_id: str,
        thread_id: str,
    ) -> AsyncIterator[NodeDebugThreadOwner]:
        if thread_id is None:
            raise ValueError("测试调试 owner 必须显式提供 thread_id")
        owner_session_id, owner_thread_id = normalize_node_debug_owner(
            session_id,
            thread_id,
        )
        yield NodeDebugThreadOwner(
            session_id=owner_session_id,
            thread_id=owner_thread_id,
            thread_node=Path.cwd(),
        )


def permissive_node_debug_session_admission() -> PermissiveNodeDebugSessionAdmission:
    """创建仅负责 owner 归一的测试准入替身。"""
    return PermissiveNodeDebugSessionAdmission()
