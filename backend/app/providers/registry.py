from collections.abc import Callable
from contextvars import ContextVar

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider

_runtime_settings: ContextVar[Settings | None] = ContextVar("provider_settings", default=None)


def provider_settings() -> Settings:
    return _runtime_settings.get() or get_settings()


def marriott_browser() -> HotelProvider:
    try:
        from app.providers.marriott_browser import MarriottBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc

    settings = provider_settings()
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

    settings = provider_settings()
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
    settings = provider_settings()
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
    settings = provider_settings()
    return GHABrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


def ihg_browser() -> HotelProvider:
    try:
        from app.providers.ihg_browser import IHGBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc
    settings = provider_settings()
    return IHGBrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


def hyatt_browser() -> HotelProvider:
    try:
        from app.providers.hyatt_browser import HyattBrowserProvider
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.BROWSER_UNAVAILABLE, "Install the browser-enabled Worker image"
        ) from exc
    settings = provider_settings()
    return HyattBrowserProvider(
        proxy_server=settings.browser_proxy_url.get_secret_value() or None,
        browser_channel=settings.browser_channel or None,
        session_dir=settings.browser_session_dir,
    )


FACTORIES: dict[str, Callable[[], HotelProvider]] = {
    "marriott": marriott_browser,
    "accor": accor_browser,
    "gha": gha_browser,
    "hilton": hilton_browser,
    "ihg": ihg_browser,
    "hyatt": hyatt_browser,
}


def create_provider(
    name: str,
    hotel_name: str | None = None,
    official_url: str | None = None,
    settings: Settings | None = None,
) -> HotelProvider:
    if name not in FACTORIES:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Provider pending verified research and implementation"
        )
    token = _runtime_settings.set(settings)
    try:
        provider = FACTORIES[name]()
    finally:
        _runtime_settings.reset(token)
    if hotel_name:
        configure_hotel = getattr(provider, "configure_hotel", None)
        if callable(configure_hotel):
            configure_hotel(hotel_name)
    if official_url:
        configure_url = getattr(provider, "configure_hotel_url", None)
        if callable(configure_url):
            configure_url(official_url)
    return provider
