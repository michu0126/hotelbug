"""Validated monitoring scopes; legacy single-hotel request bodies still work."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.domain import ProviderName

SCOPES = ("hotel", "city", "country", "brand", "provider")
TARGET_FIELDS = {
    "hotel": "hotel_id",
    "city": "city",
    "country": "country",
    "brand": "brand",
    "provider": "provider",
}


class WatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["hotel", "city", "country", "brand", "provider"] = "hotel"
    name: str | None = Field(default=None, min_length=1, max_length=200)
    hotel_id: str | None = Field(default=None, min_length=1, max_length=36)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    country: str | None = Field(default=None, min_length=1, max_length=100)
    brand: str | None = Field(default=None, min_length=1, max_length=100)
    provider: ProviderName | None = None
    days_ahead: int = Field(default=30, ge=1, le=365)
    enabled: bool = True

    @field_validator("name", "hotel_id", "city", "country", "brand", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def target(self) -> Self:
        field = TARGET_FIELDS[self.scope]
        if getattr(self, field) is None:
            raise ValueError(f"{self.scope} scope requires {field}")
        for key in ("hotel_id", "city", "country", "brand"):
            if key != field and getattr(self, key) is not None:
                raise ValueError(f"{key} does not belong to {self.scope} scope")
        if self.scope == "hotel" and self.provider is not None:
            raise ValueError("Hotel provider is determined from the hotel record")
        return self

    def filters(self) -> dict:
        field = TARGET_FIELDS[self.scope]
        target = getattr(self, field)
        result = {
            # Preserve official metadata spelling. Python casefold expands
            # e.g. German ß to ss, whereas database lower() does not.
            field: target,
            "days_ahead": self.days_ahead,
        }
        if self.provider is not None:
            result["provider"] = self.provider.value
        return result


class WatchToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
