"""在真实文件导航前设置可释放屏障，只观察固定目标，不替换执行结果。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from app.main import app
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)

__all__ = ["app"]

_navigate = ResourceNavigation.navigate


async def _blocked_navigation(owner, request, **options):
    if request.uri.endswith("/lifecycle-probe.txt"):
        artifacts = Path(os.environ["BOXTEAM_TEST_NAVIGATION_ARTIFACTS"])
        entered = artifacts / (owner._workspace_id + "-entered.json")
        pending = entered.with_suffix(".tmp")
        pending.write_text(json.dumps({
            "uri": request.uri, "gateway_id": options["local_gateway_id"], "workspace_id": owner._workspace_id,
        }), encoding="utf-8")
        pending.replace(entered)
        while not (artifacts / "release").is_file():
            await asyncio.sleep(0.01)
    return await _navigate(owner, request, **options)


ResourceNavigation.navigate = _blocked_navigation
