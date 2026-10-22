import importlib.util
import os
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_hybrid import MarriottHybridProvider
from app.providers.marriott_passive_bridge import collector_modules
from app.providers.marriott_worker import MarriottWorkerProvider
from app.providers.registry import create_provider
from tests.test_marriott import SEARCH


async def test_registry_routes_cn_price_queries_to_worker_owned_session():
    provider = create_provider(
        "marriott",
        hotel_name="New York Marriott Marquis",
        official_url="https://www.marriott.com.cn/hotels/travel/nycmq-new-york-marriott-marquis",
    )
    assert isinstance(provider, MarriottHybridProvider)
    assert isinstance(provider, MarriottWorkerProvider)
    assert provider._cn_provider._use_backend_session is True
    provider._cn_provider.search_rates = AsyncMock(return_value=["live CN quote"])
    provider._cn_provider.close = AsyncMock()
    assert await provider.get_calendar_rates(SEARCH) == ["live CN quote"]
    await provider.close()
    provider._cn_provider.close.assert_awaited_once()


async def test_borrowed_browser_context_stays_open_after_driver_disconnect():
    ui, _ = collector_modules()
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel="chrome" if os.name == "nt" else None, headless=True)
        try:
            context = await browser.new_context()
            owner = await context.new_page()
            await owner.set_content("<h1>Existing session page</h1>")
            driver = ui.MarriottUIDriver(context=context)
            await driver.start()
            owned = driver._page
            assert owned is not owner
            await owned.close()
            await driver.close()
            assert browser.is_connected()
            assert await owner.locator("h1").inner_text() == "Existing session page"
            await context.new_page()
        finally:
            await browser.close()


async def test_worker_borrowed_session_runs_entire_official_ui_from_local_fixtures(tmp_path):
    from app.providers.marriott_passive_bridge import COLLECTOR_ROOT
    from app.schemas.domain import RateRequest

    collector_modules()
    spec = importlib.util.spec_from_file_location(
        "marriott_ui_offline", COLLECTOR_ROOT / "tests/test_marriott_ui.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    provider = MarriottWorkerProvider(
        headless=True,
        browser_channel="chrome" if os.name == "nt" else None,
        session_dir=str(tmp_path),
    )
    provider.configure_hotel_url(fixture.HOTEL_URL)
    seen = []
    try:
        page = await provider._cn_provider._get_page()
        await fixture.install_routes(page.context, seen)
        task = fixture.task()
        rates = await provider.search_rates(
            RateRequest(
                provider_hotel_id=task.hotel_id,
                check_in=task.checkin,
                check_out=task.checkout,
                adults=task.adults,
            )
        )
        assert len(rates) == 67
        assert all(rate.provider_hotel_id == "NYCTE" for rate in rates)
        assert {rate.member_rate for rate in rates} == {True, False}
        assert [path for method, path, _ in seen if method == "GET" and path != "/favicon.ico"] == [
            "/",
            fixture.HOTEL_PATH,
        ]
    finally:
        await provider.close()
        await close_browser_sessions()
