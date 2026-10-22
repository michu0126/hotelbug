"""Observed Marriott cash JSON with separate sample and ordinary-page live modes."""

import asyncio
import base64
import binascii
import json
import re
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from adapters.base import BrandBlocked, PendingAdapter, RateLimited, SamplesRequired
from models import CancellationPolicy, PriceRecord, utc

ROOT = "https://www.marriott.com"
RATES_OPERATION = "PhoenixBookDTTSearchProductsByProperty"
RATES_PATH = f"/mi/query/{RATES_OPERATION}"
DEFAULT_SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "marriott"
DEFAULT_CAPTURE_DIR = Path(__file__).resolve().parents[2] / "captures" / "marriott"
SOURCE_SITES = {"www.marriott.com", "www.marriott.com.cn"}
CASH_MODE = "HotelRoomRateModesCash"
POINTS_MODES = {
    "HotelRoomRateModesPoints",
    "HotelRoomRateModesCashAndPoints",
    "HotelRoomRateModesPointsPlusCashUpgrade",
}


class MarriottSampleError(ValueError):
    """Bad or mismatched sample; never represent it as zero availability."""


def _stay(hotel_id, checkin, checkout):
    if not isinstance(hotel_id, str) or not re.fullmatch(r"[A-Za-z0-9]{5}", hotel_id):
        raise MarriottSampleError("Marriott hotel ID must contain five letters or digits")
    try:
        ci = date.fromisoformat(checkin) if isinstance(checkin, str) else checkin
        co = date.fromisoformat(checkout) if isinstance(checkout, str) else checkout
        if type(ci) is not date or type(co) is not date or co <= ci:
            raise ValueError
    except (TypeError, ValueError):
        raise MarriottSampleError("Explicit valid check-in/check-out dates are required") from None
    return hotel_id.upper(), ci, co


def _money(value):
    if not isinstance(value, dict):
        raise MarriottSampleError("Explicit monetary amount is missing")
    amount, places, currency = value.get("amount"), value.get("decimalPoint"), value.get("currency")
    if type(amount) is not int or amount < 0 or type(places) is not int or not 0 <= places <= 6:
        raise MarriottSampleError("Invalid minor-unit amount or decimalPoint")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise MarriottSampleError("Explicit currency is missing or invalid")
    return Decimal(amount).scaleb(-places), currency


def _identity(node):
    try:
        encoded = node["id"]
        result = (
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            .decode()
            .split("|")
        )
        if len(result) != 6:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise MarriottSampleError("Invalid Marriott product identity") from None


def parse_prices(
    body,
    hotel_id,
    checkin,
    checkout,
    *,
    adults=None,
    captured_at=None,
    source_kind="SAMPLE",
    source_site="www.marriott.com",
) -> list[PriceRecord]:
    """Read single-room stay totals; do not infer tax or refund terms from names."""
    code, ci, co = _stay(hotel_id, checkin, checkout)
    if source_site not in SOURCE_SITES:
        raise MarriottSampleError("Unknown Marriott price source site")
    try:
        if not isinstance(body, dict) or body.get("errors"):
            raise MarriottSampleError("GraphQL error or invalid response")
        connection = body["data"]["commerce"]["product"]["searchProductsByProperty"]
        edges = connection["edges"]
        if (
            connection["__typename"] != "ProductSearchByPropertyConnection"
            or not isinstance(edges, list)
            or type(connection["total"]) is not int
            or connection["total"] != len(edges)
        ):
            raise MarriottSampleError("Incomplete Marriott rate result or unhandled pagination")
        digest = sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        result = {}
        for edge in edges:
            node = edge["node"]
            if node["__typename"] != "HotelRoom":
                raise MarriottSampleError("Unknown product type")
            basic, rate, pricing = node["basicInformation"], node["rates"], node["totalPricing"]
            kind = rate["rateModes"]["__typename"]
            if kind in POINTS_MODES:
                continue
            if kind != CASH_MODE:
                raise MarriottSampleError("Unknown cash price schema")
            plans = basic["ratePlan"]
            if not isinstance(plans, list) or len(plans) != 1:
                raise MarriottSampleError("Rate plan identity is ambiguous")
            plan_code, room_code = plans[0]["ratePlanCode"], basic["type"]
            if _identity(node)[:5] != [code, plan_code, room_code.upper(), str(ci), str(co)]:
                raise MarriottSampleError("Hotel, dates, room or rate plan do not match the request")
            if type(basic.get("isMembersOnly")) is not bool:
                raise MarriottSampleError("Public/member eligibility is unknown")
            if type(pricing["quantity"]) is not int or pricing["quantity"] != 1:
                raise MarriottSampleError("Only single-room totals are supported")
            modes = pricing["rateModes"]
            base, currency = _money(modes["subtotalPerQuantity"]["amount"])
            total, total_currency = _money(modes["grandTotal"]["amount"])
            nightly, nightly_currency = _money(rate["rateModes"]["averageNightlyRatePerUnit"]["amount"])
            if currency != total_currency or currency != nightly_currency or total < base:
                raise MarriottSampleError("Inconsistent price currency or tax basis")
            if (co - ci).days == 1 and base != nightly:
                raise MarriottSampleError("Single-night base and nightly amounts disagree")
            fees = None
            if modes.get("totalMandatoryFeesPerQuantity") is not None:
                fees, fee_currency = _money(modes["totalMandatoryFeesPerQuantity"]["amount"])
                if fee_currency != currency:
                    raise MarriottSampleError("Mandatory-fee currency mismatch")
            deadline = basic.get("freeCancellationUntil")
            if deadline is not None and not isinstance(deadline, str):
                raise MarriottSampleError("Cancellation deadline schema changed")
            description = rate.get("description")
            if description is not None and not isinstance(description, str):
                raise MarriottSampleError("Rate description schema changed")
            record = PriceRecord(
                brand="marriott",
                hotel_id=code,
                checkin=ci,
                checkout=co,
                room_code=room_code,
                room_name=basic["name"],
                rate_code=plan_code,
                rate_name=rate["name"],
                member_rate=basic["isMembersOnly"],
                amount_excluding_tax=base,
                amount_including_tax=total,
                average_nightly_excluding_tax=nightly,
                mandatory_fees=fees,
                currency=currency,
                cancellation_policy=CancellationPolicy(free_cancellation_until=deadline or None),
                source_url="https://" + source_site + RATES_PATH,
                response_hash=digest,
                adults=adults,
                captured_at=captured_at,
                source_kind=source_kind,
                rate_description=description,
                # Total minus base includes fees, not an independently verified tax.
                tax_amount=None,
                deposit_required=basic.get("depositRequired"),
            )
            previous = result.get(record.series_key)
            if previous is not None and previous != record:
                raise MarriottSampleError("Conflicting duplicate offers")
            result[record.series_key] = record
        if edges and not result:
            raise MarriottSampleError("Sample contains no verified cash offers")
        return list(result.values())
    except MarriottSampleError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MarriottSampleError("Marriott price response schema is incomplete or changed") from None


def _captured_prices(document, hotel_id, checkin, checkout):
    """None is unrelated traffic/stay, not an empty availability result."""
    body, adults, captured_at = document, None, None
    source_site = "www.marriott.com"
    if not isinstance(document, dict):
        return None
    if "source_site" in document and "response" in document:
        source_site = document["source_site"]
        if source_site not in SOURCE_SITES or document.get("operation") != RATES_OPERATION:
            raise MarriottSampleError("Unknown passive capture source or operation")
        variables = document["variables"]
        search, options = variables["search"], variables["search"]["options"]
        if (document["property_id"], document["check_in"], document["check_out"]) != (
            search["propertyId"],
            options["startDate"],
            options["endDate"],
        ):
            raise MarriottSampleError("Capture metadata and request disagree")
        if (search["propertyId"], options["startDate"], options["endDate"]) != (
            hotel_id,
            str(checkin),
            str(checkout),
        ):
            return None
        if (
            type(options["quantity"]) is not int
            or options["quantity"] != 1
            or options["childAges"] != []
            or type(options["numberInParty"]) is not int
            or options["numberInParty"] < 1
        ):
            raise MarriottSampleError("Unsupported passive capture occupancy")
        offset = variables.get("offset", 0)
        if type(offset) is not int or offset != 0:
            raise MarriottSampleError(
                "Partial pagination is retained, not silently merged as a complete result"
            )
        adults = options["numberInParty"]
        captured_at = utc(datetime.fromisoformat(document["captured_at"].replace("Z", "+00:00")))
        body = document["response"]
    elif "url" in document:
        url = urlsplit(document["url"])
        if url.scheme != "https" or url.netloc not in SOURCE_SITES or url.path != RATES_PATH:
            return None
        source_site = url.hostname
        if document.get("method") != "POST":
            raise MarriottSampleError("Price envelope has an unexpected method")
        try:
            request = json.loads(document["request_post_data"])
            search = request["variables"]["search"]
            options = search["options"]
            if request["operationName"] != RATES_OPERATION:
                raise MarriottSampleError("Price operation mismatch")
            if (search["propertyId"], options["startDate"], options["endDate"]) != (
                hotel_id,
                str(checkin),
                str(checkout),
            ):
                return None
            if options["quantity"] != 1 or type(options["quantity"]) is not int or options["childAges"] != []:
                raise MarriottSampleError("Only one-room adult-only request samples are supported")
            adults = options["numberInParty"]
            if type(adults) is not int or adults < 1:
                raise MarriottSampleError("Invalid requested occupancy")
            if document.get("captured_at"):
                captured_at = utc(datetime.fromisoformat(document["captured_at"].replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            if isinstance(exc, MarriottSampleError):
                raise
            raise MarriottSampleError("Missing or invalid capture request context") from None
        status = document.get("status")
        if status == 403:
            raise BrandBlocked("Captured Marriott price response returned HTTP 403")
        if status == 429:
            raise RateLimited()
        if status != 200:
            raise MarriottSampleError("Captured price request did not succeed")
        try:
            body = json.loads(document["response_text"])
        except (KeyError, TypeError, ValueError):
            raise MarriottSampleError("Price response body is not JSON") from None
    else:
        body = document.get("rates", document)
        try:
            connection = body["data"]["commerce"]["product"]["searchProductsByProperty"]
        except (KeyError, TypeError):
            return None
        edges = connection.get("edges")
        if not edges:
            raise MarriottSampleError("An empty raw response cannot verify hotel/stay without its request")
        identities = [_identity(edge["node"]) for edge in edges]
        contexts = {(identity[0], identity[3], identity[4]) for identity in identities}
        expected = (hotel_id, str(checkin), str(checkout))
        if expected not in contexts:
            return None
        if contexts != {expected}:
            raise MarriottSampleError("Raw response mixes hotel or stay identities")
    return parse_prices(
        body, hotel_id, checkin, checkout, adults=adults, captured_at=captured_at, source_site=source_site
    )


def parse_capture(document) -> list[PriceRecord]:
    """Parse one observed envelope without assigning live provenance or issuing a request."""
    try:
        code, ci, co = _stay(document["property_id"], document["check_in"], document["check_out"])
        records = _captured_prices(document, code, ci, co)
        if records is None:
            raise MarriottSampleError("Capture did not match its stated hotel/stay")
        return records
    except MarriottSampleError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MarriottSampleError("Captured price context is incomplete or changed") from None


class MarriottAdapter(PendingAdapter):
    """Explicit single-query live mode; automatic fetch_rates monitoring stays off."""

    brand = "marriott"

    def __init__(
        self,
        sample_paths=None,
        *,
        mode="sample",
        browser_settings=None,
        adults=2,
        configure_context=None,
        profile_dir=None,
        hotel_name=None,
        checkout_paths=None,
    ):
        if mode not in ("sample", "live") or (mode == "live" and sample_paths is not None):
            raise ValueError("Choose either explicit live mode or local samples, never a sample fallback")
        if type(adults) is not int or adults < 1:
            raise ValueError("A positive adult count is required")
        self.mode = mode
        self._live = None
        self._browser_settings, self._adults = browser_settings, adults
        self._profile_dir, self._hotel_name = profile_dir, hotel_name
        self._configure_context = configure_context  # Offline browser test fixtures only.
        self.sample_paths = None if sample_paths is None else tuple(Path(p) for p in sample_paths)
        self.checkout_paths = None if checkout_paths is None else tuple(Path(p) for p in checkout_paths)
        self._lock = asyncio.Lock()

    async def fetch_prices(self, hotel_id, checkin, checkout) -> list[PriceRecord]:
        code, ci, co = _stay(hotel_id, checkin, checkout)
        async with self._lock:
            if self.mode == "live":
                if self._live is None:
                    from adapters.marriott_browser import MarriottLivePage

                    self._live = MarriottLivePage(
                        self._browser_settings,
                        adults=self._adults,
                        configure_context=self._configure_context,
                        profile_dir=self._profile_dir,
                        hotel_name=self._hotel_name,
                    )
                return await self._live.fetch_prices(code, ci, co)
            paths = (
                self.sample_paths
                if self.sample_paths is not None
                else sorted(DEFAULT_CAPTURE_DIR.glob("*.json")) + sorted(DEFAULT_SAMPLE_DIR.glob("*.json"))
            )
            result, matched = {}, False
            for path in paths:
                try:
                    text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
                    document = json.loads(text)
                except (OSError, ValueError):
                    raise MarriottSampleError("Sample file could not be read as JSON") from None
                try:
                    records = _captured_prices(document, code, ci, co)
                except MarriottSampleError:
                    raise
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise MarriottSampleError("Captured price context is incomplete or changed") from None
                if records is None:
                    continue
                matched = True
                for record in records:
                    key = (record.series_key, record.response_hash, record.captured_at)
                    result[key] = record
            if not matched:
                raise SamplesRequired("No Marriott price sample matches this hotel and stay")
            records = list(result.values())
            checkout_paths = (
                self.checkout_paths
                if self.checkout_paths is not None
                else sorted((DEFAULT_CAPTURE_DIR / "checkout").glob("*.json"))
            )
            if checkout_paths:
                from adapters.marriott_checkout import apply_checkout_evidence

                for path in checkout_paths:
                    try:
                        evidence = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        raise MarriottSampleError("Checkout evidence is unreadable") from None
                    records = [apply_checkout_evidence(record, evidence) for record in records]
            return records

    async def close(self):
        if self._live is not None:
            await self._live.close()

    @property
    def diagnostics(self):
        return dict(self._live.diagnostics) if self._live is not None else {}


async def fetch_prices(hotel_id, checkin, checkout) -> list[PriceRecord]:
    """Read passive captures and legacy local samples, never issue a web request."""
    return await MarriottAdapter().fetch_prices(hotel_id, checkin, checkout)
