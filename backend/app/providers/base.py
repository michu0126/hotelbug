from abc import ABC, abstractmethod

from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest


class HotelProvider(ABC):
    """Adapters own source-specific parsing; domain services only receive normalized data."""

    @abstractmethod
    async def search_hotels(self, query: dict) -> list[HotelData]: ...

    @abstractmethod
    async def get_hotel_details(self, hotel_id: str) -> HotelData: ...

    @abstractmethod
    async def search_rates(self, request: RateRequest) -> list[RateData]: ...

    @abstractmethod
    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]: ...

    @abstractmethod
    async def health_check(self) -> HealthResult: ...

    async def close(self) -> None:
        """Release HTTP/browser resources owned by this adapter."""
