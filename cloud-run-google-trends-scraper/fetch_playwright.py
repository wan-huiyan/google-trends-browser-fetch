#!/usr/bin/env python3
r"""Download Google Trends multiTimeline CSVs via Playwright (best-effort).

May fail on captchas, consent walls, or headless detection. Requires:
  pip install playwright && playwright install chromium

Optional env:

- ``TRENDS_PLAYWRIGHT_TIMEOUT_MS``: max wait for CSV export controls (default ``120000``).
- ``TRENDS_WEEKLY_EXPORT_TIMEOUT_MS``: first job (weekly reference) timeout (default ``180000``).
- ``TRENDS_GOTO_TIMEOUT_MS``: max wait for ``page.goto`` (default ``180000``).
- ``TRENDS_GOTO_WAIT_UNTIL``: first-attempt ``wait_until`` for goto: ``commit`` | ``domcontentloaded``
  | ``load`` | ``networkidle`` (default ``domcontentloaded``). Retries use ``load`` then
  ``domcontentloaded`` with extra time.
- ``TRENDS_NETWORKIDLE_TIMEOUT_MS``: cap for optional ``networkidle`` wait after load (default ``60000``).
- ``TRENDS_HEADLESS``: set ``0`` / ``false`` to show the browser (debug consent/captcha/export).
- ``TRENDS_DEBUG_EXPORT``: set ``1`` to print the start of a rejected download (why validation failed).
- ``TRENDS_TIMESERIES_READY_MS`` (default ``55000``): max time to wait for the Interest-over-time
  widget and its export/CSV controls after navigation (avoids clicking unrelated ``button.export``
  targets that yield region breakdown or empty CSV stubs).
- ``TRENDS_FETCH_MAX_ATTEMPTS`` (default ``5``): retries per URL when navigation/export fails or CSV is invalid.
- ``TRENDS_FETCH_RETRY_BASE_SEC`` / ``TRENDS_RATE_LIMIT_RETRY_BASE_SEC``: exponential backoff bases (defaults
  ~18s / ~75s) before each retry; caps at ``TRENDS_FETCH_RETRY_MAX_SEC`` (default ``600``).
  When Playwright gets **HTTP 429**, a rate-limit stub download, or page text suggests over-quota, the longer
  rate-limit base is used on the **next** retry.
- ``PLAYWRIGHT_BROWSERS_PATH``: where browser binaries live (see below).
- ``PLAYWRIGHT_SKIP_RUNTIME_INSTALL``: set to ``1`` when browsers are baked into the image.
**Cloud Run buildpacks** only run ``pip install``; the run user often cannot use a root-only
cache. If ``K_SERVICE`` is set and ``PLAYWRIGHT_BROWSERS_PATH`` is unset, this script
downloads Chromium once per instance under ``/tmp/ms-playwright`` (cold start is slow).
Prefer the **Dockerfile** path with ``PLAYWRIGHT_SKIP_RUNTIME_INSTALL=1``.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("TRENDS_DATA_DIR", str(ROOT))).resolve()
CHUNKS_JSON = DATA_ROOT / "chunks.json"


def _ensure_playwright_browsers() -> None:
    """Ensure Chromium exists (buildpack / non-root: install into a writable directory)."""
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
    print(
        f"Installing Playwright Chromium → {root} (first start or new version; may take 1–3 min) …",
        flush=True,
    )
    r = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=os.environ.copy(),
        timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "playwright install chromium failed. Use the deploy Dockerfile "
            "(playwright install --with-deps chromium) or fix permissions on "
            "PLAYWRIGHT_BROWSERS_PATH."
        )
    marker.write_text("", encoding="utf-8")


def _chunk_csv_path(stored_filename: str) -> Path:
    """Write all chunk CSVs under ``DATA_ROOT/chunks/`` (portable for Docker / Cloud Run)."""
    return DATA_ROOT / "chunks" / Path(stored_filename).name


def active_query_term() -> str:
    """Single Trends ``q`` value: ``QUERY_TERM`` set by ``main.py`` per term (never comma-joined)."""
    qt = os.environ.get("QUERY_TERM", "").strip()
    if qt:
        return qt
    raise RuntimeError(
        "QUERY_TERM must be set (single term). Multi-term lists are handled by main.py sequentially."
    )


def explore_url_range(
    date_start: str, date_end: str, *, geo: str, hl: str, q_term: str
) -> str:
    """Google Trends explore URL with exactly one ``q`` query string."""
    date_param = f"{date_start.strip()} {date_end.strip()}"
    return (
        "https://trends.google.com/trends/explore"
        f"?date={quote(date_param)}&geo={quote(geo)}&q={quote(q_term)}&hl={quote(hl)}"
    )


def merge_multitimeline_csvs(paths: list[Path], out: Path, *, date_col_name: str) -> None:
    """Merge several multiTimeline exports (disjoint term columns) into one CSV."""
    from stitch_daily import load_trends_csv

    paths = [p for p in paths if p.is_file() and p.stat().st_size > 0]
    if not paths:
        raise ValueError("merge_multitimeline_csvs: no non-empty input files")
    if len(paths) == 1:
        shutil.copyfile(paths[0], out)
        return
    dfs = [load_trends_csv(str(p)) for p in paths]
    merged = dfs[0]
    for d in dfs[1:]:
        merged = merged.join(d, how="outer")
    merged = merged.sort_index()
    _write_multitimeline_df(merged, out, date_col_name=date_col_name)


def _write_multitimeline_df(df: pd.DataFrame, out: Path, *, date_col_name: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df = df.sort_index().reset_index()
    first_col = out_df.columns[0]
    out_df = out_df.rename(columns={first_col: date_col_name})
    with out.open("w", encoding="utf-8", newline="") as f:
        f.write("Category: All categories\n\n")
        out_df.to_csv(f, index=False)


def weekly_url() -> str:
    """Sample weekly explore URL for the active single ``QUERY_TERM``."""
    start = os.environ.get("TRENDS_WEEKLY_START", "2024-09-01").strip()
    end = os.environ.get("TRENDS_WEEKLY_END", "2026-03-31").strip()
    geo = os.environ.get("TRENDS_GEO", "GB").strip()
    hl = os.environ.get("TRENDS_HL", "en-GB").strip()
    return explore_url_range(start, end, geo=geo, hl=hl, q_term=active_query_term())


def dismiss_consent(page) -> bool:
    """Dismiss common Google / Trends consent overlays. Returns True if something was clicked."""
    selectors = (
        'button:has-text("Accept all")',
        'button:has-text("Accept all cookies")',
        'button:has-text("I agree")',
        'button:has-text("Got it")',
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
                page.wait_for_timeout(1200)
                clicked = True
        except Exception:
            continue
    return clicked


def dismiss_consent_loop(page, rounds: int = 4) -> None:
    for _ in range(rounds):
        if not dismiss_consent(page):
            break
        page.wait_for_timeout(500)


def _detect_hard_block(page) -> str | None:
    """Return a short reason if the page looks like captcha / block, else None."""
    try:
        text = page.inner_text("body", timeout=10_000).lower()
    except Exception:
        return None
    markers = (
        "unusual traffic",
        "sorry, something went wrong",
        "captcha",
        "recaptcha",
        "verify you",
        "not a robot",
        "before you continue",
        "too many requests",
        "429",
        "rate limit",
        "quota exceeded",
    )
    for m in markers:
        if m in text:
            return m
    return None


def _headless_from_env() -> bool:
    raw = os.environ.get("TRENDS_HEADLESS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _debug_export_enabled() -> bool:
    return os.environ.get("TRENDS_DEBUG_EXPORT", "").strip().lower() in ("1", "true", "yes", "on")


def _log_invalid_download(path: Path) -> None:
    if not _debug_export_enabled():
        return
    try:
        snippet = path.read_text(encoding="utf-8-sig", errors="replace")[:800]
        print(f"  DEBUG rejected file preview ({path.stat().st_size} bytes):\n{snippet!r}", flush=True)
    except Exception as ex:
        print(f"  DEBUG could not read rejected file: {ex}", flush=True)


def _iot_widget_roots(page):
    """Locates Interest-over-time chart widgets (several DOM shapes across Explore builds).

    Explore still renders **other** widgets (maps, related queries, etc.) with their own
    export controls — independent of how many terms are in ``q`` — so we scope clicks to
    this widget rather than every ``button.export`` on the page.
    """
    # Union: legacy inner node, Angular ``explore-widget`` host, data-attribute host.
    return page.locator(
        '[widget-type="TIMESERIES"], '
        'explore-widget[widget-type="TIMESERIES"], '
        '[data-explore-widget-name="TIMESERIES"]'
    )


def _timeseries_ready_budget_ms(export_timeout_ms: int) -> int:
    raw = os.environ.get("TRENDS_TIMESERIES_READY_MS", "").strip()
    if raw.isdigit():
        return min(int(raw), max(120_000, export_timeout_ms))
    return min(55_000, max(25_000, export_timeout_ms // 2))


def _wait_timeseries_export_ready(page, roots, budget_ms: int) -> None:
    """Poll until a TIMESERIES host is present and shows an export/CSV control, or budget elapses."""
    t0 = time.monotonic()
    while (time.monotonic() - t0) * 1000 < budget_ms:
        try:
            n = roots.count()
            if n > 0:
                inner = roots.first.locator(
                    'button.export, '
                    'button[aria-label*="CSV" i], '
                    '[role="button"][aria-label*="CSV" i]'
                )
                if inner.count() > 0:
                    return
        except Exception:
            pass
        page.wait_for_timeout(450)


def _menu_csv_on_timeseries_hosts(
    page,
    out_path: Path,
    timeout_ms: int,
    *,
    max_hosts_per_selector: int = 4,
) -> bool:
    """Open the export flyout on known IoT hosts and choose CSV (works when direct icon is wrong)."""
    for sel in (
        "explore-widget[widget-type=\"TIMESERIES\"]",
        "[data-explore-widget-name=\"TIMESERIES\"]",
        "[widget-type=\"TIMESERIES\"]",
    ):
        host = page.locator(sel)
        try:
            n = host.count()
        except Exception:
            continue
        for i in range(min(n, max_hosts_per_selector)):
            if _try_export_menu_then_csv(page, host.nth(i), out_path, timeout_ms):
                return True
    return False


def _export_buttons_in_widget(widget) -> list[tuple[object, str]]:
    """Ordered targets inside one IoT widget: **explicit CSV / download** before generic ``.export``.

    A single-term query still uses this widget; the wrong click is usually a generic
    ``button.export`` that opens a image/pdf menu or targets a non-CSV action. Order matters.
    """
    pairs: list[tuple[object, str]] = []
    # Highest priority: labels that name CSV (avoid first match on a bare .export icon).
    priority: tuple[tuple[str, str], ...] = (
        ('button[aria-label*="Download" i][aria-label*="CSV" i]', "download-csv"),
        ('button[aria-label*="download csv" i]', "download-csv-phrase"),
        ('button[aria-label*="CSV" i]', "csv-aria"),
        ('button[title*="CSV" i]', "csv-title"),
        ('[role="button"][aria-label*="CSV" i]', "role-csv"),
        ("button.export", "export-class"),
    )
    for sel, tag in priority:
        loc = widget.locator(sel)
        n = loc.count()
        for i in range(n):
            pairs.append((loc.nth(i), f"{tag}[{i}]"))
    return pairs


def _fallback_export_candidates(page) -> list[tuple[object, str]]:
    """Fallback when TIMESERIES root is missing — still prefer CSV-hinted controls first."""
    pairs: list[tuple[object, str]] = []
    for sel, tag in (
        ('explore-widget[widget-type="TIMESERIES"] button[aria-label*="CSV" i]', "explore-widget/csv"),
        ('explore-widget[widget-type="TIMESERIES"] button.export', "explore-widget/ts"),
        ('[data-explore-widget-name="TIMESERIES"] button[aria-label*="CSV" i]', "data-name/csv"),
        ('explore-line-chart button[aria-label*="CSV" i]', "line-chart/csv"),
        ("explore-line-chart button.export", "explore-line-chart"),
        ('[data-explore-widget-name="TIMESERIES"] button.export', "data-explore-widget-name"),
        ("widget-timeseries button.export", "widget-timeseries"),
        ("button.export", "global-button.export"),
    ):
        loc = page.locator(sel)
        n = loc.count()
        for i in range(n):
            pairs.append((loc.nth(i), f"{tag}[{i}]"))
    return pairs


def _try_export_menu_then_csv(
    page,
    widget,
    out_path: Path,
    timeout_ms: int,
) -> bool:
    """Some Explore builds open a menu from ``.export``; CSV is a **menu item**, not the first click."""
    try:
        opener = widget.locator("button.export").first
        opener.wait_for(state="visible", timeout=15_000)
    except Exception:
        return False
    try:
        opener.click(timeout=10_000)
        page.wait_for_timeout(600)
    except Exception:
        return False

    # Menu is often portaled to ``document`` — search page-wide, scoped by visible menu.
    try:
        csv_item = page.get_by_role("menuitem", name=re.compile(r"csv", re.I)).first
        csv_item.wait_for(state="visible", timeout=8000)
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    try:
        with page.expect_download(timeout=timeout_ms) as dl:
            csv_item.click(timeout=15_000)
        dl.value.save_as(str(out_path))
        ok = _is_valid_trends_csv(out_path)
        if not ok:
            _log_invalid_download(out_path)
            out_path.unlink(missing_ok=True)
        else:
            return True
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def click_csv_download(page, out_path: Path, timeout_ms: int = 120_000) -> None:
    """Download IoT data as CSV from the Interest-over-time widget.

    Uses ``[widget-type="TIMESERIES"]`` so we do not click exports for maps / related queries.
    Button order prefers **CSV-labeled** controls over a bare ``button.export`` (which may
    open a format menu). If direct clicks fail validation, tries **menu → CSV** once.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dismiss_consent_loop(page)
    reason = _detect_hard_block(page)
    if reason:
        raise RuntimeError(f"Trends page looks blocked ({reason}); export controls never appear.")

    # Scroll main document so chart / toolbar can mount (lazy layouts).
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
        page.wait_for_timeout(600)
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    dismiss_consent_loop(page, rounds=2)

    # Give Interest-over-time time to render (chart API can lag behind shell; 429 retries are slower).
    roots = _iot_widget_roots(page)
    ready_ms = _timeseries_ready_budget_ms(timeout_ms)
    try:
        roots.first.wait_for(state="visible", timeout=min(ready_ms, timeout_ms))
    except Exception:
        pass
    _wait_timeseries_export_ready(page, roots, ready_ms)
    page.wait_for_timeout(1200)

    candidates: list[tuple[object, str]] = []
    nw = roots.count()
    for wi in range(nw):
        for btn, label in _export_buttons_in_widget(roots.nth(wi)):
            candidates.append((btn, f"TIMESERIES[{wi}]/{label}"))

    if not candidates:
        print(
            "  No in-widget export targets yet; trying export menu → CSV on TIMESERIES hosts …",
            flush=True,
        )
        if _menu_csv_on_timeseries_hosts(page, out_path, timeout_ms):
            return
        candidates = _fallback_export_candidates(page)

    if not candidates:
        raise RuntimeError(
            "No export button found (TIMESERIES widget / chart toolbar). "
            "Try TRENDS_HEADLESS=0 to inspect the page."
        )

    print(f"  Found {len(candidates)} candidate export control(s)", flush=True)

    last_err: Exception | None = None

    def _try_one(btn, tag: str, idx: int, total: int) -> bool:
        nonlocal last_err
        try:
            if not btn.is_visible():
                return False
            btn.scroll_into_view_if_needed(timeout=10_000)
            print(f"  Clicking {tag} ({idx + 1}/{total}) …", flush=True)
            with page.expect_download(timeout=timeout_ms) as dl:
                btn.click(timeout=15_000)
            dl.value.save_as(str(out_path))
            size = out_path.stat().st_size
            print(f"  Downloaded {size} bytes to {out_path}", flush=True)
            if _is_valid_trends_csv(out_path):
                return True
            _log_invalid_download(out_path)
            print(
                f"  Candidate {tag} did not yield a parseable IoT CSV; trying next …",
                flush=True,
            )
            out_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"  Candidate {tag} failed: {e}", flush=True)
            last_err = e
        return False

    total = len(candidates)
    for idx, (btn, tag) in enumerate(candidates):
        if _try_one(btn, tag, idx, total):
            return

    # Menu-driven export on every matched IoT host (CSV is often behind the flyout, not the first tap).
    if nw > 0:
        print("  Trying export menu → CSV on each TIMESERIES widget …", flush=True)
        for wi in range(nw):
            if _try_export_menu_then_csv(page, roots.nth(wi), out_path, timeout_ms):
                return
    print("  Trying export menu → CSV on known TIMESERIES host selectors …", flush=True)
    if _menu_csv_on_timeseries_hosts(page, out_path, timeout_ms):
        return

    # Page-wide ``button.export`` order is unstable; retry globals from last to first.
    global_pairs = [(b, t) for b, t in candidates if "global-button" in t]
    if len(global_pairs) > 1:
        print("  Retrying page-wide export buttons in reverse DOM order …", flush=True)
        total_r = len(global_pairs)
        for j, (btn, tag) in enumerate(reversed(global_pairs)):
            if _try_one(btn, tag, j, total_r):
                return

    tail = f" Caused by: {last_err}" if last_err else ""
    raise RuntimeError(
        f"No export produced a valid Trends CSV after trying {len(candidates)} candidate(s).{tail}"
    )


def _default_export_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_PLAYWRIGHT_TIMEOUT_MS", "120000").strip()
    return int(raw) if raw.isdigit() else 120_000


def _weekly_export_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_WEEKLY_EXPORT_TIMEOUT_MS", "180000").strip()
    return int(raw) if raw.isdigit() else 180_000


def _goto_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_GOTO_TIMEOUT_MS", "180000").strip()
    return int(raw) if raw.isdigit() else 180_000


def _networkidle_timeout_ms() -> int:
    raw = os.environ.get("TRENDS_NETWORKIDLE_TIMEOUT_MS", "60000").strip()
    return int(raw) if raw.isdigit() else 60_000


def _goto_wait_until_primary() -> str:
    w = os.environ.get("TRENDS_GOTO_WAIT_UNTIL", "domcontentloaded").strip().lower()
    if w in ("commit", "domcontentloaded", "load", "networkidle"):
        return w
    return "domcontentloaded"


def _goto_params_for_attempt(attempt: int) -> tuple[str, int]:
    """Return (wait_until, timeout_ms) for Trends navigation; later attempts get more time."""
    base = _goto_timeout_ms()
    primary = _goto_wait_until_primary()
    if attempt == 0:
        return primary, base
    if attempt == 1:
        return "load", base + 45_000
    return "domcontentloaded", base + 90_000


def _reset_page_before_retry(page) -> None:
    try:
        page.goto("about:blank", wait_until="commit", timeout=20_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)


def _fetch_max_attempts() -> int:
    raw = os.environ.get("TRENDS_FETCH_MAX_ATTEMPTS", "5").strip()
    try:
        n = int(raw)
        return max(1, min(n, 50))
    except ValueError:
        return 5


def _retry_sleep_before_attempt(attempt_index: int, *, rate_limited: bool) -> None:
    """Sleep before retry ``attempt_index`` (1 = first retry): exponential backoff + jitter."""
    if attempt_index < 1:
        return
    base_key = "TRENDS_RATE_LIMIT_RETRY_BASE_SEC" if rate_limited else "TRENDS_FETCH_RETRY_BASE_SEC"
    default_base = 75.0 if rate_limited else 18.0
    raw = os.environ.get(base_key, "").strip()
    try:
        base = float(raw)
    except ValueError:
        base = default_base
    try:
        max_sec = float(os.environ.get("TRENDS_FETCH_RETRY_MAX_SEC", "600").strip() or "600")
    except ValueError:
        max_sec = 600.0
    exp = min(base * (2 ** (attempt_index - 1)), max_sec)
    jitter = random.uniform(0, min(45.0, exp * 0.2))
    delay = exp + jitter
    print(f"  Waiting {delay:.0f}s before retry (rate_limit_backoff={rate_limited}) …", flush=True)
    time.sleep(delay)


def _file_looks_like_rate_limit(path: Path) -> bool:
    """Tiny HTML/JSON stubs Google returns under HTTP 429 often mention status or rate limits."""
    if not path.is_file():
        return False
    try:
        head = path.read_text(encoding="utf-8-sig", errors="replace")[:6000].lower()
    except Exception:
        return False
    needles = (
        "429",
        "too many requests",
        "rate limit",
        "quota exceeded",
        "too many queries",
        "resource exhausted",
    )
    return any(n in head for n in needles)


def _page_body_rate_limited(page) -> bool:
    """Visible Explore page sometimes embeds error text without HTTP 429 on main navigation."""
    try:
        text = page.inner_text("body", timeout=10_000).lower()
    except Exception:
        return False
    needles = ("too many requests", "429", "rate limit", "quota exceeded")
    return any(n in text for n in needles)


def _exception_suggests_rate_limit(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "too many" in msg or "rate limit" in msg


def _is_valid_trends_csv(path: Path) -> bool:
    """True if the file matches what :func:`stitch_daily.load_trends_csv` can parse (real IoT export)."""
    if not path.is_file() or path.stat().st_size < 30:
        return False
    try:
        from stitch_daily import load_trends_csv

        df = load_trends_csv(str(path))
        if df.empty or len(df.columns) < 1:
            return False
        return True
    except Exception:
        pass
    # Fallback: legacy heuristic (HTML stubs often fail line/header checks)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) < 3:
            return False
        for line in lines[:12]:
            low = line.lower()
            if any(h in low for h in ("week", "day", "date", "time")) and ("," in line or "\t" in line):
                return True
        return False
    except Exception:
        return False


def fetch_one(
    page,
    url: str,
    out_path: Path,
    pause_s: float,
    *,
    export_timeout_ms: int | None = None,
    max_attempts: int | None = None,
) -> None:
    timeout_ms = export_timeout_ms if export_timeout_ms is not None else _default_export_timeout_ms()
    idle_ms = _networkidle_timeout_ms()
    ma = max_attempts if max_attempts is not None else _fetch_max_attempts()
    last_err: Exception | None = None
    pending_rl = False

    for attempt in range(ma):
        try:
            if attempt:
                print(f"  retry {attempt + 1}/{ma} …", flush=True)
                _retry_sleep_before_attempt(attempt, rate_limited=pending_rl)
                pending_rl = False
                _reset_page_before_retry(page)
            wait_until, nav_ms = _goto_params_for_attempt(attempt)
            page.set_default_navigation_timeout(nav_ms)
            resp = page.goto(url, wait_until=wait_until, timeout=nav_ms)
            if resp is not None and resp.status == 429:
                print("  HTTP 429 Too Many Requests — Google rate limit on navigation.", flush=True)
                pending_rl = True
                last_err = RuntimeError("HTTP 429 Too Many Requests")
                if attempt >= ma - 1:
                    raise last_err
                continue
            page.wait_for_timeout(5000)
            dismiss_consent_loop(page)
            page.wait_for_timeout(2500)
            try:
                page.wait_for_load_state("networkidle", timeout=idle_ms)
            except PlaywrightTimeout:
                pass
            if _page_body_rate_limited(page):
                print("  Page content suggests rate limiting (429 / quota); backing off.", flush=True)
                pending_rl = True
                if attempt >= ma - 1:
                    raise RuntimeError(
                        "Google Trends appears rate-limited (429). Space out runs or wait before retrying."
                    )
                continue
            click_csv_download(page, out_path, timeout_ms=timeout_ms)

            if _is_valid_trends_csv(out_path):
                time.sleep(pause_s)
                return

            if _file_looks_like_rate_limit(out_path):
                print("  Download looks like a rate-limit / error stub (HTTP 429), not CSV.", flush=True)
                pending_rl = True
            else:
                print("  CSV validation failed (empty/malformed); retrying …", flush=True)
            out_path.unlink(missing_ok=True)
            last_err = RuntimeError("Invalid or blocked Trends CSV export")
            if attempt >= ma - 1:
                raise RuntimeError(
                    "Trends CSV still invalid after retries — often HTTP 429 if Google rate-limits "
                    "automated requests. Increase delays between jobs or run less often."
                ) from last_err
            continue

        except Exception as e:
            last_err = e
            if _exception_suggests_rate_limit(e):
                pending_rl = True
                print("  Error suggests Google rate limiting (429).", flush=True)
            if attempt >= ma - 1:
                raise
            continue

    assert last_err is not None
    raise last_err


def main() -> int:
    if not CHUNKS_JSON.is_file():
        print(f"Missing chunks.json at {CHUNKS_JSON}", file=sys.stderr, flush=True)
        return 2
    try:
        _ensure_playwright_browsers()
    except Exception as e:
        print(str(e), file=sys.stderr, flush=True)
        return 2
    chunks = json.loads(CHUNKS_JSON.read_text())
    geo = os.environ.get("TRENDS_GEO", "GB").strip()
    hl = os.environ.get("TRENDS_HL", "en-GB").strip()
    try:
        q_term = active_query_term()
    except RuntimeError as e:
        print(str(e), file=sys.stderr, flush=True)
        return 2

    weekly_start = os.environ.get("TRENDS_WEEKLY_START", "2024-09-01").strip()
    weekly_end = os.environ.get("TRENDS_WEEKLY_END", "2026-03-31").strip()
    ref_path = DATA_ROOT / "reference_weekly.csv"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "chunks").mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=_headless_from_env(),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            locale="en-GB",
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            java_script_enabled=True,
        )
        page = context.new_page()

        step = 0

        weekly_parts: list[Path] = []
        try:
            step += 1
            url = explore_url_range(weekly_start, weekly_end, geo=geo, hl=hl, q_term=q_term)
            part = DATA_ROOT / "_weekly_00.csv"
            print(f"[{step}] reference_weekly ({q_term!r}) …", flush=True)
            fetch_one(
                page,
                url,
                part,
                pause_s=22.0,
                export_timeout_ms=_weekly_export_timeout_ms(),
            )
            weekly_parts.append(part)
            print(f"  → saved {part}", flush=True)
            merge_multitimeline_csvs(weekly_parts, ref_path, date_col_name="Week")
            print(f"  → merged → {ref_path}", flush=True)
        except Exception as e:
            print(f"  ERROR (weekly): {e}", file=sys.stderr, flush=True)
            context.close()
            browser.close()
            return 1
        finally:
            for pth in weekly_parts:
                pth.unlink(missing_ok=True)

        for c in chunks:
            dest = _chunk_csv_path(c["filename"])
            chunk_parts: list[Path] = []
            idx = int(c.get("index", 0))
            try:
                step += 1
                url = explore_url_range(c["start"], c["end"], geo=geo, hl=hl, q_term=q_term)
                part = DATA_ROOT / "chunks" / f"_chunk{idx:02d}.csv"
                print(f"[{step}] {dest.name} ({q_term!r}) …", flush=True)
                fetch_one(
                    page,
                    url,
                    part,
                    pause_s=18.0,
                    export_timeout_ms=_default_export_timeout_ms(),
                )
                chunk_parts.append(part)
                print(f"  → saved {part}", flush=True)
                merge_multitimeline_csvs(chunk_parts, dest, date_col_name="Day")
                print(f"  → merged → {dest}", flush=True)
            except Exception as e:
                print(f"  ERROR ({dest.name}): {e}", file=sys.stderr, flush=True)
                context.close()
                browser.close()
                return 1
            finally:
                for pth in chunk_parts:
                    pth.unlink(missing_ok=True)

        context.close()
        browser.close()

    print("All downloads finished.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
