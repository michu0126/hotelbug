import asyncio
import os

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from app.core.errors import ProviderError
from app.providers.accor_browser import CURRENCY_BUTTON, CURRENCY_READY, parse_public_rates
from tests.test_accor_browser import rendered_page, request


async def test_public_prices_wait_for_late_explicit_currency():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel="chrome" if os.name == "nt" else None, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(rendered_page().replace("<button>EUR (€)</button>", ""))
            # Prices alone are insufficient until the official ISO currency exists.
            with pytest.raises(ProviderError):
                parse_public_rates(
                    await page.content(), request(), "https://all.accor.com/booking/en/accor/hotel/0338"
                )
            pending = asyncio.create_task(
                page.wait_for_function(
                    CURRENCY_READY,
                    arg=CURRENCY_BUTTON.pattern,
                    timeout=2000,
                )
            )
            await asyncio.sleep(0.1)
            assert not pending.done()
            await page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<button>EUR (€)</button>')")
            await pending
            assert (
                parse_public_rates(
                    await page.content(), request(), "https://all.accor.com/booking/en/accor/hotel/0338"
                )[0].currency
                == "EUR"
            )
            await page.set_content("<button>EUR (€)</button><button>USD ($)</button>")
            with pytest.raises(BrowserTimeout):
                await page.wait_for_function(CURRENCY_READY, arg=CURRENCY_BUTTON.pattern, timeout=100)
        finally:
            await browser.close()
