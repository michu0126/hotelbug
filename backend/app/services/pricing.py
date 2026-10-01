"""Use an explicit public cash amount without inventing an all-fees total."""

from decimal import Decimal
from typing import Protocol


class CashQuote(Protocol):
    cash_price: Decimal | None
    total_price: Decimal | None


def price_field(quote: CashQuote) -> str:
    return "total_price" if quote.total_price is not None else "cash_price"


def displayed_price(quote: CashQuote) -> Decimal | None:
    return getattr(quote, price_field(quote))
