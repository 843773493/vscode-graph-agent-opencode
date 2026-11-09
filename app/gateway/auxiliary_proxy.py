from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.env import get_project_root

router = APIRouter()

def _terminal_asset(path: str) -> FileResponse:
    """Gateway 直接发布产品 attach 资产，不再启动第二个静态服务。"""
    root = get_project_root()
    client = root / "src/workspace-services/terminal/client"
    fixed = {
        "": client / "index.html", "index.html": client / "index.html",
        "main.js": client / "main.js", "styles.css": client / "styles.css",
        "terminalUserActions.js": client / "terminalUserActions.js",
        "terminalShortcuts.js": client / "terminalShortcuts.js",
        "shared/commandText.js": root / "src/workspace-services/terminal/shared/commandText.js",
        "protocol/messages.js": root / "src/workspace-services/terminal/protocol/messages.js",
        "vendor/xterm/xterm.css": root / "node_modules/@xterm/xterm/css/xterm.css",
        "vendor/xterm/xterm.js": root / "node_modules/@xterm/xterm/lib/xterm.js",
        "vendor/xterm/addon-fit.js": root / "node_modules/@xterm/addon-fit/lib/addon-fit.js",
        "vendor/xterm/addon-search.js": root / "node_modules/@xterm/addon-search/lib/addon-search.js",
        "vendor/codicon/codicon.css": root / "src/clients/web/node_modules/@vscode/codicons/dist/codicon.css",
        "vendor/codicon/codicon.ttf": root / "src/clients/web/node_modules/@vscode/codicons/dist/codicon.ttf",
    }
    target = fixed.get(path)
    if target is None:
        if path.startswith("protocol/generated/"):
            source = root / "src/workspace-services/protocol/generated"
            relative = path.removeprefix("protocol/generated/")
        elif path.startswith("@bufbuild/protobuf/"):
            source = root / "node_modules/@bufbuild/protobuf/dist/esm"
            relative = path.removeprefix("@bufbuild/protobuf/")
            if not relative or "." not in relative.rsplit("/", 1)[-1]:
                relative = relative.rstrip("/") + "/index.js"
                relative = relative.lstrip("/")
        else:
            raise HTTPException(404, "未知 Terminal attach 资产")
        target = (source / relative).resolve()
        if not target.is_relative_to(source.resolve()) or target.suffix != ".js":
            raise HTTPException(404, "Terminal attach 资产越界或类型不支持")
    if not target.is_file():
        raise HTTPException(404, f"Terminal attach 资产缺失: {path}")
    return FileResponse(target)


def _proxy_attach_frontend(*, kind: str, path: str):
    if kind == "terminal":
        return _terminal_asset(path)
    if kind == "browser":
        return _browser_asset(path)
    raise HTTPException(status_code=404, detail=f"未知 attach 前端: {kind}")


def _browser_asset(path: str) -> FileResponse:
    """Gateway 直接发布 Browser attach 页面及固定产品资源。"""
    root = get_project_root()
    client = root / "src/workspace-services/browser/client"
    fixed = {
        "": client / "index.html", "index.html": client / "index.html",
        "main.js": client / "main.js", "styles.css": client / "styles.css",
        "browserClientUtils.js": client / "browserClientUtils.js",
        "browserCollaborationUi.js": client / "browserCollaborationUi.js",
        "browserDebugElementTree.js": client / "browserDebugElementTree.js",
        "browserElementContext.js": client / "browserElementContext.js",
        "browserInputEvents.js": client / "browserInputEvents.js",
        "browserModalUi.js": client / "browserModalUi.js",
        "browserShortcuts.js": client / "browserShortcuts.js",
        "browserToolbarEvents.js": client / "browserToolbarEvents.js",
        "vendor/codicon/codicon.css": root / "src/clients/web/node_modules/@vscode/codicons/dist/codicon.css",
        "vendor/codicon/codicon.ttf": root / "src/clients/web/node_modules/@vscode/codicons/dist/codicon.ttf",
        "protocol/messages.js": root / "src/workspace-services/browser/protocol/messages.js",
    }
    target = fixed.get(path)
    if target is None:
        if path.startswith("protocol/generated/"):
            source = root / "src/workspace-services/protocol/generated"
            relative = path.removeprefix("protocol/generated/")
        elif path.startswith("@bufbuild/protobuf/"):
            source = root / "node_modules/@bufbuild/protobuf/dist/esm"
            relative = path.removeprefix("@bufbuild/protobuf/")
            if not relative or "." not in relative.rsplit("/", 1)[-1]:
                relative = relative.rstrip("/") + "/index.js"
                relative = relative.lstrip("/")
        else:
            raise HTTPException(404, "未知 Browser attach 资产")
        target = (source / relative).resolve()
        if not target.is_relative_to(source.resolve()) or target.suffix != ".js":
            raise HTTPException(404, "Browser attach 资产越界或类型不支持")
    if not target.is_file():
        raise HTTPException(404, f"Browser attach 资产缺失: {path}")
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@router.get("/api/gateway/attach/protocol/{path:path}")
async def proxy_protocol_attach_asset(path: str):
    if path.startswith("generated/boxteam/terminal/"):
        kind = "terminal"
    elif path.startswith("generated/boxteam/browser/"):
        kind = "browser"
    elif path.startswith("generated/boxteam/common/"):
        # common 生成文件被两个 attach 客户端共享，使用任一静态协议前端回源即可。
        kind = "terminal"
    else:
        raise HTTPException(
            status_code=404,
            detail=(
                "无法从协议资源路径判断 attach 前端，"
                f"仅支持 terminal/browser generated namespace: {path}"
            ),
        )
    return _proxy_attach_frontend(kind=kind, path=f"protocol/{path}")


@router.get("/api/gateway/attach/{kind}/{path:path}")
async def proxy_attach_frontend(kind: str, path: str):
    return _proxy_attach_frontend(kind=kind, path=path)
