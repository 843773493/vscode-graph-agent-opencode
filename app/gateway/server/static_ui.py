from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

_MCP_OAUTH_CALLBACK_PATH = "/mcp-oauth-callback.html"


class _OAuthCallbackAccessFilter(logging.Filter):
    """保留原访问记录，唯独移除私有 OAuth 回调 query。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, protocol, status = record.args
            if isinstance(target, str) and target.partition("?")[0] == _MCP_OAUTH_CALLBACK_PATH:
                record.args = (client, method, _MCP_OAUTH_CALLBACK_PATH, protocol, status)
        return True


class SpaStaticFiles(StaticFiles):
    """提供静态资源，并把非 API 的前端路由回退到 index.html。"""

    def __init__(self, *, directory: Path) -> None:
        super().__init__(directory=directory, html=True, check_dir=True)
        manifest = directory / "immutable-assets.json"
        assets = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
        if not isinstance(assets, list) or not all(isinstance(path, str) for path in assets):
            raise ValueError(f"静态资源缓存清单必须是路径列表: {manifest}")
        self.immutable_assets = frozenset(assets)

    async def get_response(self, path: str, scope):
        normalized_path = path.replace("\\", "/")
        if normalized_path == "api" or normalized_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        try:
            response = await super().get_response(normalized_path, scope)
        except HTTPException as error:
            if error.status_code != 404 or Path(normalized_path).suffix or normalized_path.split("/")[0] in {
                "out", "assets", "extensions", "node_modules",
            }:
                raise
            response = await super().get_response("index.html", scope)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if normalized_path in self.immutable_assets else "no-cache"
        )
        if normalized_path == _MCP_OAUTH_CALLBACK_PATH.lstrip("/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response


def install_static_web_ui(
    app: FastAPI,
    *,
    assets_root: Path | None = None,
) -> bool:
    configured_root = assets_root
    if configured_root is None:
        raw_root = os.environ.get("BOXTEAM_WEB_ASSETS")
        if not raw_root:
            return False
        configured_root = Path(raw_root)

    resolved_root = configured_root.expanduser().resolve()
    index_path = resolved_root / "index.html"
    if not resolved_root.is_dir():
        raise FileNotFoundError(f"Gateway Web UI 资源目录不存在: {resolved_root}")
    if not index_path.is_file():
        raise FileNotFoundError(f"Gateway Web UI 缺少 index.html: {index_path}")

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _OAuthCallbackAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(_OAuthCallbackAccessFilter())
    app.mount(
        "/",
        SpaStaticFiles(directory=resolved_root),
        name="boxteam-web-ui",
    )
    return True
