"""确定性的 SQLite catalog 会话测试资源播种器。

该模块只服务测试数据准备，不模拟产品创建流程。需要验证
``SessionCreationService`` 的测试应直接调用该服务；依赖固定 canonical
``session_id`` 的存量基础设施测试则通过这里一次性写入完整的 catalog、
剥离 manifest 和 session-control 数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    validate_session_id,
)
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import load_or_create_workspace_id


@dataclass(frozen=True, slots=True)
class CatalogSessionBundleSpec:
    """固定会话资源的完整输入字段。"""

    sessions_root: Path
    session_id: str
    workspace_id: str
    title: str
    parent_node_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CatalogSessionBundle:
    """已播种会话资源及其权威节点投影。"""

    session_id: str
    main_thread_id: str
    directory: Path
    node: SessionCatalogNode


def _workspace_root(sessions_root: Path) -> Path:
    resolved = sessions_root.expanduser().resolve()
    if resolved.name == "sessions" and resolved.parent.name == ".boxteam":
        return resolved.parent.parent
    return resolved.parent


def _navigation_root(sessions_root: Path) -> Path:
    resolved = sessions_root.expanduser().resolve()
    if resolved.name == "sessions":
        return resolved.parent / "navigation"
    return resolved.parent / f".{resolved.name}-session-navigation"


def _default_created_at() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def seed_catalog_session(spec: CatalogSessionBundleSpec) -> CatalogSessionBundle:
    """写入一个固定 ID 的完整测试会话资源。

    该函数只使用 catalog store 的节点写入 API；不会调用已经下线的
    resolver allocate/register 接口，也不创建 staging 或 marker。
    """

    sessions_root = spec.sessions_root.expanduser().resolve()
    validate_session_id(spec.session_id)
    if not isinstance(spec.workspace_id, str) or not spec.workspace_id:
        raise ValueError("workspace_id 不能为空")
    if not isinstance(spec.title, str) or not spec.title:
        raise ValueError("title 不能为空")
    created_at = spec.created_at or _default_created_at()
    if created_at.tzinfo is None:
        raise ValueError("created_at 必须带时区")
    main_thread_id = f"thr_{uuid4().hex}"
    locator = (
        f"sessions/{created_at.astimezone(UTC):%Y/%m/%d}/{spec.session_id}"
    )
    directory = sessions_root / locator.removeprefix("sessions/")
    directory.parent.mkdir(parents=True, exist_ok=True)

    navigation_root = _navigation_root(sessions_root)
    store = SessionCatalogStore(
        navigation_root / "session-catalog.sqlite",
        sessions_root,
    )
    try:
        try:
            node = store.get_node(spec.session_id)
        except KeyError:
            node = store.create_session_node(
                spec.session_id,
                spec.workspace_id,
                spec.parent_node_id,
                spec.title,
                created_at,
                locator,
                main_thread_id,
            )
        else:
            if node.storage_relative_locator != locator:
                raise RuntimeError(
                    "固定会话 ID 已存在但 locator 不一致: "
                    f"session_id={spec.session_id}, "
                    f"existing={node.storage_relative_locator}, expected={locator}"
                )
            main_thread_id = node.main_thread_id or main_thread_id

        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "session_id": spec.session_id,
            "workspace_id": spec.workspace_id,
            "kind": "normal",
            "delegation": None,
            "generation_origin": None,
            "current_agent_id": "default",
            "current_provider_id": None,
            "context_source_session_id": None,
            "created_at": node.created_at or created_at.isoformat(),
            "updated_at": node.created_at or created_at.isoformat(),
        }
        (directory / "session.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        control = SessionControlStore(directory / "session-control.sqlite")
        try:
            control.initialize_main_thread(
                main_thread_id,
                datetime.fromisoformat(str(manifest["created_at"])),
            )
            control.initialize_fence("active", 1)
            control.verify_matches_catalog_main_thread(main_thread_id)
        finally:
            control.close()
        return CatalogSessionBundle(
            session_id=spec.session_id,
            main_thread_id=main_thread_id,
            directory=directory,
            node=node,
        )
    finally:
        store.close()


def seed_catalog_session_bundle(
    sessions_root: Path,
    session_id: str,
    *,
    workspace_id: str | None = None,
    title: str | None = None,
    parent_node_id: str | None = None,
) -> CatalogSessionBundle:
    """按测试常用参数构造并播种一个固定 ID 会话。"""

    resolved_root = sessions_root.expanduser().resolve()
    resolved_workspace_id = workspace_id or load_or_create_workspace_id(
        _workspace_root(resolved_root)
    )
    return seed_catalog_session(
        CatalogSessionBundleSpec(
            sessions_root=resolved_root,
            session_id=session_id,
            workspace_id=resolved_workspace_id,
            title=title or f"测试会话 {session_id}",
            parent_node_id=parent_node_id,
        )
    )


__all__ = [
    "CatalogSessionBundle",
    "CatalogSessionBundleSpec",
    "seed_catalog_session",
    "seed_catalog_session_bundle",
]
