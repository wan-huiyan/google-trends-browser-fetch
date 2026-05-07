"""Browser automation for Google Trends related queries (rising and top).

Uses the **classic Explore URL** (same as Kate's ``fetch_playwright.py``):
``https://trends.google.com/trends/explore?date=...&geo=...&q=...&hl=...``
The bare ``/explore`` host can show a gradual-rollout placeholder instead of data.

Key difference from the main TIMESERIES scraper:
- No chunking or stitching — rising/top queries are a single point-in-time snapshot.
- Uses stable ``aria-label`` selectors on the download buttons where possible.
- Both tables in one browser session per term; the page is already loaded so the
  second download is fast.

Important env vars:
- ``TRENDS_STORAGE_STATE``: path to JSON from ``python create_storage_state.py`` —
  **strongly recommended locally**; anonymous sessions often get HTTP 429 immediately.
- ``TRENDS_WARMUP``: ``1`` / ``0`` — visit Google + Trends home before first Explore URL
  (default **on** when ``TRENDS_HEADLESS=0``, **off** when headless).
- ``TRENDS_PLAYWRIGHT_CHANNEL``: ``chrome`` to use installed Chrome instead of bundled Chromium.
- ``PLAYWRIGHT_SLOW_MO_MS``: slow down each action (headed debugging).
- ``TRENDS_HEADLESS``: set ``0`` for visible browser.
- ``TRENDS_GOTO_TIMEOUT_MS``, ``TRENDS_QUERY_READY_MS``, ``TRENDS_DATE_PARAM``, etc.
"""

from __future__ import annotations

import csv
import io
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _sanitize_date_param(raw: str) -> str:
    """Strip accidental ``#`` comments / quotes from env misconfiguration."""
    s = (raw or "").strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def build_explore_url(term: str, *, geo: str, hl: str, date_param: str) -> str:
    """Classic Explore URL — matches ``explore_url_range`` in ``fetch_playwright.py``."""
    dp = _sanitize_date_param(date_param)
    return (
        "https://trends.google.com/trends/explore"
        f"?date={quote(dp)}"
        f"&geo={quote(geo)}"
        f"&q={quote(term.strip())}"
        f"&hl={quote(hl)}"
    )


# ---------------------------------------------------------------------------
# Browser helpers (consent, block detection)
# ---------------------------------------------------------------------------

def _headless() -> bool:
    return os.environ.get("TRENDS_HEADLESS", "1").strip().lower() not in ("0", "false", "no")


def dismiss_consent(page) -> bool:
    selectors = (
        'button:has-text("Accept all")',
        'button:has-text("Accept all cookies")',
        'button:has-text("I agree")',
        'button:has-text("Got it")',
        'a.cookieBarConsentButton',
        'text=OK, got it',
        'button:has-text("Agree")',
        'button[aria-label*="Accept" i]',
        '[aria-label="Accept all"]',
        'form[action*="consent"] button[type="submit"]',
        'div[role="dialog"] button:has-text("Accept")',
        "#L2AGLb",
    )
    clicked = False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=5000)
                page.wait_for_timeout(1000)
                clicked = True
        except Exception:
            continue
    return clicked


def dismiss_consent_loop(page, rounds: int = 4) -> None:
    for _ in range(rounds):
        if not dismiss_consent(page):
            break
        page.wait_for_timeout(400)


def _detect_google_rate_block(page) -> bool:
    """True if the page is Google's 429 / over-quota HTML (not the in-app widget)."""
    try:
        text = page.inner_text("body", timeout=8000).lower()
    except Exception:
        return False
    markers = (
        "too many requests",
        "429",
        "rate limit",
        "quota exceeded",
        "sent too many requests",
        "that's an error",
        "try again later",
    )
    return any(m in text for m in markers)


def _429_backoff_sec(attempt_index: int) -> float:
    """Long exponential backoff after HTTP 429 (same idea as ``fetch_playwright``)."""
    raw = os.environ.get("TRENDS_429_RETRY_BASE_SEC", "75").strip()
    try:
        base = float(raw)
    except ValueError:
        base = 75.0
    try:
        cap = float(os.environ.get("TRENDS_FETCH_RETRY_MAX_SEC", "600").strip() or "600")
    except ValueError:
        cap = 600.0
    exp = min(base * (2**attempt_index), cap)
    jitter = random.uniform(0, min(45.0, exp * 0.2))
    return exp + jitter


def _generic_retry_backoff_sec(attempt_index: int) -> float:
    """Shorter backoff for timeouts / missing buttons (not 429)."""
    exp = min(20.0 * (2**attempt_index), 120.0) + random.uniform(0, 5)
    return exp


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _query_ready_ms() -> int:
    raw = os.environ.get("TRENDS_QUERY_READY_MS", "90000").strip()
    return int(raw) if raw.isdigit() else 90_000


def _goto_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_GOTO_TIMEOUT_MS", "120000").strip()
    return int(raw) if raw.isdigit() else 120_000


def _max_attempts() -> int:
    raw = os.environ.get("TRENDS_FETCH_MAX_ATTEMPTS", "5").strip()
    try:
        return max(1, min(int(raw), 15))
    except ValueError:
        return 5


def _intra_download_pause_ms() -> int:
    raw = os.environ.get("TRENDS_INTRA_TERM_DOWNLOAD_PAUSE_MS", "4000").strip()
    return int(raw) if raw.isdigit() else 4000


def _session_start_delay_sec() -> float:
    raw = os.environ.get("TRENDS_SESSION_START_DELAY_SEC", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _storage_state_path() -> Path | None:
    raw = os.environ.get("TRENDS_STORAGE_STATE", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if p.is_file():
        return p
    print(f"WARN: TRENDS_STORAGE_STATE file missing: {p}", flush=True)
    return None


def _warmup_enabled() -> bool:
    raw = os.environ.get("TRENDS_WARMUP", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return not _headless()


def _warmup_browser(page) -> None:
    """Visit Google + Trends landing before Explore (reduces instant 429 for cold bots)."""
    if not _warmup_enabled():
        return
    print("Warm-up: google.com → trends hub (human-like pacing) …", flush=True)
    try:
        page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=90_000)
        time.sleep(random.uniform(2.5, 5.5))
        page.goto(
            "https://trends.google.com/trends/",
            wait_until="domcontentloaded",
            timeout=120_000,
            referer="https://www.google.com/",
        )
        time.sleep(random.uniform(4.0, 9.0))
    except Exception as exc:
        print(f"  Warm-up incomplete (continuing anyway): {exc}", flush=True)


def _launch_browser_kwargs() -> dict:
    kw: dict = {
        "headless": _headless(),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    ch = os.environ.get("TRENDS_PLAYWRIGHT_CHANNEL", "").strip().lower()
    if ch in ("chrome", "msedge", "chromium"):
        kw["channel"] = ch
    sm = os.environ.get("PLAYWRIGHT_SLOW_MO_MS", "").strip()
    if sm.isdigit() and int(sm) > 0:
        kw["slow_mo"] = int(sm)
    return kw


def _browser_context_kwargs() -> dict:
    """Extra HTTP headers on every request (Referer helps some Google endpoints)."""
    opts: dict = {
        "locale": "en-GB",
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "accept_downloads": True,
        "extra_http_headers": {
            "Accept-Language": "en-GB,en;q=0.9",
        },
    }
    ss = _storage_state_path()
    if ss:
        opts["storage_state"] = str(ss)
        print(f"Using signed-in cookies from {ss}", flush=True)
    else:
        print(
            "No TRENDS_STORAGE_STATE file — anonymous session (429 common). "
            "Run: python create_storage_state.py",
            flush=True,
        )
    return opts


def _networkidle_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_NETWORKIDLE_TIMEOUT_MS", "55000").strip()
    return int(raw) if raw.isdigit() else 55_000


def _scroll_explore_like_human(page) -> None:
    """Lazy Angular mounts widgets off-screen; scroll helps exports appear."""
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
        page.wait_for_timeout(700)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.55)")
        page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
    except Exception:
        pass


def _related_queries_widget(page):
    """The **Related queries** card (not geo-map side bullets or Related topics).

    Explore renders several ``fe_related_queries`` widgets; only the one anchored at
    ``#RELATED_QUERIES`` has Rising/Top CSV export. Others may show Top only with
    Rising disabled and no download button — matching them caused infinite waits.
    """
    return page.locator("div.widget-container:has(#RELATED_QUERIES) trends-widget").first


def _click_related_panel_tab(page, label: str, *, widget) -> bool:
    """Switch Rising vs Top inside the Related queries widget.

    Classic Explore uses an Angular Material ``md-select`` (bullets-view-selector), not
    ``role="tab``. Options stay in the widget DOM with ``value="risingBullets"`` /
    ``value="bullets"`` even when the menu is closed (overlay may portal when open).
    """
    lv = label.strip().lower()
    value_attr = "risingBullets" if lv == "rising" else "bullets" if lv == "top" else None

    # Prefer stable ``value`` attrs on ``md-option`` inside this widget only.
    if value_attr:
        try:
            opt = widget.locator(f'md-option[value="{value_attr}"]').first
            if opt.count() > 0:
                opt.scroll_into_view_if_needed(timeout=10_000)
                try:
                    if opt.get_attribute("disabled") is not None:
                        print(f"  md-option {label!r} is disabled (no data)", flush=True)
                        return False
                    if (opt.get_attribute("aria-disabled") or "").lower() == "true":
                        print(f"  md-option {label!r} is disabled (no data)", flush=True)
                        return False
                except Exception:
                    pass
                opt.click(timeout=8000, force=True)
                page.wait_for_timeout(900)
                print(f"  Selected Related-queries view: {label!r} (md-option)", flush=True)
                return True
        except Exception:
            pass

    # Fallback: open dropdown then click visible option text.
    try:
        dd = widget.locator("md-select.bullets-view-selector").first
        if dd.count() > 0:
            dd.scroll_into_view_if_needed(timeout=10_000)
            if dd.is_visible(timeout=5000):
                dd.click(timeout=8000)
                page.wait_for_timeout(450)
                opt = widget.locator("md-option").filter(has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
                if opt.count() == 0:
                    opt = widget.locator("md-option").filter(has_text=label)
                first = opt.first
                if first.count() > 0:
                    try:
                        if first.get_attribute("disabled") is not None:
                            print(f"  md-option {label!r} is disabled (no data)", flush=True)
                            return False
                        if (first.get_attribute("aria-disabled") or "").lower() == "true":
                            print(f"  md-option {label!r} is disabled (no data)", flush=True)
                            return False
                    except Exception:
                        pass
                    first.click(timeout=8000)
                    page.wait_for_timeout(900)
                    print(f"  Selected Related-queries view: {label!r} (md-select)", flush=True)
                    return True
    except Exception:
        pass

    patterns = (
        f'[role="tab"]:has-text("{label}")',
        f'button[role="tab"]:has-text("{label}")',
        f'div[role="tab"]:has-text("{label}")',
    )
    for sel in patterns:
        try:
            loc = widget.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.click(timeout=8000)
                page.wait_for_timeout(1200)
                print(f"  Activated tab: {label!r}", flush=True)
                return True
        except Exception:
            continue
    return False


# Selectors tried **inside** the RELATED_QUERIES widget locator only.
# The export button downloads a combined CSV containing both RISING and TOP sections.
_EXPORT_SELECTORS: tuple[str, ...] = (
    'button[title="CSV"].widget-actions-item.export',
    'button.widget-actions-item.export[title="CSV"]',
    'button.export[title="CSV"]',
    'button[title="CSV"]',
    'button[aria-label*="CSV" i]',
)


def _poll_until_export_ready(
    root,
    *,
    timeout_ms: int,
) -> "object | None":
    """Return the enabled CSV export button inside ``root`` (the RELATED_QUERIES widget), or None."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_log = 0.0
    while time.monotonic() < deadline:
        for sel in _EXPORT_SELECTORS:
            try:
                loc = root.locator(sel).first
                if loc.count() == 0:
                    continue
                if not loc.is_visible(timeout=1200):
                    continue
                if loc.get_attribute("disabled") is not None:
                    continue
                print(f"  Export button ready: matched {sel[:85]}", flush=True)
                return loc
            except Exception:
                continue
        root.page.wait_for_timeout(450)
        now = time.monotonic()
        if now - last_log > 15:
            remain = int(deadline - now)
            print(f"  …still waiting for CSV export button (~{remain}s left)", flush=True)
            last_log = now
    return None


def _download_via_locator(locator, out_path: Path, timeout_ms: int, desc: str) -> bool:
    try:
        locator.scroll_into_view_if_needed(timeout=15_000)
        with locator.page.expect_download(timeout=timeout_ms) as dl_info:
            locator.click(timeout=20_000)
        dl_info.value.save_as(str(out_path))
        size = out_path.stat().st_size if out_path.is_file() else 0
        print(f"  Downloaded {desc} → {out_path} ({size} bytes)", flush=True)
        return size > 20
    except Exception as exc:
        print(f"  Download failed ({desc}): {exc}", flush=True)
        out_path.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Per-term fetch
# ---------------------------------------------------------------------------

def _ensure_playwright_browsers() -> None:
    if os.environ.get("PLAYWRIGHT_SKIP_RUNTIME_INSTALL", "").lower() in ("1", "true", "yes"):
        return
    target = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if not target and os.environ.get("K_SERVICE"):
        target = "/tmp/ms-playwright"
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = target
    if not target:
        return
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    try:
        from importlib.metadata import version as pkg_version
        tag = pkg_version("playwright")
    except Exception:
        tag = "unknown"
    marker = root / f".chromium_ok_{tag}"
    if marker.is_file():
        return
    print(f"Installing Playwright Chromium → {root} …", flush=True)
    r = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=os.environ.copy(),
        timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError("playwright install chromium failed")
    marker.write_text("", encoding="utf-8")


def fetch_queries_for_term(
    page,
    term: str,
    *,
    geo: str,
    hl: str,
    date_param: str,
    out_dir: Path,
    run_id: str,
) -> Path | None:
    """Navigate to Trends Explore for ``term`` and download the combined queries CSV.

    Google exports a single CSV containing both RISING and TOP sections with the format::

        Category: All categories
        "term: (date range, country)"

        RISING
        query1,Breakout
        query2,+550%

        TOP
        query1,100
        query2,50

    Returns the saved ``Path``, or ``None`` if all attempts fail.
    Retries up to ``TRENDS_FETCH_MAX_ATTEMPTS`` times on rate-limit or navigation error.
    """
    url = build_explore_url(term, geo=geo, hl=hl, date_param=date_param)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", term.strip().lower()).strip("_")
    combined_path = out_dir / f"queries_{safe}.csv"

    ready_ms = _query_ready_ms()
    goto_ms = _goto_timeout_ms()
    attempts = _max_attempts()
    pending_429 = False

    for attempt in range(attempts):
        if attempt:
            backoff = (
                _429_backoff_sec(attempt - 1)
                if pending_429
                else _generic_retry_backoff_sec(attempt - 1)
            )
            pending_429 = False
            print(f"  Waiting {backoff:.0f}s before retry …", flush=True)
            time.sleep(backoff)
            try:
                page.goto("about:blank", wait_until="commit", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

        print(f"  Navigating: {url}", flush=True)
        try:
            resp = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=goto_ms,
                referer="https://trends.google.com/trends/",
            )
        except PlaywrightTimeout:
            print(f"  Navigation timeout (attempt {attempt + 1})", flush=True)
            continue
        except Exception as exc:
            print(f"  Navigation error: {exc}", flush=True)
            continue

        if resp is not None and resp.status == 429:
            print(f"  HTTP 429 on navigation (attempt {attempt + 1})", flush=True)
            pending_429 = True
            continue

        page.wait_for_timeout(3000)
        dismiss_consent_loop(page)
        page.wait_for_timeout(2000)

        if _detect_google_rate_block(page):
            print(f"  Google rate-limit / error page (attempt {attempt + 1})", flush=True)
            pending_429 = True
            continue

        # Let Angular finish (saved HTML is only a shell until JS fills widgets).
        ni_ms = _networkidle_timeout_ms()
        print(f"  Waiting for network idle (up to {ni_ms // 1000}s) …", flush=True)
        try:
            page.wait_for_load_state("networkidle", timeout=ni_ms)
        except PlaywrightTimeout:
            print("  networkidle timed out — continuing anyway", flush=True)

        _scroll_explore_like_human(page)
        dismiss_consent_loop(page, rounds=2)

        rq_widget = _related_queries_widget(page)
        try:
            rq_widget.wait_for(state="visible", timeout=min(ready_ms, 60_000))
        except Exception:
            print(f"  RELATED_QUERIES widget not visible (attempt {attempt + 1})", flush=True)
            continue

        print(f"  Waiting for CSV export button (up to {ready_ms // 1000}s) …", flush=True)
        export_loc = _poll_until_export_ready(rq_widget, timeout_ms=ready_ms)
        if export_loc is None:
            print(f"  No CSV export button found (attempt {attempt + 1})", flush=True)
            continue

        page.wait_for_timeout(800)
        ok = _download_via_locator(export_loc, combined_path, ready_ms, "queries")
        if ok:
            return combined_path

    print(f"  All attempts exhausted for {term!r}", flush=True)
    return None


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

# Section header labels as they appear in the Google Trends export (case-insensitive).
_SECTION_LABELS: dict[str, str] = {"rising": "rising", "top": "top"}


def _split_csv_row(line: str) -> list[str]:
    """Split a single CSV line, auto-detecting comma/semicolon/tab delimiter."""
    if "\t" in line and line.count("\t") >= max(1, line.count(",")):
        delim = "\t"
    elif line.count(";") > line.count(","):
        delim = ";"
    else:
        delim = ","
    try:
        return next(csv.reader(io.StringIO(line), delimiter=delim))
    except Exception:
        return []


def parse_combined_queries_csv(
    path: Path,
    *,
    term: str,
    run_date: date,
    run_id: str,
) -> list[dict]:
    """Parse the combined Rising+Top CSV that Google Trends exports in one download.

    Real export format (no column-header row — section labels act as delimiters)::

        Category: All categories
        "brake discs: (2/7/26 - 5/7/26, United Kingdom)"

        RISING                  ← optional; absent when no rising data
        auto doc,Breakout
        autodoc uk,Breakout

        TOP
        brake pads,100
        front brake discs,33

    Each non-preamble, non-section-label line is treated as ``query,value``.
    """
    if not path.is_file() or path.stat().st_size < 5:
        return []

    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  Could not read {path}: {exc}", flush=True)
        return []

    if "<html" in raw[:400].lower() or "<!doctype" in raw[:400].lower():
        print(f"  {path.name} looks like HTML — skipping", flush=True)
        return []

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    current_section: str | None = None
    seen_any_section = False

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Strip surrounding quotes that Google sometimes wraps around the title line.
        unquoted = line.strip('"').strip("'")
        norm = unquoted.lower()

        # Section headers — bare "RISING" or "TOP" on their own line.
        if norm in _SECTION_LABELS:
            current_section = _SECTION_LABELS[norm]
            seen_any_section = True
            continue

        # Preamble lines (before first section header).
        if not seen_any_section:
            continue

        parts = _split_csv_row(line)
        if not parts:
            continue

        related_query = parts[0].strip().strip('"').strip("'")
        if not related_query:
            continue

        value_raw = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""

        is_breakout = value_raw.lower() == "breakout"
        value_numeric: float | None = None
        if not is_breakout and value_raw:
            clean = re.sub(r"[+%\s]", "", value_raw)
            try:
                value_numeric = float(clean)
            except ValueError:
                pass

        rows.append(
            {
                "run_date": run_date.isoformat(),
                "term": term,
                "related_query": related_query,
                "query_type": current_section,
                "value_raw": value_raw,
                "value_numeric": value_numeric,
                "value_is_breakout": is_breakout,
                "run_id": run_id,
                "ingested_at": now,
            }
        )

    if not rows:
        preview = raw[:400].replace("\n", "\\n")
        print(f"  No rows parsed from {path.name}; preview: {preview!r}", flush=True)

    return rows


# ---------------------------------------------------------------------------
# High-level runner used by main.py
# ---------------------------------------------------------------------------

def run_all_terms(
    terms: list[str],
    *,
    geo: str,
    hl: str,
    date_param: str,
    data_dir: Path,
    run_id: str,
    inter_term_pause_sec: float = 45.0,
) -> list[dict]:
    """Fetch rising + top related queries for all ``terms``.

    Returns a flat list of row dicts suitable for BigQuery insertion.
    Reuses a single browser session across all terms.
    """
    _ensure_playwright_browsers()
    all_rows: list[dict] = []
    run_date = datetime.now(timezone.utc).date()

    start_delay = _session_start_delay_sec()
    if start_delay > 0:
        print(
            f"Session start delay {start_delay:.0f}s (set TRENDS_SESSION_START_DELAY_SEC=0 to skip) …",
            flush=True,
        )
        time.sleep(start_delay)

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_browser_kwargs())
        context = browser.new_context(**_browser_context_kwargs())
        page = context.new_page()
        _warmup_browser(page)

        for i, term in enumerate(terms):
            if i and inter_term_pause_sec > 0:
                print(f"Pausing {inter_term_pause_sec:.0f}s between terms …", flush=True)
                time.sleep(inter_term_pause_sec)

            print(f"\n=== Term {i + 1}/{len(terms)}: {term!r} ===", flush=True)

            combined_path = fetch_queries_for_term(
                page,
                term,
                geo=geo,
                hl=hl,
                date_param=date_param,
                out_dir=data_dir / "related_queries_downloads",
                run_id=run_id,
            )

            if combined_path and combined_path.is_file():
                rows = parse_combined_queries_csv(
                    combined_path,
                    term=term,
                    run_date=run_date,
                    run_id=run_id,
                )
                rising_n = sum(1 for r in rows if r["query_type"] == "rising")
                top_n = sum(1 for r in rows if r["query_type"] == "top")
                print(f"  Parsed {rising_n} rising + {top_n} top rows for {term!r}", flush=True)
                all_rows.extend(rows)
                combined_path.unlink(missing_ok=True)

        context.close()
        browser.close()

    return all_rows
