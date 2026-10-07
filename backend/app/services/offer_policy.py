"""Classify explicit offer labels without guessing opaque provider rate codes.

Classification does not discard quotes or alter their comparable identity. Only
known special offers are excluded from public cash anomaly notifications; an
unrecognized public label remains eligible, not blocked by a whitelist.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class Offer(Protocol):
    rate_name: str | None
    member_rate: bool | None
    points_price: int | None
    cash_price: Decimal | None
    total_price: Decimal | None
    room_type: str | None
    room_code: str | None
    rate_code: str | None


# Match plan names only, not marketing copy from another plan, room names or
# codes such as IHG IGCOR (observed Best Flexible Rate, NOT a corporate quote).
# English and Chinese literal label support is not a universal semantic parser.
LABELS = {
    "MEMBER_RATE": re.compile(
        r"\bmembers?(?:\s+only)?\s+(?:rate|price|discount|offer|exclusive)\b"
        r"|\bmembers?\s+only\b|会员(?:价|专享|优惠)|會員(?:價|專享|優惠)"
    ),
    "CORPORATE_RATE": re.compile(
        r"\bcorporate(?:\s+negotiated)?\s+(?:rate|price|offer|discount)\b"
        r"|\bcompany\s+(?:negotiated\s+)?rate\b|\bnegotiated\s+rate\b"
        r"|公司(?:协议|協議|合约|合約)价|企業協議價|企业协议价|协议价|協議價"
    ),
    "RESIDENT_RATE": re.compile(
        r"\b(?:local\s+)?residents?\s+(?:rate|price|offer|discount|special)\b"
        r"|\blocal\s+residence\s+special\b"
        r"|本地居民(?:价|優惠|优惠|专享)|当地居民(?:价|優惠|优惠|专享)|當地居民(?:價|優惠|專享)"
    ),
    "ADVANCE_PURCHASE": re.compile(
        r"\badvance\s+purchase\b|\badvanced\s+purchase\b"
        r"|\bbook\s+early\s+(?:&|and)\s+save\b|\bearly\s+bird\b"
        r"|提前(?:购买|購買|预付|預付)|早鸟(?:价|优惠)?|早鳥(?:價|優惠)?"
    ),
    "PACKAGE": re.compile(r"\bpackages?\b|套餐|套票"),
    "POINTS_PLUS_CASH": re.compile(
        r"\bpoints?\s*(?:\+|&|and)\s*cash\b|积分\s*(?:\+|加)\s*现金|積分\s*(?:\+|加)\s*現金"
    ),
}
NON_MEMBER = re.compile(r"\bnon\s*-?\s*members?\b|非会员|非會員")


@dataclass(frozen=True)
class OfferClassification:
    tags: tuple[str, ...]
    evidence: dict[str, str]

    @property
    def alert_eligible(self) -> bool:
        return not self.tags

    def as_dict(self) -> dict:
        return {
            "alert_eligible": self.alert_eligible,
            "tags": list(self.tags),
            "evidence": dict(self.evidence),
        }


def classify_offer(offer: Offer) -> OfferClassification:
    evidence: dict[str, str] = {}
    if offer.member_rate is True:
        evidence["MEMBER_RATE"] = "member_rate=true"
    if offer.points_price is not None:
        kind = (
            "POINTS_PLUS_CASH"
            if offer.cash_price is not None or offer.total_price is not None
            else "POINTS_ONLY"
        )
        evidence[kind] = "points_price_present"
    name = unicodedata.normalize("NFKC", offer.rate_name or "").casefold()
    # Typographic dashes and line breaks are presentation, not plan eligibility.
    name = " ".join(name.translate(str.maketrans({"–": "-", "—": "-", "‑": "-"})).split())
    for tag, pattern in LABELS.items():
        searchable = NON_MEMBER.sub("", name) if tag == "MEMBER_RATE" else name
        match = pattern.search(searchable)
        if match:
            evidence.setdefault(tag, "rate_name:" + match.group(0))
    if not (offer.room_type or offer.room_code) or not (offer.rate_name or offer.rate_code):
        evidence["UNIDENTIFIED_OFFER"] = "room_or_plan_identity_missing"
    return OfferClassification(tuple(evidence), evidence)
