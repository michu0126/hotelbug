"""Hilton public-page adapter and explicit synthetic counterpart."""

from collections import deque
from decimal import Decimal

from adapters.base import official_url
from adapters.hilton_browser import HiltonBrowserAdapter
from models import Hotel, Quote, Stay, utcnow

SEARCH_URL = "https://www.hilton.com/en/"


class HiltonAdapter(HiltonBrowserAdapter):
    pass


class MockHiltonAdapter:
    """Synthetic cash totals, intentionally isolated from real observations."""

    def __init__(self, amounts=("100.00",), currency="USD", *, clock=utcnow):
        self.amounts = deque(Decimal(str(amount)) for amount in amounts)
        if not self.amounts:
            raise ValueError("Mock amounts cannot be empty")
        self.currency = currency
        self.clock = clock
        self.calls = []

    async def fetch_rates(self, hotel: Hotel, stay: Stay, *, confirmation: bool = False) -> list[Quote]:
        if hotel.brand != "hilton":
            raise ValueError("Hilton adapter requires a Hilton hotel")
        official_url("hilton", hotel.official_url)
        self.calls.append((hotel.hotel_id, stay, confirmation))
        amount = self.amounts[0]
        if len(self.amounts) > 1:
            self.amounts.popleft()
        return [
            Quote(
                hotel,
                stay,
                "MOCK_PUBLIC_FLEXIBLE",
                "MOCK_STANDARD_ROOM",
                amount,
                self.currency,
                "TAX_INCLUDED",
                hotel.official_url,
                is_mock=True,
                observed_at=self.clock(),
            )
        ]

    async def close(self) -> None:
        pass
