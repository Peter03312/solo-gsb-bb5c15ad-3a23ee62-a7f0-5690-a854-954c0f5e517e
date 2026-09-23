class APIError(Exception):
    """统一的 API 业务错误基类。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class ValidationError(APIError):
    status_code = 422
    code = "validation_error"


class ConflictError(APIError):
    status_code = 409
    code = "conflict"


class NotFoundError(APIError):
    status_code = 404
    code = "not_found"
