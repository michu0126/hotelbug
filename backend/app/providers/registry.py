from collections.abc import Callable

from app.core.config import get_settings
from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider
from app.providers.marriott import MarriottProvider


def accor_browser() -> HotelProvider:
    # API and Scheduler can list providers without installing the optional browser runtime.
    try:
        from app.providers.accor_browser import AccorBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc

    settings = get_settings()
    return AccorBrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
    )


FACTORIES: dict[str, Callable[[], HotelProvider]] = {
    "marriott": MarriottProvider,
    "accor": accor_browser,
}


def create_provider(name: str) -> HotelProvider:
    if name not in FACTORIES:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Provider pending verified research and implementation"
        )
    return FACTORIES[name]()
