"""Project-owned main-document events only; no XHR, cookies, headers or body."""

import json
import re
from urllib.parse import urlparse

from app.providers.registry import FACTORIES

PATHS = {"/en/", "/en/search/", "/en/book/reservation/rooms/", "/en/book/reservation/rates/"}


def public_path(url):
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc == "www.hilton.com" and parsed.path in PATHS:
        return parsed.path
    return None


def attach_navigation_diagnostics(page, events, seen):
    if id(page) in seen:
        return
    number = len(seen) + 1
    seen.add(id(page))

    def append(event, path, **fields):
        if path and len(events) < 64:
            events.append({"event": event, "page": number, "public_path": path, **fields})

    def record_request(request, event, status=None):
        try:
            if (
                request.resource_type != "document"
                or not request.is_navigation_request()
                or request.frame != page.main_frame
            ):
                return
            path = public_path(request.url)
            fields = {}
            if event == "DOCUMENT_RESPONSE":
                fields["status"] = status if isinstance(status, int) and 100 <= status <= 599 else None
            if event == "DOCUMENT_FAILED":
                failure = request.failure
                match = re.fullmatch(r"net::(ERR_[A-Z0-9_]+)", failure or "")
                fields["transport_error"] = match[1] if match else "UNKNOWN"
            append(event, path, **fields)
        except Exception:
            # A closing/disconnected page must not replace the actual query error.
            return

    def record_response(response):
        try:
            record_request(response.request, "DOCUMENT_RESPONSE", response.status)
        except Exception:
            return

    def record_commit(frame):
        try:
            if frame == page.main_frame:
                append("DOCUMENT_COMMITTED", public_path(frame.url))
        except Exception:
            return

    def observe_popup(popup):
        try:
            attach_navigation_diagnostics(popup, events, seen)
        except Exception:
            if len(events) < 64:
                events.append({"event": "DOCUMENT_DIAGNOSTIC_UNAVAILABLE"})

    page.on("request", lambda request: record_request(request, "DOCUMENT_STARTED"))
    page.on("response", record_response)
    page.on("requestfailed", lambda request: record_request(request, "DOCUMENT_FAILED"))
    page.on("requestfinished", lambda request: record_request(request, "DOCUMENT_FINISHED"))
    page.on("framenavigated", record_commit)
    page.on("popup", observe_popup)


def install_navigation_diagnostics():
    factory = FACTORIES["hilton"]

    def diagnostic_factory():
        instance = factory()
        get_page, search_rates = instance._get_page, instance.search_rates
        events, seen = [], set()

        async def get_observed_page():
            page = await get_page()
            try:
                attach_navigation_diagnostics(page, events, seen)
            except Exception:
                if len(events) < 64:
                    events.append({"event": "DOCUMENT_DIAGNOSTIC_UNAVAILABLE"})
            return page

        async def read_rates(request):
            try:
                return await search_rates(request)
            finally:
                print(json.dumps({"public_hilton_navigation": events}), flush=True)

        instance._get_page = get_observed_page
        instance.search_rates = read_rates
        return instance

    FACTORIES["hilton"] = diagnostic_factory
