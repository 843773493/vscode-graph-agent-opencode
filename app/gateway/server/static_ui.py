from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles


class SpaStaticFiles(StaticFiles):
    """提供静态资源，并把非 API 的前端路由回退到 index.html。"""

    async def get_response(self, path: str, scope):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


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

    app.mount(
        "/",
        SpaStaticFiles(directory=resolved_root, html=True, check_dir=True),
        name="boxteam-web-ui",
    )
    return True
