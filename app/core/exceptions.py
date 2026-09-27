from fastapi import HTTPException


class BaseAPIException(HTTPException):
    """工作区业务异常基类：语义状态码 + 面向调用方的可读文本。

    ``code`` 的前 3 位即该异常语义上的 HTTP 状态码（``403000`` → ``403``），
    这是本仓既有约定。``detail`` 内部形态是 ``{"code", "message", "details"}``
    字典，只用于携带结构化信息；对外契约必须走 :meth:`readable_message`，
    不得把字典 repr 或 ``str(error)``（形如 ``"403: {'code': 403000, ...}"``）
    直接下发。

    这样无论路由显式映射，还是异常冒泡到全局 ``BaseAPIException`` 处理器，
    都复用同一份状态码与文本抽取实现，漏接不再产生 500 + 内部字典泄漏。
    """

    code: int = 500000
    message: str = "internal server error"

    def __init__(self, details: dict | None = None):
        super().__init__(
            # code 前 3 位即语义状态码：403000 → 403、404000 → 404。
            status_code=self.code // 1000,
            detail={
                "code": self.code,
                "message": self.message,
                "details": details or {}
            }
        )

    def readable_message(self) -> str:
        """对外可读文本：``details`` 优先，其次 ``message``；绝不返回字典 repr。"""
        detail = self.detail
        if isinstance(detail, dict):
            text = detail.get("details") or detail.get("message")
            if text:
                return str(text)
        return str(detail)


class ForbiddenError(BaseAPIException):
    code = 403000
    message = "forbidden"


class NotFoundError(BaseAPIException):
    code = 404000
    message = "resource not found"
