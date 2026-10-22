from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from adapters.accor import AccorAdapter
from adapters.base import SamplesRequired, official_url
from adapters.gha import GHAAdapter
from adapters.hilton import HiltonAdapter, MockHiltonAdapter
from adapters.hyatt import HyattAdapter
from adapters.ihg import IHGAdapter
from adapters.marriott import MarriottAdapter
from models import Hotel, Quote, Stay

HOTEL = Hotel("hilton", "MOCK001", "Mock hotel", "https://www.hilton.com/en/")
STAY = Stay(date(2030, 10, 20), date(2030, 10, 21))


def quote(**changes):
    return Quote(
        **{
            "hotel": HOTEL,
            "stay": STAY,
            "rate_type": "PUBLIC_FLEXIBLE",
            "room_type": "KING",
            "amount": Decimal("100"),
            "currency": "USD",
            "tax_basis": "TAX_INCLUDED",
            "source_url": HOTEL.official_url,
            **changes,
        }
    )


def test_quote_roundtrip_and_comparison_key():
    item = quote()
    assert Quote.from_dict(item.to_dict()) == item
    assert quote(amount=Decimal("60")).series_key == item.series_key
    for change in (
        {"currency": "EUR"},
        {"rate_type": "MEMBER"},
        {"room_type": "SUITE"},
        {"tax_basis": "TAX_EXCLUDED"},
        {"is_mock": True},
        {"stay": replace(STAY, adults=1)},
        {"hotel": replace(HOTEL, hotel_id="OTHER")},
    ):
        assert quote(**change).series_key != item.series_key


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity"])
def test_non_cash_amounts_are_rejected(amount):
    with pytest.raises(ValueError):
        quote(amount=Decimal(amount))


def test_naive_time_is_not_comparable():
    with pytest.raises(ValueError):
        quote(observed_at=datetime(2030, 1, 1))
    assert quote(observed_at=datetime(2030, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("adapter", [MarriottAdapter, IHGAdapter, HyattAdapter, GHAAdapter, AccorAdapter])
async def test_other_brand_monitoring_entries_remain_unenabled(adapter):
    instance = adapter()
    with pytest.raises(SamplesRequired):
        await instance.fetch_rates(HOTEL, STAY)
    await instance.close()


async def test_hilton_construction_does_not_launch_a_browser_or_contact_the_site():
    adapter = HiltonAdapter()
    assert adapter._playwright is None and adapter._browser is None
    await adapter.close()


async def test_hilton_mock_is_explicit_and_confirmation_fetch_is_independent():
    adapter = MockHiltonAdapter(["60", "64"])
    first = (await adapter.fetch_rates(HOTEL, STAY))[0]
    second = (await adapter.fetch_rates(HOTEL, STAY, confirmation=True))[0]
    assert first.is_mock and second.is_mock
    assert first.amount == 60 and second.amount == 64
    assert first.observation_id != second.observation_id
    assert adapter.calls[-1][-1] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.hilton.com.evil.invalid/",
        "http://www.hilton.com/",
        "https://user:pass@www.hilton.com/",
        "https://www.hilton.com:8443/",
    ],
)
def test_only_official_public_urls_are_accepted(url):
    with pytest.raises(ValueError):
        official_url("hilton", url)
