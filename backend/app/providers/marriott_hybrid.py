"""Call the observed English quote operation inside the official-site page.

No guessed CN endpoint, cross-host Cookie header, denial fallback or session reset.
Catalog discovery and CN room/calendar flows remain existing browser collectors.
"""

import hashlib
import json
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.crawler.http import retry_after
from app.providers.marriott import MarriottProvider, parse_rates
from app.providers.marriott_browser import ROOT, MarriottBrowserProvider

REQUEST_TIMEOUT_MS = 45000
MAX_RESPONSE_CHARACTERS = 16 * 1024 * 1024

# Chromium supplies the site's session cookies and browser-managed headers.
# Do not use APIRequestContext here: sharing its cookie jar does not make its
# requests originate in the renderer or establish a loaded official document.
BROWSER_FETCH_SCRIPT = r"""async ({url, headers, body, timeout_ms, max_characters}) => {
  const origin = 'https://www.marriott.com';
  if (location.origin !== origin || new URL(url).origin !== origin)
    return {error:'origin_mismatch'};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  try {
    const response = await fetch(url, {
      method:'POST', headers, body:JSON.stringify(body),
      credentials:'same-origin', redirect:'manual', signal:controller.signal
    });
    if (response.type === 'opaqueredirect') return {status:0, redirected:true};
    const text = await response.text();
    if (text.length > max_characters) return {error:'body_too_large'};
    return {status:response.status, text,
      content_type:response.headers.get('content-type'),
      retry_after:response.headers.get('retry-after')};
  } catch (error) {
    return {error:error.name === 'AbortError' ? 'timeout' : 'network'};
  } finally { clearTimeout(timer); }
}"""


class MarriottHybridProvider(MarriottBrowserProvider):
    _api_document_url = ROOT + "/default.mi"

    def configure_hotel_url(self, official_url):
        super().configure_hotel_url(official_url)
        if self._root == ROOT:
            self._api_document_url = official_url

    async def _api_page(self):
        page = await self._get_page()
        status = None
        if page.url == "about:blank":
            response = await page.goto(
                self._api_document_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS
            )
            status = response.status if response else None
            if status == 429:
                raise ProviderError(
                    ErrorCode.RATE_LIMITED,
                    "Marriott official page rate limited",
                    retry_after(response.headers.get("retry-after")),
                )
        parsed = urlsplit(page.url)
        if parsed.scheme != "https" or parsed.netloc != "www.marriott.com":
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott API page has an unexpected origin")
        await self._check_access(page, status)
        return page

    async def search_rates(self, search):
        if self._root != ROOT:
            return await super().search_rates(search)
        if self._official_code and self._official_code != search.provider_hotel_id.upper():
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott configured property code mismatch")
        spec = MarriottProvider.query_request("rates", MarriottProvider.rate_variables(search))
        try:
            page = await self._api_page()
            result = await page.evaluate(
                BROWSER_FETCH_SCRIPT,
                {
                    "url": spec["url"],
                    "headers": {
                        k: v
                        for k, v in spec["headers"].items()
                        if k.lower() not in {"cookie", "host", "origin", "referer", "accept-language"}
                    },
                    "body": spec["json"],
                    "timeout_ms": REQUEST_TIMEOUT_MS,
                    "max_characters": MAX_RESPONSE_CHARACTERS,
                },
            )
            if not isinstance(result, dict):
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott browser API result is missing")
            failure = result.get("error")
            if failure:
                code, message = {
                    "timeout": (ErrorCode.TIMEOUT, "Marriott browser API request timed out"),
                    "origin_mismatch": (ErrorCode.INVALID_RESPONSE, "Marriott browser API origin changed"),
                    "body_too_large": (
                        ErrorCode.PROVIDER_CHANGED,
                        "Marriott browser API response is too large",
                    ),
                }.get(failure, (ErrorCode.NETWORK_ERROR, "Marriott browser API request failed"))
                raise ProviderError(code, message)
            status = result.get("status")
            if result.get("redirected") or status == 401 or isinstance(status, int) and 300 <= status < 400:
                raise ProviderError(ErrorCode.TOKEN_EXPIRED, "Marriott official session needs refresh")
            if status == 429:
                raise ProviderError(
                    ErrorCode.RATE_LIMITED,
                    "Marriott API rate limited",
                    retry_after(result.get("retry_after")),
                )
            if status == 403:
                raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott API access refused")
            if type(status) is not int or status < 200 or status > 599:
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott browser API status is missing")
            if status >= 400:
                raise ProviderError(ErrorCode.HTTP_ERROR, f"Marriott API HTTP {status}")
            text = result.get("text")
            if not isinstance(text, str):
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott browser API body is missing")
            raw = text.encode("utf-8")
            if "text/html" in (result.get("content_type") or "") and any(
                marker in raw[:10000].lower()
                for marker in (b"access denied", b"verify you are human", b"captcha challenge", b"cf-chl-")
            ):
                raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott API challenge page")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott API expected JSON") from None
            if not isinstance(body, dict):
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott API expected an object")
            return parse_rates(body, search, hashlib.sha256(raw).hexdigest())
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Marriott official session API request failed",
            ) from None
