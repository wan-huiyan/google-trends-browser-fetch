"""One-time helper: sign into Google Trends in a visible browser and save cookies.

Run once locally to create trends_auth.json, which the scraper then reuses to
avoid HTTP 429 rate-limits on every subsequent run.

Usage:
    python create_storage_state.py

The browser opens, you sign in to your Google account, navigate to
https://trends.google.com, then press Enter in this terminal to save the session.
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "trends_auth.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        locale="en-GB",
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()
    page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
    print("Sign in to your Google account, then navigate to https://trends.google.com")
    print("Press Enter here once you are on the Trends page and fully signed in …")
    input()
    context.storage_state(path=str(OUT))
    browser.close()

print(f"Saved session to {OUT}")
print("Set TRENDS_STORAGE_STATE=./trends_auth.json in your .env")
