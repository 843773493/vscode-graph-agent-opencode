"""child-thread 单测的隔离工作区与 SessionService。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace
from tests.unit.services.business.session_children.helpers import WORKSPACE_ID


@pytest.fixture()
def catalog_workspace_context(tmp_path: Path):
    context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def session_service(catalog_workspace_context) -> SessionService:
    """每个测试使用独立临时工作区，不触碰仓库根目录。"""
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(
            sessions_dir=catalog_workspace_context.sessions_root
        ),
        workspace_id=WORKSPACE_ID,
        path_resolver=catalog_workspace_context.resolver,
        creation_service=catalog_workspace_context.creation_service,
    )
