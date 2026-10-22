"""Join already-observed, sanitized checkout evidence to exactly one offer.

This module never creates carts or calls checkout endpoints. The input is a
whitelisted evidence file, not a raw cart/account/payment response.
"""

import re
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adapters.marriott import SOURCE_SITES, MarriottSampleError
from models import CancellationPolicy

EVIDENCE_KIND = "marriott_selected_checkout_v1"
CN_DEADLINE = re.compile(r"酒店当地时间夜间\s*11:59\s*之前免费取消.*?截止日期为\s*(\d{4}-\d{2}-\d{2})")
CANCELLATION_RULES = {"CPC", "CP", "CF"}


def explicit_deadline(descriptions, hotel_timezone):
    """Only the observed local 23:59 text + an explicit IANA zone is supported."""
    if not hotel_timezone:
        return None
    try:
        zone = ZoneInfo(hotel_timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise MarriottSampleError("Checkout hotel timezone is invalid") from None
    matches = {match.group(1) for text in descriptions if (match := CN_DEADLINE.search(text))}
    if not matches:
        return None
    if len(matches) != 1:
        raise MarriottSampleError("Cancellation deadline evidence conflicts")
    return datetime.combine(date.fromisoformat(matches.pop()), time(23, 59), zone)


def apply_checkout_evidence(record, evidence):
    try:
        if evidence.get("kind") != EVIDENCE_KIND or evidence.get("source_site") not in SOURCE_SITES:
            raise MarriottSampleError("Unknown sanitized checkout evidence format")
        identity = evidence["product_identity"]
        if not isinstance(identity, list) or len(identity) != 5 or type(evidence["member_rate"]) is not bool:
            raise MarriottSampleError("Checkout identity or membership is incomplete")
        fingerprint = evidence["price_response_hash"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise MarriottSampleError("Checkout evidence must bind to an exact observed price response")
        if (
            identity
            != [
                record.hotel_id,
                record.rate_code,
                record.room_code.upper(),
                str(record.checkin),
                str(record.checkout),
            ]
            or evidence["source_site"] != record.source_site
            or evidence["currency"] != record.currency
            or evidence["member_rate"] != record.member_rate
            or evidence["adults"] != record.adults
            or evidence["rooms"] != record.rooms
            or fingerprint != record.response_hash
        ):
            return record
        amounts = {
            key: Decimal(str(evidence["totals"][key])) for key in ("subtotal", "fees", "taxes", "total")
        }
        if any(not value.is_finite() or value < 0 for value in amounts.values()):
            raise MarriottSampleError("Invalid explicit checkout money")
        if (
            amounts["subtotal"] + amounts["fees"] + amounts["taxes"] != amounts["total"]
            or amounts["subtotal"] != record.amount_excluding_tax
            or amounts["total"] != record.amount_including_tax
            or (record.mandatory_fees is not None and amounts["fees"] != record.mandatory_fees)
        ):
            raise MarriottSampleError("Checkout totals do not match the selected offer")
        descriptions, flags = [], set()
        for rule in evidence["rules"]:
            if rule["code"] not in CANCELLATION_RULES:
                continue
            values = rule["descriptions"]
            if not isinstance(values, list) or any(not isinstance(text, str) for text in values):
                raise MarriottSampleError("Invalid cancellation text")
            descriptions.extend(text for text in values if text not in descriptions)
            flag = rule.get("non_refundable")
            if flag is not None:
                if type(flag) is not bool:
                    raise MarriottSampleError("Invalid cancellation flag")
                flags.add(flag)
        if len(flags) > 1:
            raise MarriottSampleError("Cancellation flags conflict")
        policy = CancellationPolicy(
            free_cancellation_until=record.cancellation_policy.free_cancellation_until,
            policy_text="\n".join(descriptions) or None,
            non_refundable=next(iter(flags)) if flags else None,
            deadline_at=explicit_deadline(descriptions, evidence.get("hotel_timezone")),
        )
        return replace(
            record, tax_amount=amounts["taxes"], mandatory_fees=amounts["fees"], cancellation_policy=policy
        )
    except MarriottSampleError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise MarriottSampleError("Sanitized checkout evidence is incomplete or changed") from None
