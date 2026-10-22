"""Challenge signals are constants, not bypass techniques or price selectors."""

import re
from urllib.parse import urlsplit

from adapters.base import OFFICIAL_DOMAINS

CHALLENGE_SELECTORS = (
    "#challenge-form",
    '[data-testid="captcha-challenge"]',
    'iframe[title*="challenge" i]',
    'iframe[title*="验证码"]',
)
CHALLENGE_TEXT = re.compile(
    r"verify (?:that )?you are (?:a )?human|complete the security check|"
    r"please (?:solve|complete) the captcha|请完成安全验证|请完成验证码|访问被拒绝|access denied",
    re.IGNORECASE,
)
PAGE_TIMEOUT_MS = 45000
JSON_BODY_TIMEOUT_SECONDS = 30


def is_official_request(brand, request) -> bool:
    host = (urlsplit(request.url).hostname or "").lower()
    return request.resource_type in ("document", "xhr", "fetch") and any(
        host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS[brand]
    )


async def visible_challenge(page) -> bool:
    for selector in CHALLENGE_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
    return bool(CHALLENGE_TEXT.search(await page.locator("body").inner_text(timeout=2000)))
