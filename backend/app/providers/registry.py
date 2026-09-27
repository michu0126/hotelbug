from collections.abc import Callable

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider
from app.providers.marriott import MarriottProvider

# No guessed endpoints, synthetic prices or unverified adapters in production.
FACTORIES: dict[str, Callable[[], HotelProvider]] = {"marriott": MarriottProvider}


def create_provider(name: str) -> HotelProvider:
    if name not in FACTORIES:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Provider pending verified research and implementation"
        )
    return FACTORIES[name]()
