from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProviderError
from app.providers.accor_browser import AccorBrowserProvider, parse_public_rates
from app.schemas.domain import RateRequest


def rendered_page():
    # Minimal projection of the rendered public Accor page observed 2026-09-29.
    return """<button>EUR (€)</button><h1>ibis Alès Centre-Ville</h1>
    <div>October 07, 2026 October 08, 2026</div>
    <section><h3 class="hotel-accommodations-offers__item-title">Standard Room with 1 double bed</h3>
      <label class="hotel-accommodation-offers-content__label" for="1RA3-DBC">
        <span class="ads-radio__label">SAVER RATE</span><span>Non-refundable</span>
        <p class="offer-price">Member rate <span class="price-display-amount">€91.77</span></p>
        <p class="offer-price offer-price--alternative">Public rate <span class="price-display-amount">€96.47</span></p>
        <div class="stay-details">1 night 2 adults Taxes and fees included.</div>
      </label>
    </section>"""


def request():
    return RateRequest(provider_hotel_id="0338", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))


def test_accor_public_price_does_not_take_lower_member_price():
    rates = parse_public_rates(
        rendered_page(), request(), "https://all.accor.com/booking/en/accor/hotel/0338"
    )
    assert len(rates) == 1
    rate = rates[0]
    assert rate.total_price == Decimal("96.47") and rate.currency == "EUR"
    assert rate.member_rate is False and rate.refundable is False
    assert rate.room_type == "Standard Room with 1 double bed"


@pytest.mark.parametrize(
    "old,new",
    [
        ("October 08, 2026", "October 09, 2026"),
        ("2 adults", "1 adult"),
        ("Taxes and fees included.", "Excluding taxes"),
        ("EUR (€)", "€"),
        ("€96.47", "€96,47"),
    ],
)
def test_accor_rejects_mismatched_or_ambiguous_quote(old, new):
    with pytest.raises(ProviderError):
        parse_public_rates(
            rendered_page().replace(old, new), request(), "https://all.accor.com/booking/en/accor/hotel/0338"
        )


def test_accor_wrong_property_rejected():
    with pytest.raises(ProviderError):
        parse_public_rates(rendered_page(), request(), "https://all.accor.com/booking/en/accor/hotel/0339")


async def test_booking_navigation_waits_for_commit_then_verifiable_offers(monkeypatch):
    provider = AccorBrowserProvider()
    page = MagicMock()
    control = MagicMock()
    control.filter.return_value = control
    control.first = control
    control.get_by_role.return_value = control
    for action in ("fill", "press", "click", "wait_for"):
        setattr(control, action, AsyncMock())
    page.locator.return_value = control
    page.get_by_role.return_value = control
    page.wait_for_url = AsyncMock()
    page.content = AsyncMock(return_value=rendered_page())
    page.url = "https://all.accor.com/booking/en/accor/hotel/0338"
    monkeypatch.setattr(provider, "_open", AsyncMock(return_value=page))
    occupancy = AsyncMock()
    monkeypatch.setattr(provider, "_set_occupancy", occupancy)
    collect = AsyncMock(return_value=parse_public_rates(rendered_page(), request(), page.url))
    monkeypatch.setattr(provider, "_collect_public_rates", collect)
    rates = await provider.search_rates(request())
    occupancy.assert_awaited_once_with(page, request())
    collect.assert_awaited_once_with(page, request())
    assert len(rates) == 1 and rates[0].total_price == Decimal("96.47")
    page.wait_for_url.assert_awaited_once_with("**/booking/**", wait_until="commit", timeout=45000)
    control.wait_for.assert_awaited_once_with(state="visible", timeout=45000)
