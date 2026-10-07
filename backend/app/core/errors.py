from enum import StrEnum


class ErrorCode(StrEnum):
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    BROWSER_STARTUP_FAILED = "BROWSER_STARTUP_FAILED"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED_BY_ANTIBOT = "BLOCKED_BY_ANTIBOT"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    PROVIDER_CHANGED = "PROVIDER_CHANGED"
    DIRECTORY_REDIRECT = "DIRECTORY_REDIRECT"
    BOOKING_WINDOW_CLOSED = "BOOKING_WINDOW_CLOSED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class ProviderError(Exception):
    def __init__(self, code: ErrorCode, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
