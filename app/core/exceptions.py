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


class NotFoundException(BaseAPIException):
    code = 404000
    message = "resource not found"


class InvalidRequestException(BaseAPIException):
    code = 400000
    message = "invalid request"


class UnauthorizedException(BaseAPIException):
    code = 401000
    message = "unauthorized"


class ForbiddenError(BaseAPIException):
    code = 403000
    message = "forbidden"


class NotFoundError(BaseAPIException):
    code = 404000
    message = "resource not found"
