from collections.abc import Callable

from app.core.config import get_settings
from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider


def marriott_browser() -> HotelProvider:
    try:
        from app.providers.marriott_browser import MarriottBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc

    settings = get_settings()
    return MarriottBrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


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
        session_dir=settings.browser_session_dir,
    )


def hilton_browser() -> HotelProvider:
    try:
        from app.providers.hilton_browser import HiltonBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc
    settings = get_settings()
    return HiltonBrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


def gha_browser() -> HotelProvider:
    try:
        from app.providers.gha_browser import GHABrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc
    settings = get_settings()
    return GHABrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


FACTORIES: dict[str, Callable[[], HotelProvider]] = {
    "marriott": marriott_browser,
    "accor": accor_browser,
    "gha": gha_browser,
    "hilton": hilton_browser,
}


def create_provider(name: str, hotel_name: str | None = None) -> HotelProvider:
    if name not in FACTORIES:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Provider pending verified research and implementation"
        )
    provider = FACTORIES[name]()
    if hotel_name:
        configure_hotel = getattr(provider, "configure_hotel", None)
        if callable(configure_hotel):
            configure_hotel(hotel_name)
    return provider
