"""
screenshots.py - drives the real frontend against the running bot (Playwright) and saves
screenshots used in the report. Requires: rasa + action server running, frontend served on :3000.
Run: python scripts/screenshots.py
"""
import asyncio
from playwright.async_api import async_playwright

URL = "http://localhost:3000/index.html?rasa=http://localhost:5005"
OUT = "docs/figures"


async def say(page, text):
    await page.fill("#msg", text); await page.keyboard.press("Enter"); await page.wait_for_timeout(1800)


async def click(page, title):
    await page.get_by_role("button", name=title).first.click(); await page.wait_for_timeout(1800)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 460, "height": 900}, device_scale_factor=2,
                                  geolocation={"latitude": 51.507, "longitude": -0.128}, permissions=["geolocation"])
        page = await ctx.new_page(); await page.goto(URL); await page.wait_for_timeout(2500)
        # Scenario 1: city break London -> Paris, high sustainability
        await click(page, "Plan a trip")
        await page.screenshot(path=f"{OUT}/ui_1_greeting_quick_replies.png")
        await click(page, "📍 Use my location")
        await click(page, "Paris")
        await say(page, "next Friday")
        await click(page, "€150–400")
        await page.screenshot(path=f"{OUT}/ui_2_intake.png")
        await click(page, "🌿 High")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{OUT}/ui_3_results_cards.png")
        await click(page, "🏨 Eco hotels")
        await page.screenshot(path=f"{OUT}/ui_4_hotels.png")
        # Error recovery and handover
        await say(page, "blorp zzt qwerty")          # stage 1: did-you-mean buttons
        await say(page, "flibber jabber")            # stage 2: constrained menu
        await page.screenshot(path=f"{OUT}/ui_5_fallback.png")
        await click(page, "Talk to a person")
        await click(page, "Yes, connect me")
        await page.screenshot(path=f"{OUT}/ui_6_handover.png")
        await b.close()

asyncio.run(main())
