from fastapi import HTTPException


class BaseAPIException(HTTPException):
    code: int = 500000
    message: str = "internal server error"

    def __init__(self, details: dict = None):
        super().__init__(
            status_code=500,
            detail={
                "code": self.code,
                "message": self.message,
                "details": details or {}
            }
        )


class ForbiddenError(BaseAPIException):
    code = 403000
    message = "forbidden"


class NotFoundError(BaseAPIException):
    code = 404000
    message = "resource not found"
