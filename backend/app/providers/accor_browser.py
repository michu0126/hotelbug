"""Accor public website form and rendered public, tax-inclusive room prices."""

import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import expect

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider
from app.providers.browser_session import new_provider_page
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://all.accor.com"
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def is_explicit_test_hotel(name: str) -> bool:
    """Exclude the actual public helpdesk test entry, not arbitrary 'test' words."""
    return " ".join(name.casefold().split()) == "test hotel for the helpdesk resynch"


def hotel_code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{4}", value):
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "Accor hotel code must be four alphanumeric characters"
        )
    return value.upper()


def name_key(value: str) -> str:
    return hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()[:24]


def public_detail_location(html: str, hotel_name: str, hotel_id: str, page_url: str) -> dict:
    """Read the observed public Hotel schema only after hotel/name agreement."""
    parsed = urlparse(page_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "all.accor.com"
        or parsed.path != f"/hotel/{hotel_code(hotel_id)}/index.en.shtml"
    ):
        return {}
    hotels = []
    soup = BeautifulSoup(html, "html.parser")

    def visible_address_agrees(address):
        if not isinstance(address, dict):
            return False
        required = [address.get("streetAddress"), address.get("addressLocality")]
        if not all(isinstance(value, str) and value.strip() for value in required):
            return False
        sections = [
            title.parent for title in soup.select("h2") if title.get_text(" ", strip=True) == "Hotel location"
        ]
        if len(sections) != 1:
            return False
        titles = sections[0].select("p.infos__title")
        if len(titles) != 1 or " ".join(titles[0].get_text(" ", strip=True).casefold().split()) != " ".join(
            hotel_name.casefold().split()
        ):
            return False
        block = titles[0].find_next_sibling("p")
        if block is None:
            return False
        displayed = " ".join(block.get_text(" ", strip=True).casefold().split())
        # Schema names may be SEO titles. Only the actual owner name/address
        # block can establish agreement, not nearby stations or a body search.
        return all(
            re.search(r"(?<!\w)" + re.escape(" ".join(value.casefold().split())) + r"(?!\w)", displayed)
            for value in required
        )

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type")
            if types == "Hotel" or isinstance(types, list) and "Hotel" in types:
                name = value.get("name")
                if isinstance(name, str):
                    # This exact suffix is in the actual ALL public schema;
                    # don't infer brands or match only a substring of the name.
                    name = name.removesuffix(" - ALL")
                    if " ".join(name.casefold().split()) == " ".join(
                        hotel_name.casefold().split()
                    ) or visible_address_agrees(value.get("address")):
                        hotels.append(value)
            if "@graph" in value:
                visit(value["@graph"])

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    if len(hotels) != 1:
        return {}
    result = {}
    address = hotels[0].get("address")
    if isinstance(address, dict):
        for source, target in (
            ("streetAddress", "address"),
            ("addressLocality", "city"),
            ("addressRegion", "region"),
            ("addressCountry", "country"),
        ):
            value = address.get(source)
            if isinstance(value, str) and value.strip():
                result[target] = " ".join(value.split())
    geo = hotels[0].get("geo")
    if isinstance(geo, dict):
        for key, limit in (("latitude", 90), ("longitude", 180)):
            value = geo.get(key)
            if type(value) not in (str, int, float):
                continue
            try:
                coordinate = Decimal(str(value))
                if coordinate.is_finite() and -limit <= coordinate <= limit:
                    result[key] = coordinate
            except InvalidOperation:
                continue
    return result


def parse_public_rates(html: str, request: RateRequest, page_url: str) -> list[RateData]:
    code = hotel_code(request.provider_hotel_id)
    url = urlparse(page_url)
    if url.hostname != "all.accor.com" or url.path.rstrip("/") != f"/booking/en/accor/hotel/{code}":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor booking hotel does not match request")
    if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor page adapter supports one room, one night")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    for day in (request.check_in, request.check_out):
        if f"{MONTHS[day.month - 1]} {day.day:02d}, {day.year}" not in text:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor displayed dates do not match request")
    currencies = []
    for button in soup.select("button"):
        match = re.fullmatch(r"([A-Z]{3})\s*\([^)]*\)", button.get_text(" ", strip=True))
        if match:
            currencies.append(match.group(1))
    if len(set(currencies)) != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor selected currency is missing or ambiguous")
    digest = hashlib.sha256(html.encode()).hexdigest()
    rates = {}
    for label in soup.select("label.hotel-accommodation-offers-content__label"):
        public = next(
            (
                p
                for p in label.select(".offer-price")
                if p.get_text(" ", strip=True).startswith("Public rate")
            ),
            None,
        )
        if public is None:
            continue
        amount = public.select_one(".price-display-amount")
        title = label.select_one(".ads-radio__label")
        details = label.select_one(".stay-details")
        if amount is None or title is None or details is None:
            continue
        terms = details.get_text(" ", strip=True)
        if "Taxes and fees included." not in terms or "1 night" not in terms:
            continue
        if not re.search(rf"\b{request.adults} adults?\b", terms):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor guest count does not match request")
        money = re.fullmatch(
            r"[^\d]*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:[A-Z]{3})?", amount.get_text(" ", strip=True)
        )
        if not money:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor public price format changed")
        parent = label.parent
        heading = None
        while parent and parent.name != "body":
            headings = parent.select(".hotel-accommodations-offers__item-title")
            if len(headings) == 1:
                heading = headings[0]
                break
            if len(headings) > 1:
                break
            parent = parent.parent
        if heading is None:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room identity missing")
        room_name = " ".join(heading.get_text(" ", strip=True).split())
        rate_name = " ".join(title.get_text(" ", strip=True).split())
        policy = label.get_text(" ", strip=True)
        rate = RateData(
            **request.model_dump(exclude={"provider_hotel_id"}),
            provider="accor",
            provider_hotel_id=code,
            room_type=room_name,
            room_code="name:" + name_key(room_name),
            rate_name=rate_name + " · Public rate",
            rate_code="public-name:" + name_key(rate_name),
            total_price=Decimal(money.group(1).replace(",", "")),
            currency=currencies[0],
            member_rate=False,
            availability=True,
            price_basis="stay_total",
            refundable=False
            if "Non-refundable" in policy
            else True
            if "Cancel free of charge" in policy
            else None,
            breakfast_included=True if "Breakfast included" in policy else None,
            source_url=ROOT + url.path,
            raw_response_hash=digest,
        )
        if rate.total_price <= 0:
            continue
        rates[rate.offer_key()] = rate
    if not rates:
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED, "No verified Accor public tax-inclusive offers on page"
        )
    return list(rates.values())


class AccorBrowserProvider(HotelProvider):
    session_provider = "accor"

    def __init__(
        self,
        proxy_server: str | None = None,
        browser_channel: str | None = None,
        session_dir: str | None = None,
        headless: bool = True,
    ):
        self.proxy_server = proxy_server
        self.browser_channel = browser_channel
        self.session_dir = session_dir
        self.headless = headless
        self._page = None

    async def _get_page(self):
        if self._page is None:
            self._page = await new_provider_page(
                self.session_provider,
                proxy_server=self.proxy_server,
                browser_channel=self.browser_channel,
                session_dir=self.session_dir,
                headless=self.headless,
            )
            self._page.set_default_timeout(30000)

            # Consent can appear after the form becomes usable, especially on the
            # second task in a persistent session. Handle it before blocked actions.
            async def dismiss_consent(locator):
                await locator.click()

            await self._page.add_locator_handler(
                self._page.get_by_text("Continue without Accepting", exact=False).first,
                dismiss_consent,
            )
        return self._page

    async def _open(self, code: str):
        page = await self._get_page()
        response = await page.goto(
            ROOT + f"/hotel/{hotel_code(code)}/index.en.shtml", wait_until="domcontentloaded", timeout=45000
        )
        if response and response.status in (403, 429):
            raise ProviderError(
                ErrorCode.BLOCKED_BY_ANTIBOT if response.status == 403 else ErrorCode.RATE_LIMITED,
                "Accor access rejected",
            )
        if response and response.status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"Accor HTTP {response.status}")
        await page.locator("#booking-date-in.hasDatepicker").wait_for(state="visible")
        return page

    async def search_hotels(self, query: dict) -> list[HotelData]:
        if not query.get("provider_hotel_id"):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor details require a catalog hotel code")
        return [await self.get_hotel_details(query["provider_hotel_id"])]

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        try:
            page = await self._open(hotel_id)
            name = (await page.locator("h1").first.inner_text()).strip()
            name = re.sub(r"\s+\d(?:\.\d)?\s+stars?$", "", name, flags=re.IGNORECASE)
            location = public_detail_location(await page.content(), name, hotel_id, page.url)
            return HotelData(
                provider="accor",
                provider_hotel_id=hotel_code(hotel_id),
                hotel_name=name,
                official_url=ROOT + f"/hotel/{hotel_code(hotel_id)}/index.en.shtml",
                **location,
                # Explicit False prevents global monitoring of the official
                # helpdesk placeholder. Omit True so a normal catalog refresh
                # cannot re-enable a hotel the user deliberately disabled.
                **({"active": False} if is_explicit_test_hotel(name) else {}),
            )
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Accor hotel page failed",
            ) from exc

    async def _set_occupancy(self, page, request: RateRequest) -> None:
        """Normalize the actual form state using the site's own guest buttons."""
        summary = page.locator("#compo-summary")
        if await summary.get_attribute("aria-expanded") != "true":
            await summary.click()
        await page.locator("#booking-compo").wait_for(state="visible")
        room_fields = page.locator("#booking-compo .booking__room")
        room_count = await room_fields.count()
        if not 1 <= room_count <= 9:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room controls changed")
        # Remove the last extra room so the original first room stays stable.
        for remaining in range(room_count - 1, 0, -1):
            await room_fields.last.get_by_role("button", name="Remove the room", exact=True).click()
            await expect(room_fields).to_have_count(remaining, timeout=5000)
        await expect(page.locator("#search-room-number")).to_have_value("1", timeout=5000)
        room = page.locator("#compo-room-0")
        for selector, target, minimum, maximum, noun in (
            ("#search-adult-room-0", request.adults, 1, 9, "an adult"),
            ("#search-children-room-0", 0, 0, 6, "a child"),
        ):
            control = page.locator(selector)
            # input_value reads the current property; get_attribute('value')
            # still returns the original 1/0 after the user changes guests.
            raw = await control.input_value()
            if not raw.isdigit() or not minimum <= int(raw) <= maximum:
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor guest controls changed")
            current = int(raw)
            direction = 1 if current < target else -1
            action = "Add" if direction == 1 else "Remove"
            for expected in range(current + direction, target + direction, direction):
                await room.get_by_role("button", name=f"{action} {noun}", exact=True).click()
                await expect(control).to_have_value(str(expected), timeout=5000)
            await expect(control).to_have_value(str(target), timeout=5000)
        await summary.click()
        await expect(summary).to_have_attribute("aria-expanded", "false", timeout=5000)

    async def _collect_public_rates(self, page, request: RateRequest) -> list[RateData]:
        """Read named plans for every actual room card, not collapsed 'from' prices."""
        cards = page.locator(".hotel-accommodations-offers__item")
        headings = page.locator(".hotel-accommodations-offers__item-title")
        room_names = [" ".join(name.split()) for name in await headings.all_text_contents()]
        if not room_names or any(not name for name in room_names) or len(set(room_names)) != len(room_names):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room identities missing or ambiguous")
        if await cards.count() != len(room_names):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room cards do not match headings")
        deadline = monotonic() + 45

        def remaining_ms():
            remaining = int((deadline - monotonic()) * 1000)
            if remaining <= 0:
                raise BrowserTimeout("Accor all-room public offers did not finish within 45 seconds")
            return remaining

        rates = {}
        for index, room_name in enumerate(room_names):
            card = cards.nth(index)
            heading = card.locator(".hotel-accommodations-offers__item-title")
            if " ".join((await heading.inner_text()).split()) != room_name:
                raise ProviderError(
                    ErrorCode.PROVIDER_CHANGED, "Accor room order changed while reading offers"
                )
            public = card.locator(
                "label.hotel-accommodation-offers-content__label .offer-price--alternative"
            ).first
            if not await public.is_visible():
                choose = card.get_by_role("button", name="Choose this room", exact=True)
                if await choose.count() != 1:
                    raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room offer control missing")
                await choose.click(timeout=remaining_ms())
            await public.wait_for(state="visible", timeout=remaining_ms())
            # Switching rooms collapses the preceding plans. Keep each real
            # snapshot separately, with no guessing from summary/member prices.
            parsed = parse_public_rates(await page.content(), request, page.url)
            current = [rate for rate in parsed if rate.room_type == room_name]
            if not current:
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "Accor selected room has no verified public plans"
                )
            rates.update({rate.offer_key(): rate for rate in current})
        final_names = [" ".join(name.split()) for name in await headings.all_text_contents()]
        if final_names != room_names:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room list changed while reading offers")
        return list(rates.values())

    async def search_rates(self, request: RateRequest) -> list[RateData]:
        if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor page adapter supports one room, one night")
        stage = "hotel page"
        try:
            page = await self._open(request.provider_hotel_id)
            stage = "stay dates"
            await page.locator('[name="search.dateIn"]').fill(request.check_in.strftime("%d/%m/%Y"))
            await page.locator('[name="search.dateIn"]').press("Tab")
            await page.locator('[name="search.dateOut"]').fill(request.check_out.strftime("%d/%m/%Y"))
            await page.locator('[name="search.dateOut"]').press("Tab")
            stage = "guest count"
            await self._set_occupancy(page, request)
            form = page.locator("form").filter(has=page.locator('[name="search.dateIn"]'))
            stage = "search submission"
            await form.get_by_role("button", name="See rates", exact=True).click()
            stage = "booking navigation"
            # Booking controls can be ready while unrelated resources prevent
            # the full load event. Wait for navigation, then the real offers.
            await page.wait_for_url("**/booking/**", wait_until="commit", timeout=45000)
            stage = "public offers"
            await page.locator(
                "label.hotel-accommodation-offers-content__label .offer-price--alternative"
            ).first.wait_for(state="visible", timeout=45000)
            stage = "all room offers"
            return await self._collect_public_rates(page, request)
        except AssertionError as exc:
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, f"Accor form did not reach requested state at {stage}"
            ) from exc
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                f"Accor booking page failed at {stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="0338", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(status="ONLINE" if rates else "DEGRADED", message="Accor rendered public offers")

    async def close(self):
        if self._page:
            await self._page.close()
            self._page = None
