from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.core.errors import ErrorCode, ProviderError


def retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError):
            return None


async def request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """No automatic retries. The durable queue owns retry and provider cooldown policy."""
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderError(ErrorCode.TIMEOUT, "Request timeout") from exc
    except httpx.RequestError as exc:
        raise ProviderError(ErrorCode.NETWORK_ERROR, "Network request failed") from exc
    if response.status_code == 429:
        raise ProviderError(
            ErrorCode.RATE_LIMITED, "Rate limited", retry_after(response.headers.get("retry-after"))
        )
    if response.status_code == 403:
        raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Access refused")
    if response.status_code == 401:
        raise ProviderError(ErrorCode.TOKEN_EXPIRED, "Authentication required")
    if response.is_error:
        raise ProviderError(ErrorCode.HTTP_ERROR, f"HTTP {response.status_code}")
    if "text/html" in response.headers.get("content-type", ""):
        body = response.text[:10000].lower()
        if any(
            marker in body
            for marker in ("verify you are human", "access denied", "cf-chl-", "captcha challenge")
        ):
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Challenge page returned")
    return response
