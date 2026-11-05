"""本地 HTTP 测试服务共用的 socket 和服务线程生命周期。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import HTTPServer
from threading import Thread
from typing import TypeVar

Server = TypeVar("Server", bound=HTTPServer)


@contextmanager
def running_http_server(server: Server) -> Iterator[Server]:
    """接管已绑定的服务，启动失败或任一清理失败也释放其余资源。"""
    with ExitStack() as cleanup:
        cleanup.callback(server.server_close)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def join_thread() -> None:
            thread.join(timeout=5)
            assert not thread.is_alive(), "测试 HTTP 服务线程未停止"

        # 未启动的线程不能 shutdown；成功启动后按停机、join、关闭 socket 释放。
        cleanup.callback(join_thread)
        cleanup.callback(server.shutdown)
        yield server
