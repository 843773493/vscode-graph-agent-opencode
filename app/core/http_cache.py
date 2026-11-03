"""让指定 HTTP 资源入口的失败响应禁止缓存，不干预成功响应的 owner 策略。"""

from starlette.datastructures import MutableHeaders
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class NoStoreErrorsMiddleware:
    def __init__(self, app: ASGIApp, *, paths: tuple[str, ...]) -> None:
        self.app = app
        self.paths = tuple(compile_path(path)[0] for path in paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not any(path.fullmatch(scope["path"]) for path in self.paths):
            await self.app(scope, receive, send)
            return

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] >= 400:
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_response)
