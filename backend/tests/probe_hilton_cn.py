"""Exercise the public Hilton China search UI and print rendered, visible results."""

import argparse
import asyncio
import json
import re
from datetime import date

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--query", default="上海")
    parser.add_argument("--country")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--select-result", action="store_true")
    parser.add_argument("--open-calendar", action="store_true")
    parser.add_argument("--check-in", default="2026-10-20")
    parser.add_argument("--check-out", default="2026-10-21")
    parser.add_argument("--run-search", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--inspect-name")
    parser.add_argument("--book-name")
    args = parser.parse_args()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=not args.headed,
            proxy={"server": args.proxy} if args.proxy else None,
        )
        page = await browser.new_page(locale="zh-CN", timezone_id="Asia/Shanghai")
        try:
            response = await page.goto(
                "https://www.hilton.com.cn/zh-CN/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            print("status=", response.status if response else None, flush=True)
            search = page.locator('input[placeholder*="城市"]:visible').first
            await search.wait_for(timeout=20000)
            await search.click()
            await search.fill(args.query)
            await page.wait_for_timeout(4000)
            if args.debug:
                print(
                    "suggestions=",
                    json.dumps(
                        await page.locator(".search-result-item:visible").evaluate_all(
                            "els=>els.map(e=>({name:e.querySelector('.result-name')?.innerText.trim(),description:e.querySelector('.result-desc')?.innerText.trim()}))"
                        ),
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                print(
                    "visible_options=",
                    await page.locator('[role="option"]:visible').all_text_contents(),
                    flush=True,
                )
                print(
                    "visible_lists=",
                    await page.locator("li:visible").evaluate_all(
                        "els=>els.map(e=>e.innerText.trim()).filter(Boolean).slice(0,80)"
                    ),
                    flush=True,
                )
                print(
                    "exact_query_nodes=",
                    await page.get_by_text(args.query, exact=True).evaluate_all(
                        "els=>els.filter(e=>e.offsetParent).map(e=>({tag:e.tagName,class:e.className,html:(e.parentElement?.parentElement||e).outerHTML.slice(0,2400)})).slice(0,20)"
                    ),
                    flush=True,
                )
            if args.select_result:
                suggestions = page.locator(".search-result-item:visible")
                selected = False
                for index in range(await suggestions.count()):
                    suggestion = suggestions.nth(index)
                    name = (await suggestion.locator(".result-name").inner_text()).strip()
                    description = (
                        (await suggestion.locator(".result-desc").inner_text()).strip()
                        if await suggestion.locator(".result-desc").count()
                        else ""
                    )
                    if name != args.query:
                        continue
                    if args.country and not description.startswith(f"{args.country}/"):
                        continue
                    await suggestion.click()
                    selected = True
                    break
                if not selected:
                    raise RuntimeError(
                        f"No exact Hilton China suggestion for {args.country or ''}/{args.query}"
                    )
                await page.wait_for_timeout(1000)
                print("selected_value=", await search.input_value(), flush=True)
                if args.debug:
                    print(
                        "date_nodes=",
                        await page.locator("body *").evaluate_all(
                            "els=>els.filter(e=>e.offsetParent && /1\\u665a/.test(e.innerText) && e.innerText.length<120).map(e=>({tag:e.tagName,class:e.className,text:e.innerText,html:e.outerHTML.slice(0,1000)})).slice(-4)"
                        ),
                        flush=True,
                    )
                if args.open_calendar:
                    await page.locator(".date-field:visible").first.click()
                    await page.wait_for_timeout(1000)
                    if args.debug:
                        print(
                            "calendar_nodes=",
                            await page.locator(
                                '[class*="calendar"]:visible, [class*="date"]:visible'
                            ).evaluate_all(
                                "els=>els.map(e=>({tag:e.tagName,class:e.className,text:e.innerText.slice(0,500),html:e.outerHTML.slice(0,1400)})).slice(-5)"
                            ),
                            flush=True,
                        )
                        print(
                            "calendar_buttons=",
                            await page.locator("button:visible").evaluate_all(
                                "els=>els.map(e=>({text:e.innerText,class:e.className,aria:e.getAttribute('aria-label'),html:e.outerHTML.slice(0,900)})).filter(e=>e.text||e.aria).slice(-30)"
                            ),
                            flush=True,
                        )
                    if args.run_search:

                        async def choose_day(value: date) -> None:
                            label = f"{value.year}年{value.month}月"
                            for _ in range(13):
                                month = page.locator(".calendar-month:visible").filter(
                                    has=page.get_by_text(label, exact=True)
                                )
                                if await month.count():
                                    day = month.locator(
                                        ".calendar-day:not(.disabled):not(.other-month)"
                                    ).filter(has_text=re.compile(rf"^{value.day}$"))
                                    await day.first.click()
                                    return
                                await page.locator(".calendar-header .right-arrow:visible").last.click()
                            raise RuntimeError(f"Hilton China calendar did not expose {value}")

                        await choose_day(date.fromisoformat(args.check_in))
                        await choose_day(date.fromisoformat(args.check_out))
                        await page.locator(".search-button:visible").first.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(5000)
                        print("results_url=", page.url, flush=True)
                        print("results_title=", await page.title(), flush=True)
                        if args.inspect_name:
                            heading = page.get_by_text(args.inspect_name, exact=True)
                            print(
                                "hotel_card_info=",
                                json.dumps(
                                    await heading.evaluate(
                                        "e=>{const c=e.closest('.hotel-card'); return {text:c?.innerText,images:[...c.querySelectorAll('img')].map(x=>x.src).slice(0,2),links:[...c.querySelectorAll('a')].map(x=>({text:x.innerText,href:x.href})),buttons:[...c.querySelectorAll('button')].map(x=>({text:x.innerText,class:x.className})),priced:[...c.querySelectorAll('[class*=price]')].map(x=>({text:x.innerText,class:x.className,html:x.outerHTML.slice(0,1200)}))};}"
                                    ),
                                    ensure_ascii=True,
                                ),
                                flush=True,
                            )
                            print(
                                "hotel_ancestors=",
                                json.dumps(
                                    await heading.evaluate(
                                        "e=>{const r=[];for(let p=e;p&&r.length<7;p=p.parentElement)r.push({tag:p.tagName,class:p.className,html:p.outerHTML.slice(0,6000)});return r}"
                                    ),
                                    ensure_ascii=True,
                                ),
                                flush=True,
                            )
                        if args.book_name:
                            card = page.locator(".hotel-card:visible").filter(
                                has=page.get_by_text(args.book_name, exact=True)
                            )
                            booking = card.get_by_role("link", name="立即预订", exact=True)
                            print("booking_href=", await booking.get_attribute("href"), flush=True)
                            async with page.expect_popup() as popup_info:
                                await booking.click()
                            booking_page = await popup_info.value
                            await booking_page.wait_for_load_state("domcontentloaded", timeout=45000)
                            await booking_page.wait_for_timeout(8000)
                            print("booking_url=", booking_page.url, flush=True)
                            print("booking_title=", ascii(await booking_page.title()), flush=True)
                            print(
                                "booking_body=",
                                ascii((await booking_page.locator("body").inner_text())[:16000]),
                                flush=True,
                            )
                        print(
                            "results_body=",
                            (await page.locator("body").inner_text())[:16000]
                            .encode("ascii", "backslashreplace")
                            .decode(),
                            flush=True,
                        )
            if args.debug or not args.run_search:
                print(
                    "body=",
                    (await page.locator("body").inner_text())[:10000]
                    .encode("ascii", "backslashreplace")
                    .decode(),
                    flush=True,
                )
        except BrowserTimeout:
            print("timeout_url=", page.url, flush=True)
            print(
                (await page.locator("body").inner_text())[:5000].encode("ascii", "backslashreplace").decode(),
                flush=True,
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
