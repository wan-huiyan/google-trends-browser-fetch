"""Cloud Run entrypoint: Google Trends related queries (rising + top).

Runs alongside Kate's TIMESERIES scraper in the same folder but as a separate
Cloud Run service (set FUNCTION_TARGET=run_related_queries_job on that service).

What it does:
  1. Reads the list of tracked terms from BigQuery (``03_trends_daily``).
  2. Opens Google Trends Explore for each term with Playwright.
  3. Downloads the single combined CSV (RISING + TOP sections).
  4. Upserts all rows into BigQuery (``05_related_queries``).

Environment variables (set on the Cloud Run service):
  PROJECT_ID             GCP project (default: inferred from ADC)
  DATASET_ID             BigQuery dataset (default: google_trends)
  TRENDS_DAILY_TABLE_ID  source terms table (default: 03_trends_daily)
  RELATED_TABLE_ID       output table (default: 05_related_queries)
  TRENDS_GEO             Trends geo code (default: GB)
  TRENDS_HL              Trends locale (default: en-GB)
  TRENDS_DATE_PARAM      date range for Explore URL (default: today 3-m)
  TRENDS_HEADLESS        set 0 to show browser locally (default: 1)
  TRENDS_STORAGE_STATE   path to Playwright auth JSON (optional but reduces 429s)

Local run:
  python main_related.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import functions_framework
from flask import jsonify

from bq_related_queries import get_terms_from_bq, load_related_queries_to_bq
from fetch_related_queries import run_all_terms

_DEPLOY_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load .env from the script directory into os.environ (skips already-set keys)."""
    env_file = _DEPLOY_ROOT / ".env"
    if not env_file.is_file():
        return
    import re
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        if raw and raw[0] in ('"', "'"):
            closing = raw.find(raw[0], 1)
            val = raw[1:closing] if closing != -1 else raw[1:]
        else:
            val = re.sub(r"\s+#.*$", "", raw).strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def _resolved_table(env_key: str, default_name: str) -> str:
    project = os.environ.get("PROJECT_ID", "").strip()
    dataset = os.environ.get("DATASET_ID", "google_trends").strip()
    table = os.environ.get(env_key, default_name).strip()
    if "." in table:
        return table
    return f"{project}.{dataset}.{table}" if project else f"{dataset}.{table}"


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _inter_term_pause() -> float:
    raw = os.environ.get("TRENDS_INTER_TERM_PAUSE_SEC", "15").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 15.0


def main() -> int:
    run_id = _run_id()

    daily_table = _resolved_table("TRENDS_DAILY_TABLE_ID", "03_trends_daily")
    related_table = _resolved_table("RELATED_TABLE_ID", "05_related_queries")
    geo = os.environ.get("TRENDS_GEO", "GB").strip()
    hl = os.environ.get("TRENDS_HL", "en-GB").strip()
    date_param = os.environ.get("TRENDS_DATE_PARAM", "today 3-m").strip().strip('"').strip("'")

    print(f"Run ID: {run_id}", flush=True)
    print(f"Source terms: {daily_table}", flush=True)
    print(f"Output table: {related_table}", flush=True)
    print(f"Geo: {geo}, HL: {hl}, Date: {date_param}", flush=True)

    # Fallback term list when the BQ source table is empty or unavailable.
    fallback_raw = os.environ.get("TRENDS_TERMS", "").strip()
    fallback_terms = [t.strip() for t in fallback_raw.split(",") if t.strip()] if fallback_raw else []

    terms = get_terms_from_bq(daily_table) or fallback_terms
    if not terms:
        print("No terms found. Set TRENDS_TERMS or populate 03_trends_daily.", file=sys.stderr)
        return 2

    print(f"\nProcessing {len(terms)} terms: {terms}", flush=True)

    data_dir = Path(os.environ.get("TRENDS_DATA_DIR", str(_DEPLOY_ROOT)))
    rows = run_all_terms(
        terms,
        geo=geo,
        hl=hl,
        date_param=date_param,
        data_dir=data_dir,
        run_id=run_id,
        inter_term_pause_sec=_inter_term_pause(),
    )

    if not rows:
        print("No rows collected — nothing to upload.", flush=True)
        return 0

    print(f"\nCollected {len(rows)} total rows across {len(terms)} terms.", flush=True)
    load_related_queries_to_bq(rows, related_table, run_id=run_id)
    print("Job finished OK.", flush=True)
    return 0


@functions_framework.http
def run_related_queries_job(request):
    """HTTP entry point for Cloud Run + Functions Framework."""
    start = datetime.now(timezone.utc)
    print(f"HTTP job start (UTC): {start:%Y-%m-%d %H:%M:%S}", flush=True)
    rc = main()
    end = datetime.now(timezone.utc)
    elapsed = end - start
    minutes, seconds = divmod(elapsed.total_seconds(), 60)
    print(f"Time to run: {int(minutes)}m {seconds:.0f}s", flush=True)
    payload: dict[str, Any] = {
        "status": "ok" if rc == 0 else "error",
        "exit_code": rc,
        "duration_seconds": round(elapsed.total_seconds(), 1),
    }
    return (payload, 200 if rc == 0 else 500)


if __name__ == "__main__":
    raise SystemExit(main())
