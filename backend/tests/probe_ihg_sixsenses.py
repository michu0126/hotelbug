"""One ordinary public Six Senses hotel-link click, no API probing or quote writes."""

import json
import re
from urllib.parse import urlparse

from sqlalchemy import select

from app.models.tables import AppSetting, utcnow
from app.providers.registry import FACTORIES


def booking_link_projection(links):
    """Only explicit booking words; 'facebook' is not a booking control."""
    words = re.compile(
        r"(^|[^a-z])(book|reserve|reservation|reservations|availability|available)([^a-z]|$)", re.I
    )
    return [
        {key: item.get(key) for key in ("text", "hostname", "path")}
        for item in links
        if isinstance(item.get("text"), str) and words.search(item["text"])
    ]


async def follow_public_hotel(page, hotel_name):
    # Accessible names may differ from the displayed hotel heading. Match the
    # already observed exact text within hotel links only.
    link = page.locator("h4 a[href]").filter(has_text=re.compile(r"^\s*" + re.escape(hotel_name) + r"\s*$"))
    count = await link.count()
    if count != 1 or not await link.is_visible():
        return {"outcome": "LINK_NOT_UNIQUE_OR_VISIBLE", "matching_links": count}
    href = urlparse(await link.get_attribute("href") or "")
    if href.scheme != "https" or href.hostname != "www.sixsenses.com":
        return {"outcome": "LINK_TARGET_UNVERIFIED"}
    target = page
    try:
        if await link.get_attribute("target") == "_blank":
            async with page.expect_popup(timeout=30000) as popup:
                await link.click()
            target = await popup.value
            await target.wait_for_load_state("domcontentloaded", timeout=30000)
        else:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                await link.click()
        final = urlparse(target.url)
        result = {
            "public_url": final._replace(query="", fragment="").geturl(),
            "hostname": final.hostname,
        }
        if final.scheme != "https" or final.hostname not in {"www.sixsenses.com", "sixsenses.com"}:
            return {**result, "outcome": "DESTINATION_UNVERIFIED"}
        title = await target.title()
        if "access denied" in title.casefold() or "just a moment" in title.casefold():
            return {**result, "outcome": "ACCESS_REJECTED"}
        await target.locator("h1").first.wait_for(state="visible", timeout=20000)
        result.update(
            outcome="PUBLIC_PAGE_READ",
            headings=await target.locator("h1").all_inner_texts(),
            booking_links=booking_link_projection(
                await target.locator("a").evaluate_all(
                    """es => es.filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                    .map(e => ({text: e.innerText.trim().slice(0, 160),
                        hostname: new URL(e.href).hostname, path: new URL(e.href).pathname}))"""
                )
            ),
        )
        return result
    finally:
        if target is not page:
            await target.close()


def install_public_link_probe(sessions):
    factory = FACTORIES["ihg"]
    attempted = False

    def probed_factory():
        instance = factory()
        original = instance.discover_catalog

        async def discover(query):
            nonlocal attempted
            result = await original(query)
            if attempted:
                return result
            # This exact target was observed in real IHG directory headings.
            # Derive names from this normal loaded page, not a guessed URL list.
            links = await instance._page.locator("h4 a[href]").evaluate_all(
                """es => es.filter(e => e.hostname === 'www.sixsenses.com')
                    .map(e => e.innerText.trim()).filter(Boolean)"""
            )
            for name in links:
                key = "audit:six-senses-public-link:" + name
                async with sessions() as db, db.begin():
                    marker = await db.scalar(select(AppSetting).where(AppSetting.key == key))
                    if marker:
                        continue  # Persisted evidence; never repeat an observed link.
                    db.add(AppSetting(key=key, value={"status": "STARTED", "at": utcnow().isoformat()}))
                attempted = True
                try:
                    observation = await follow_public_hotel(instance._page, name)
                except Exception as error:
                    observation = {"outcome": "PUBLIC_NAVIGATION_FAILED", "error_type": type(error).__name__}
                report = {"hotel_name": name, **observation}
                async with sessions() as db, db.begin():
                    (await db.get(AppSetting, key)).value = report
                print(json.dumps({"six_senses_public_link": report}, ensure_ascii=True), flush=True)
                break  # At most one ordinary hotel-link visit, including failures.
            return result

        instance.discover_catalog = discover
        return instance

    FACTORIES["ihg"] = probed_factory
