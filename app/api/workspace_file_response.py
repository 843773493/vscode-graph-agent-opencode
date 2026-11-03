"""Workspace 下载沿用 Starlette 的 Range/响应头实现，收口延迟 I/O 与临时文件生命周期。"""

from __future__ import annotations

import hashlib
import sys
from typing import Literal

import anyio
from anyio import to_thread
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.types import Message, Receive, Scope, Send

from app.core.atomic_fs import native_io_path
from app.services.infrastructure.workspace_files.filesystem import (
    PreparedFileDownload,
    file_access,
)


class WorkspaceFileResponse(FileResponse):
    def __init__(self, download: PreparedFileDownload, *, disposition: Literal["inline", "attachment"],
                 preview_owner: str | None = None) -> None:
        self.download = download
        self.preview_owner = preview_owner if disposition == "inline" and not download.temporary else None
        super().__init__(
            native_io_path(download.path),
            stat_result=download.metadata,
            media_type=download.media_type,
            filename=download.filename,
            content_disposition_type=disposition,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pending_start: Message | None = None

        async def send_file(message: Message) -> None:
            nonlocal pending_start
            if message["type"] == "http.response.start":
                pending_start = message
                return
            # 首次打开、读取失败时尚未发头，TraceMiddleware 可返回原请求的错误。
            if pending_start is not None:
                await send(pending_start)
                pending_start = None
            await send(message)

        # 原生 I/O 必须经过本 owner，不能交给服务器 pathsend 后再失去错误边界。
        extensions = {key: value for key, value in scope.get("extensions", {}).items()
                      if key != "http.response.pathsend"}
        try:
            with file_access(self.download.uri, "文件传输"):
                headers = Headers(scope=scope)
                if self.preview_owner is not None and scope["method"] == "GET" and "range" not in headers:
                    metadata = self.download.metadata
                    # 只散列身份与 stat 观察值；这是弱校验器，不是正文 hash 或保存 CAS。
                    observation = (self.preview_owner, self.download.uri, metadata.st_dev, metadata.st_ino,
                                   metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                    etag = 'W/"' + hashlib.sha256(repr(observation).encode()).hexdigest() + '"'
                    self.headers["ETag"] = etag
                    self.headers["Cache-Control"] = "private, no-cache"
                    self.headers.add_vary_header(
                        "X-BoxTeam-Workspace-Id, X-BoxTeam-Backend-Workspace-Id, X-BoxTeam-Gateway-Id, "
                        "X-Local-Token, X-BoxTeam-Federation-Token, Cookie"
                    )
                    candidates = {value.strip().removeprefix("W/") for value in headers.get("if-none-match", "").split(",")}
                    if "*" in candidates or etag.removeprefix("W/") in candidates:
                        # stat 可见不表示仍可读取；空读打开检查当前权限，不读取正文。
                        async with await anyio.open_file(self.path, "rb"):
                            pass
                        await Response(status_code=304, headers={key: self.headers[key]
                            for key in ("ETag", "Cache-Control", "Vary")})(scope, receive, send)
                        return
                await super().__call__({**scope, "extensions": extensions}, receive, send_file)
        finally:
            if self.download.temporary:
                primary_error = sys.exception()
                try:
                    with anyio.CancelScope(shield=True), file_access(self.download.uri, "临时文件清理"):
                        await to_thread.run_sync(native_io_path(self.download.path).unlink)
                except OSError as cleanup_error:
                    if primary_error is not None:
                        raise BaseExceptionGroup("文件传输失败且临时文件清理失败", [primary_error, cleanup_error]) from None
                    raise
