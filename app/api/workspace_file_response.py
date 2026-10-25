"""Workspace 下载沿用 Starlette 的 Range/响应头实现，收口延迟 I/O 与临时文件生命周期。"""

from __future__ import annotations

import sys
from typing import Literal

import anyio
from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from app.services.infrastructure.workspace_files.filesystem import (
    PreparedFileDownload,
    file_access,
)


class WorkspaceFileResponse(FileResponse):
    def __init__(self, download: PreparedFileDownload, *, disposition: Literal["inline", "attachment"]) -> None:
        self.download = download
        super().__init__(
            download.path,
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
                await super().__call__({**scope, "extensions": extensions}, receive, send_file)
        finally:
            if self.download.temporary:
                primary_error = sys.exception()
                try:
                    with anyio.CancelScope(shield=True), file_access(self.download.uri, "临时文件清理"):
                        await anyio.to_thread.run_sync(self.download.path.unlink)
                except OSError as cleanup_error:
                    if primary_error is not None:
                        raise BaseExceptionGroup("文件传输失败且临时文件清理失败", [primary_error, cleanup_error]) from None
                    raise
