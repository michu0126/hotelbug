"""Use the observed English quote API with the same private browser Cookie jar.

No guessed CN endpoint, cross-host Cookie header, denial fallback or session reset.
Catalog discovery and CN room/calendar flows remain existing browser collectors.
"""

import hashlib
import json

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.crawler.http import retry_after
from app.providers.marriott import MarriottProvider, parse_rates
from app.providers.marriott_browser import ROOT, MarriottBrowserProvider


class MarriottHybridProvider(MarriottBrowserProvider):
    async def search_rates(self, search):
        if self._root != ROOT:
            return await super().search_rates(search)
        if self._official_code and self._official_code != search.provider_hotel_id.upper():
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott configured property code mismatch")
        spec = MarriottProvider.query_request("rates", MarriottProvider.rate_variables(search))
        try:
            # new_provider_page applies the latest Cookie revision before this request.
            # The request context shares the jar; Cookie scopes/HttpOnly/expiry are
            # handled by Chromium, never concatenated into an all-domain header.
            page = await self._get_page()
            response = await page.context.request.post(
                spec["url"],
                headers=spec["headers"],
                data=spec["json"],
                timeout=45000,
                max_redirects=0,
            )
            try:
                if response.status == 429:
                    raise ProviderError(
                        ErrorCode.RATE_LIMITED,
                        "Marriott API rate limited",
                        retry_after(response.headers.get("retry-after")),
                    )
                if response.status == 403:
                    raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott API access refused")
                if response.status == 401 or 300 <= response.status < 400:
                    raise ProviderError(ErrorCode.TOKEN_EXPIRED, "Marriott official session needs refresh")
                if response.status >= 400:
                    raise ProviderError(ErrorCode.HTTP_ERROR, f"Marriott API HTTP {response.status}")
                raw = await response.body()
                if "text/html" in response.headers.get("content-type", "") and any(
                    marker in raw[:10000].lower()
                    for marker in (
                        b"access denied",
                        b"verify you are human",
                        b"captcha challenge",
                        b"cf-chl-",
                    )
                ):
                    raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott API challenge page")
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott API expected JSON") from None
                if not isinstance(body, dict):
                    raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott API expected an object")
                return parse_rates(body, search, hashlib.sha256(raw).hexdigest())
            finally:
                await response.dispose()
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Marriott official session API request failed",
            ) from None
