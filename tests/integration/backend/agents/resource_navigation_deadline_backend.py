"""仅供导航期限验收：真实解析入口阻塞，并记录目标文件是否被读取。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.main import app
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)

__all__ = ["app"]

_parse = ResourceNavigation._parse_target
_navigate = ResourceNavigation.navigate
_read = ResourceNavigation._read


def _record(owner, stage, **facts):
    artifacts = Path(os.environ["BOXTEAM_TEST_NAVIGATION_ARTIFACTS"])
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / f"{owner._workspace_id}-{stage}.json").write_text(json.dumps(facts), encoding="utf-8")


def _slow_parse(owner, request, gateway_id):
    if request.uri.endswith("/deadline-probe.txt"):
        time.sleep(1)
    return _parse(owner, request, gateway_id)


async def _observed_navigation(owner, request, **options):
    if not request.uri.endswith("/deadline-probe.txt"):
        return await _navigate(owner, request, **options)
    _record(owner, "entered", timeout_ms=request.timeout_ms,
            remaining=options["deadline"] - time.monotonic(), uri=request.uri)
    try:
        return await _navigate(owner, request, **options)
    finally:
        _record(owner, "finished")


def _observed_read(owner, path, node, request, deadline):
    if path.name == "deadline-probe.txt":
        _record(owner, "read-attempted")
    return _read(owner, path, node, request, deadline)


ResourceNavigation._parse_target = _slow_parse
ResourceNavigation.navigate = _observed_navigation
ResourceNavigation._read = _observed_read
