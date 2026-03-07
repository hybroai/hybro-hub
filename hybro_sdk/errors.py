"""Custom exception hierarchy for the Hybro SDK."""

from __future__ import annotations


class GatewayError(Exception):
    """Base exception for all Hybro Gateway errors."""

    def __init__(self, message: str, status_code: int | None = None, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


class AuthError(GatewayError):
    """Raised when the API key is missing, invalid, or inactive."""


class RateLimitError(GatewayError):
    """Raised when the request is rate-limited (HTTP 429)."""

    def __init__(self, message: str, retry_after: int | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class AgentNotFoundError(GatewayError):
    """Raised when the requested agent is not found or inactive (HTTP 404)."""


class AccessDeniedError(GatewayError):
    """Raised when the caller lacks access to the requested agent (HTTP 403)."""


class AgentCommunicationError(GatewayError):
    """Raised when the upstream agent fails to respond (HTTP 502)."""


def raise_for_status(
    status_code: int,
    *,
    body: dict | None = None,
    headers: dict | None = None,
    fallback_text: str = "",
) -> None:
    """Map an HTTP error status code to the appropriate SDK exception.

    This is intentionally a standalone function so it can be reused by both
    the normal request path and the SSE streaming path.
    """
    if 200 <= status_code < 300:
        return

    body = body or {}
    headers = headers or {}
    detail = body.get("detail", body)
    msg = (
        detail.get("message", fallback_text)
        if isinstance(detail, dict)
        else str(detail)
    ) or fallback_text or f"HTTP {status_code}"
    detail_dict = detail if isinstance(detail, dict) else {}

    if status_code == 401:
        raise AuthError(msg, status_code=401, detail=detail_dict)
    if status_code == 403:
        raise AccessDeniedError(msg, status_code=403, detail=detail_dict)
    if status_code == 404:
        raise AgentNotFoundError(msg, status_code=404, detail=detail_dict)
    if status_code == 429:
        retry_after = int(headers.get("Retry-After", headers.get("retry-after", 60)))
        raise RateLimitError(msg, retry_after=retry_after, status_code=429, detail=detail_dict)
    if status_code == 502:
        raise AgentCommunicationError(msg, status_code=502, detail=detail_dict)

    raise GatewayError(msg, status_code=status_code, detail=detail_dict)
