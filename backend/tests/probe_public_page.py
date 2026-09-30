"""Inspect rendered public booking controls without reading private browser state."""

import argparse
import asyncio
import json

from playwright.async_api import Error, async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--forms", action="store_true")
    args = parser.parse_args()
    async with async_playwright() as playwright:
        options = {"channel": args.channel, "headless": not args.headed}
        if args.proxy:
            options["proxy"] = {"server": args.proxy}
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        try:
            response = await page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            print("http_status=", response.status if response else None, flush=True)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Error:
                pass
            print("title=", await page.title(), flush=True)
            if args.forms:
                forms = await page.locator(
                    'input[name="search.dateIn"], input[name="search.dateOut"]'
                ).evaluate_all("elements => elements.map(e=>e.parentElement.outerHTML.slice(0,6000))")
                print(json.dumps(forms, ensure_ascii=True), flush=True)
            controls = await page.locator("input, button, a").evaluate_all("""elements => elements.map(e => ({
                tag:e.tagName, text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,140),
                placeholder:e.getAttribute('placeholder'), name:e.getAttribute('name'),
                href:e.tagName==='A'?e.getAttribute('href'):undefined
            })).filter(e=>e.tag!=='A'||/book|reserv|room|rate/i.test(e.text)).slice(0,90)""")
            print(json.dumps(controls, ensure_ascii=True), flush=True)
            print(
                (await page.locator("body").inner_text())[:5000].encode("ascii", "backslashreplace").decode(),
                flush=True,
            )
        except Error as exc:
            print("browser_error=", type(exc).__name__, flush=True)
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
