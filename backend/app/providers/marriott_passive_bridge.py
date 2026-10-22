"""Use the verified China-site UI collector from the global backend queue.

Discovery/details/calendar hints remain the established backend implementation.
Only cash-price collection changes: ordinary homepage/hotel/date controls emit
the requests, and the collector observes their responses. No injected fetch or
request replay, and no host/profile change after a denial.
"""

import hashlib
import importlib
import json
import math
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as BrowserError

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider
from app.schemas.domain import RateData, RateRequest

CN_HOST = "www.marriott.com.cn"
COLLECTOR_ROOT = Path(__file__).resolve().parents[3] / "collector"
UNAVAILABLE_EVIDENCE = "official_sold_out_redirect_and_matching_hotel_card"
OVERVIEW_CACHE: dict[str, str] = {}


def collector_modules():
    """Load the sibling collector without shadowing a different models package.

    The backend uses ``app.models``, whereas the small standalone collector uses
    top-level ``models``/``adapters``. Fail explicitly on a genuine conflict;
    never replace modules already loaded by somebody else's application.
    """
    if not (COLLECTOR_ROOT / "adapters" / "marriott_ui.py").is_file():
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Local Marriott collector is not installed")
    for name in ("models", "adapters", "passive_capture"):
        loaded = sys.modules.get(name)
        if loaded is None:
            continue
        origin = getattr(loaded, "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(COLLECTOR_ROOT):
            raise ProviderError(ErrorCode.BROWSER_UNAVAILABLE, "Collector module namespace is occupied")
    collector_path = str(COLLECTOR_ROOT)
    if collector_path not in sys.path:
        sys.path.insert(0, collector_path)
    try:
        return importlib.import_module("adapters.marriott_ui"), importlib.import_module("adapters.base")
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Local Marriott collector dependencies missing"
        ) from exc


def _terms_code(record) -> str:
    # RateData lacks full cancellation/deposit text. Keep it in the comparable
    # identity, so a changed contract or source cannot appear as a price drop.
    evidence = {
        "source_site": record.source_site,
        "cancellation_policy": record.cancellation_policy.comparison_dict(),
        "deposit_required": record.deposit_required,
    }
    suffix = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()[:20]
    return f"{record.rate_code}:cn:{suffix}"


def normalize_live_records(records, search: RateRequest, *, started_at: datetime) -> list[RateData]:
    """Map only explicitly matching, current live observations; never infer tax."""
    if started_at.tzinfo is None:
        raise ValueError("Query start must include a timezone")
    result = []
    for record in records:
        if (
            record.brand != "marriott"
            or record.hotel_id != search.provider_hotel_id
            or record.checkin != search.check_in
            or record.checkout != search.check_out
            or record.adults != search.adults
            or record.rooms != search.rooms
            or record.source_site != CN_HOST
            or record.source_kind != "LIVE"
            or record.captured_at is None
            # process_capture intentionally timestamps observations in seconds.
            or record.captured_at < started_at.replace(microsecond=0)
            or record.captured_at > datetime.now(timezone.utc).replace(microsecond=0)
        ):
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott live quote identity or provenance mismatch"
            )
        policy = record.cancellation_policy
        refundable = False if policy.non_refundable is True else policy.refundable
        tax = record.tax_amount
        if tax is not None and record.amount_excluding_tax + tax != record.amount_including_tax:
            # A total may also contain mandatory fees. Do not label those taxes.
            tax = None
        result.append(
            RateData(
                **search.model_dump(),
                provider="marriott",
                room_type=record.room_name,
                room_code=record.room_code,
                rate_name=record.rate_name,
                rate_code=_terms_code(record),
                cash_price=record.amount_excluding_tax,
                tax=tax,
                total_price=record.amount_including_tax,
                currency=record.currency,
                refundable=refundable,
                member_rate=record.member_rate,
                availability=True,
                price_basis=record.price_basis,
                captured_at=record.captured_at,
                source_url=record.source_url,
                raw_response_hash=record.response_hash,
            )
        )
    return result


class MarriottPassiveBridgeProvider(MarriottBrowserProvider):
    def __init__(self, *, port=9222, timeout_seconds=90, use_backend_session=False, **kwargs):
        super().__init__(**kwargs)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Chrome debugging port must be 1024–65535")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Query timeout must be positive and finite")
        self._collector_port = port
        self._collector_timeout = timeout_seconds
        self._collector_driver = None
        self._use_backend_session = use_backend_session
        self._overview_url = None
        self._catalog_lookup_url = None

    def configure_hotel_url(self, official_url: str) -> None:
        super().configure_hotel_url(official_url)
        url = urlsplit(official_url)
        # Never guess a hotel slug or switch .com to .com.cn after a denial.
        self._overview_url = (
            official_url if url.netloc == CN_HOST and url.path.startswith("/hotels/") else None
        )
        self._catalog_lookup_url = (
            official_url if url.netloc == CN_HOST and self._overview_url is None else None
        )

    async def _resolve_overview(self, ui) -> str | None:
        """Follow an actual same-hotel link from a validated public catalog URL.

        The existing worldwide directory publishes calendar links. They are
        useful for discovering the hotel's real overview URL, not for inventing
        a slug or treating an indicative calendar amount as an available rate.
        """
        if self._overview_url is not None or self._catalog_lookup_url is None:
            return self._overview_url
        if self._official_code in OVERVIEW_CACHE:
            self._overview_url = OVERVIEW_CACHE[self._official_code]
            return self._overview_url
        try:
            page = await self._get_page()
            response = await page.goto(self._catalog_lookup_url, wait_until="domcontentloaded")
            await self._check_access(page, response.status if response else None)
            if urlsplit(page.url).netloc != CN_HOST:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott directory lookup changed origin")
            links = page.locator('a[href*="/hotels/"]')
            await links.first.wait_for(state="attached", timeout=15000)
            candidates = []
            for index in range(await links.count()):
                href = await links.nth(index).get_attribute("href")
                if not href:
                    continue
                value = urljoin(page.url, href)
                try:
                    ui.validate_hotel_url(value, self._official_code)
                except ValueError:
                    continue
                candidates.append(value)
            if candidates:
                self._overview_url = sorted(set(candidates), key=lambda url: ("/overview" not in url, url))[0]
                OVERVIEW_CACHE[self._official_code] = self._overview_url
            return self._overview_url
        except BrowserError:
            raise ProviderError(ErrorCode.NETWORK_ERROR, "Marriott overview link lookup failed") from None

    async def search_rates(self, search: RateRequest) -> list[RateData]:
        if search.rooms != 1 or self._official_code != search.provider_hotel_id:
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott UI requires matching hotel and one room"
            )
        ui, errors = collector_modules()
        if await self._resolve_overview(ui) is None:
            raise ProviderError(
                ErrorCode.NOT_IMPLEMENTED,
                "A verified Marriott China hotel overview URL is required for passive UI pricing",
            )
        if self._collector_driver is None:
            context = (await self._get_page()).context if self._use_backend_session else None
            self._collector_driver = ui.MarriottUIDriver(
                port=self._collector_port, timeout_seconds=self._collector_timeout, context=context
            )
        task = SimpleNamespace(
            hotel_id=search.provider_hotel_id,
            hotel_url=self._overview_url,
            checkin=search.check_in,
            checkout=search.check_out,
            adults=search.adults,
        )
        started_at = datetime.now(timezone.utc)
        try:
            outcome = await self._collector_driver.query(task, include_records=True)
        except errors.BrandBlocked:
            raise ProviderError(
                ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott official page refused access"
            ) from None
        except errors.RateLimited as exc:
            raise ProviderError(
                ErrorCode.RATE_LIMITED, "Marriott official page rate limited", math.ceil(exc.retry_after)
            ) from None
        except ui.DateNotSelectable:
            raise ProviderError(
                ErrorCode.BOOKING_WINDOW_CLOSED, "Date not selectable in Marriott calendar"
            ) from None
        except errors.TransientFailure:
            raise ProviderError(ErrorCode.TIMEOUT, "Marriott ordinary UI query did not finish") from None
        except (OSError, ValueError):
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott UI or browser connection unavailable"
            ) from None
        expected = {
            "hotel_id": task.hotel_id,
            "checkin": str(task.checkin),
            "checkout": str(task.checkout),
            "adults": task.adults,
            "source_site": CN_HOST,
        }
        if not isinstance(outcome, dict) or any(outcome.get(k) != v for k, v in expected.items()):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott result context mismatch")
        if outcome.get("status") == "no_availability":
            if outcome.get("offers") != 0 or outcome.get("evidence") != UNAVAILABLE_EVIDENCE:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott availability evidence missing")
            return []  # Unavailable is not a fabricated zero-price record.
        records = outcome.get("records")
        if (
            outcome.get("status") != "ok"
            or not isinstance(records, list)
            or type(outcome.get("offers")) is not int
            or outcome["offers"] != len(records)
            or any(record.response_hash != outcome.get("response_hash") for record in records)
        ):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott live response records missing")
        return normalize_live_records(records, search, started_at=started_at)

    async def close(self) -> None:
        try:
            if self._collector_driver is not None:
                # Only tabs created by this query, never the caller's Chrome tabs.
                for page in tuple(self._collector_driver._owned):
                    if not page.is_closed():
                        with suppress(BrowserError):
                            await page.close()
                await self._collector_driver.close()  # Disconnect only; never close external Chrome.
                self._collector_driver = None
        finally:
            await super().close()
