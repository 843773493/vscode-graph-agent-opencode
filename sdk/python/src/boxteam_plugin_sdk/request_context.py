"""不依赖 Web 框架的 request_id 执行上下文。"""

from contextvars import ContextVar, Token

_CURRENT_REQUEST_ID: ContextVar[str | None] = ContextVar("boxteam_current_request_id", default=None)


def set_current_request_id(request_id: str | None) -> Token[str | None]:
    return _CURRENT_REQUEST_ID.set(request_id)


def reset_current_request_id(token: Token[str | None]) -> None:
    _CURRENT_REQUEST_ID.reset(token)


def get_current_request_id() -> str | None:
    return _CURRENT_REQUEST_ID.get()


def require_current_request_id(consumer: str) -> str:
    request_id = _CURRENT_REQUEST_ID.get()
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError(
            f"{consumer} 需要创建请求的权威 request_id，但当前作用域未绑定："
            "调用方必须在请求作用域内传入（或从持久化的 Job 记录携带）"
        )
    return request_id


__all__ = [
    "get_current_request_id",
    "require_current_request_id",
    "reset_current_request_id",
    "set_current_request_id",
]
