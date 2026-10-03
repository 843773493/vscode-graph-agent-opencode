"""离线 OpenAPI 镜像快照的唯一来源与读取实现。

契约层有多个测试冻结同一组离线镜像；快照清单与读取方式必须只有一处定义，
否则新增/改名镜像时会出现只更新部分文件的漏改。
"""

from __future__ import annotations

import json
from pathlib import Path

OPENAPI_SNAPSHOTS = (
    "src/clients/web/openapi.json",
    "src/clients/web/src/types/openapi/index.json",
)


def load_openapi_snapshot(snapshot: str) -> dict[str, object]:
    return json.loads((Path.cwd() / snapshot).read_text(encoding="utf-8"))


__all__ = ["OPENAPI_SNAPSHOTS", "load_openapi_snapshot"]
