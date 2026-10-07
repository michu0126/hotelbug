import argparse
import asyncio
import json
from datetime import date, timedelta

from app.core.errors import ProviderError
from app.core.logging import redact
from app.providers.accor_browser import AccorBrowserProvider
from app.providers.browser_session import close_browser_sessions
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--code", default="0338")
    parser.add_argument("--session-dir")
    parser.add_argument("--with-details", action="store_true")
    parser.add_argument("--adults", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--repeat-adults", type=int, choices=range(1, 9), nargs="*", default=[])
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 7))
    args = parser.parse_args()
    provider = AccorBrowserProvider(
        proxy_server=args.proxy, browser_channel=args.channel, session_dir=args.session_dir
    )
    page_errors = []
    navigation_failures = []
    attached_pages = set()

    def observe_page(page):
        if page in attached_pages:
            return
        attached_pages.add(page)
        page.on("pageerror", lambda error: page_errors.append(redact(str(error))))
        page.on(
            "requestfailed",
            lambda failed: (
                navigation_failures.append({"url": redact(failed.url), "failure": failed.failure})
                if failed.resource_type == "document"
                else None
            ),
        )

    try:
        page = await provider._get_page()
        observe_page(page)
        if args.with_details:
            print((await provider.get_hotel_details(args.code)).model_dump_json(), flush=True)
            await provider.close()
        # Repeat ordinary form queries in the same dedicated session, without
        # importing another browser's cookies or sending any notifications.
        for adults in [args.adults, *args.repeat_adults]:
            page_errors.clear()
            navigation_failures.clear()
            observe_page(await provider._get_page())
            rates = await provider.search_rates(
                RateRequest(
                    provider_hotel_id=args.code,
                    check_in=args.date,
                    check_out=args.date + timedelta(days=1),
                    adults=adults,
                )
            )
            print(json.dumps([r.model_dump(mode="json") for r in rates], ensure_ascii=True), flush=True)
            print("adults=", adults, "verified_public_quotes=", len(rates), flush=True)
    except ProviderError as exc:
        print("provider_error=", exc.code, str(exc), flush=True)
        if exc.__cause__:
            print("cause=", redact(str(exc.__cause__)), flush=True)
        page = provider._page
        if page and not page.is_closed():
            # Public, named form state only: never dump cookies, hidden inputs,
            # credentials, localStorage, or network authorization headers.
            state = {"url": redact(page.url)}
            state["page_errors"] = page_errors[-10:]
            state["navigation_failures"] = navigation_failures[-10:]
            state["session_pages"] = [redact(item.url) for item in page.context.pages if not item.is_closed()]
            for selector in (
                "#booking-date-in",
                "#booking-date-out",
                "#search-room-number",
                "#search-adult-room-0",
                "#search-children-room-0",
            ):
                control = page.locator(selector)
                if await control.count() == 1:
                    state[selector] = {
                        "value": await control.input_value(),
                        "invalid": await control.get_attribute("aria-invalid"),
                    }
            summary = page.locator("#compo-summary")
            if await summary.count() == 1:
                state["guest_summary"] = await summary.inner_text()
            form = page.locator("form").filter(has=page.locator("#booking-date-in"))
            if await form.count() == 1:
                state["form_target"] = await form.get_attribute("target")
                state["form_text"] = (await form.inner_text())[:1500]
                state["invalid_controls"] = await form.locator("input,select,textarea").evaluate_all(
                    "els => els.filter(e => !e.validity.valid).map(e => ({id:e.id, message:e.validationMessage}))"
                )
            print("public_form_state=", json.dumps(state, ensure_ascii=True), flush=True)
        raise SystemExit(2) from None
    finally:
        await provider.close()
        await close_browser_sessions()


if __name__ == "__main__":
    asyncio.run(main())
