"""Configurable scoring policy, independent of notification eligibility."""

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnomalyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drop_20: int = Field(default=10, ge=0, le=100)
    drop_30: int = Field(default=15, ge=0, le=100)
    drop_40: int = Field(default=20, ge=0, le=100)
    drop_50: int = Field(default=25, ge=0, le=100)
    drop_70: int = Field(default=30, ge=0, le=100)
    neighbor: int = Field(default=15, ge=0, le=100)
    near_low: int = Field(default=10, ge=0, le=100)
    new_low: int = Field(default=20, ge=0, le=100)
    verified: int = Field(default=10, ge=0, le=100)
    consistent_history: int = Field(default=15, ge=0, le=100)
    neighbor_drop_fraction: Decimal = Field(default=Decimal("0.3"), gt=0, lt=1)
    near_low_margin: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    support_min_days: int = Field(default=6, ge=3, le=90)
    calendar_max_age_hours: int = Field(default=72, ge=1, le=168)
    low_threshold: int = Field(default=40, ge=1, le=100)
    very_low_threshold: int = Field(default=60, ge=1, le=100)
    possible_bug_threshold: int = Field(default=80, ge=1, le=100)

    @model_validator(mode="after")
    def ordered_levels(self) -> Self:
        if not self.low_threshold < self.very_low_threshold < self.possible_bug_threshold:
            raise ValueError("Scoring level thresholds must be strictly increasing")
        return self
