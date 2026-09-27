from collections.abc import Callable

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider

# No guessed endpoints, synthetic prices or unverified adapters in production.
FACTORIES: dict[str, Callable[[], HotelProvider]] = {}


def create_provider(name: str) -> HotelProvider:
    if name not in FACTORIES:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Provider pending verified research and implementation"
        )
    return FACTORIES[name]()
