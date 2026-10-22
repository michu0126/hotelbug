"""The CN passive page flow in the Worker's existing persistent browser session."""

from app.providers.marriott_hybrid import MarriottHybridProvider
from app.providers.marriott_passive_bridge import MarriottPassiveBridgeProvider


class MarriottWorkerProvider(MarriottHybridProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cn_provider = None
        self._session_options = kwargs

    def configure_hotel(self, hotel_name):
        super().configure_hotel(hotel_name)
        if self._cn_provider is not None:
            self._cn_provider.configure_hotel(hotel_name)

    def configure_hotel_url(self, official_url):
        super().configure_hotel_url(official_url)
        if self._root == "https://www.marriott.com.cn":
            self._cn_provider = MarriottPassiveBridgeProvider(
                use_backend_session=True,
                **self._session_options,
            )
            if self._hotel_name:
                self._cn_provider.configure_hotel(self._hotel_name)
            self._cn_provider.configure_hotel_url(official_url)

    async def search_rates(self, search):
        if self._cn_provider is not None:
            return await self._cn_provider.search_rates(search)
        return await super().search_rates(search)

    async def close(self):
        try:
            if self._cn_provider is not None:
                await self._cn_provider.close()
        finally:
            await super().close()
