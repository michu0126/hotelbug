"""Local DOM reproducer for IHG's asynchronously rendered public date picker."""

import argparse
import asyncio
from datetime import date

from playwright.async_api import async_playwright

from app.providers.ihg_browser import IHGBrowserProvider

HTML = """<button onclick="openCalendar()">Open dates</button>
<div id="hidden" style="display:none">
  <span data-year="2026" data-month="10" data-day="8">8</span>
</div>
<div role="dialog" aria-label="Choose Date" style="display:none" id="calendar">
  <button onclick="nextMonth()">Next</button><div id="days"></div>
</div>
<output id="selected"></output>
<script>
let year=2026, month=10, clicks=0;
function render() {
  document.getElementById('days').innerHTML = Array.from({length:28}, (_,i) =>
    `<button data-year="${year}" data-month="${month}" data-day="${i+1}"
      onclick="document.getElementById('selected').textContent='${year}-${month}-${i+1}'">${i+1}</button>`
  ).join('');
}
function openCalendar() {
  document.getElementById('calendar').style.display='block';
  setTimeout(render, 250);
}
function nextMonth() {
  ++clicks;
  // Keep the old DOM briefly during the transition, then mount the new grid.
  setTimeout(() => { if (++month>12) { month=1; ++year; } render(); }, 250);
}
</script>"""


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="chromium")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    async with async_playwright() as playwright:
        print("ihg_calendar_stage=browser_start", flush=True)
        browser = await playwright.chromium.launch(channel=args.channel, headless=not args.headed)
        try:
            provider = IHGBrowserProvider()
            cases = [(date(2026, 10, 8), 0), (date(2026, 11, 8), 1), (date(2027, 1, 8), 3)]
            for target, expected_clicks, hidden in [
                (target, clicks, hidden) for hidden in (False, True) for target, clicks in cases
            ]:
                print(f"ihg_calendar_case={target.isoformat()} hidden_duplicate={hidden}", flush=True)
                # set_content alone does not reset the Window's script globals.
                # Each scenario must begin with an independent October grid.
                page = await browser.new_page()
                page.set_default_timeout(15000)
                html = (
                    HTML
                    if hidden
                    else HTML.replace('<span data-year="2026" data-month="10" data-day="8">8</span>', "")
                )
                print("ihg_calendar_stage=set_content", flush=True)
                await page.set_content(html)
                print("ihg_calendar_stage=open_dates", flush=True)
                await page.get_by_role("button", name="Open dates", exact=True).click()
                print("ihg_calendar_stage=choose_day", flush=True)
                await provider._choose_day(page, target)
                assert (
                    await page.locator("#selected").inner_text()
                    == f"{target.year}-{target.month}-{target.day}"
                )
                assert await page.evaluate("clicks") == expected_clicks
                await page.close()
                print("ihg_calendar_case=PASS", flush=True)
            print("ihg_delayed_calendar_hidden_duplicate_month_transition_year_rollover=PASS")
            print("hotel_requests=0 production_profiles=0")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
