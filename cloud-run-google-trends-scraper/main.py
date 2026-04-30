"""Cloud Run entrypoint: optional plan → Playwright fetch → stitch → optional GCS / BQ.

**Layout:** ship a single flat folder as Cloud Run source (e.g. ``main.py``,
``requirements.txt``, ``project.toml``, and the sibling ``*.py`` modules). There is
no ``DEPLOY_ROOT`` (or similar) environment variable: ``_DEPLOY_ROOT`` is always
``Path(__file__).resolve().parent`` — the directory that contains this file in the
image. Leave ``TRENDS_DATA_DIR`` unset to use that same directory for data and outputs;
set it only if you need a different writable path (e.g. ``/tmp``).

Environment:

- ``TRENDS_DATA_DIR``: writable working directory (default: this file’s directory).
- ``START_DATE`` / ``END_DATE`` (``YYYY-MM-DD``): if **both** set (``full`` mode), run
  ``plan_chunks.py`` first, write ``chunks.json``, and set ``TRENDS_WEEKLY_START`` /
  ``TRENDS_WEEKLY_END`` for the weekly Trends URL.
- ``TRENDS_RUN_MODE``: ``full`` (default) uses explicit ``START_DATE``/``END_DATE`` for a
  full refresh. ``daily`` (alias ``incremental``) ignores those and sets the pull window to
  ``TRENDS_DAILY_LOOKBACK_DAYS`` ending at ``TRENDS_AS_OF_DATE`` or today (UTC), so each run
  fetches a rolling window long enough to stitch chunks and calibrate to weekly, then BQ
  upserts new days without wiping history.
- ``TRENDS_DAILY_LOOKBACK_DAYS`` (default ``180``): inclusive calendar-day span for ``daily``
  mode (raise if below stitching needs — aim ≥ chunk length + overlap).
- ``TRENDS_BACKFILL_NEW_TERMS`` (default ``1`` / ``true``): in ``daily`` mode, when
  ``BQ_TRENDS_DAILY_TABLE`` / ``TRENDS_DAILY_TABLE_ID`` resolves to a table, each term with
  **no** existing rows in that table is planned from ``TRENDS_WEEKLY_START`` through the same
  end date as the daily window (full history for first ingest). Terms that already have data
  keep the rolling lookback only. Set to ``0`` / ``false`` to always use lookback for every term.
- ``TRENDS_AS_OF_DATE`` (optional ``YYYY-MM-DD``): end of the daily window (default UTC today).
- ``TRENDS_TERMS``: optional — comma-separated list **or** JSON array of query strings, e.g.
  ``["brake pads","car battery"]``. Each term runs in a **separate** pipeline step; each Trends
  URL contains **exactly one** ``q`` (no multi-term compare normalization).
- ``QUERY_TERM``: if ``TRENDS_TERMS`` is unset, a single term only (same one-term-per-URL rule).
- ``TRENDS_GEO``, ``TRENDS_HL``: passed to ``plan_chunks`` and ``fetch_playwright``.
- ``TRENDS_CHUNK_DAYS``, ``TRENDS_OVERLAP_DAYS``: optional; forwarded to ``plan_chunks``.
- ``TRENDS_REGION_LABEL`` (and ``QUERY_TERM`` / ``TRENDS_TERMS``): used for export headers
  and to align BQ ``term`` text with the weekly reference when ``STITCH_WEEKLY_REF_SOURCE=bq``.
- ``GCS_OUTPUT_BUCKET``: optional; uploads artifacts under ``runs/<RUN_ID>/``.
- ``RUN_ID``: optional GCS prefix (default UTC timestamp).
- ``BQ_WEEKLY_TABLE``: optional weekly table. If it contains **two dots** (``project.dataset.table``),
  it is used as-is. If it is a **bare table id** (no dots), it is combined with ``PROJECT_ID``
  and ``DATASET_ID`` when those are set. If unset, ``REFERENCE_WEEKLY_TABLE_ID`` is used as the
  table segment together with ``PROJECT_ID`` and ``DATASET_ID``.
- ``PROJECT_ID``, ``DATASET_ID``, ``REFERENCE_WEEKLY_TABLE_ID``: split form for the weekly table
  (see above). Not a ``gs://`` URI.
- ``TRENDS_DAILY_TABLE_ID``, ``STITCH_QUALITY_TABLE_ID``: with ``PROJECT_ID`` + ``DATASET_ID``,
  target tables for loading ``trends_daily.csv`` and ``stitch_quality.json`` (optional
  ``BQ_TRENDS_DAILY_TABLE`` / ``BQ_STITCH_QUALITY_TABLE`` overrides, same rules as ``BQ_WEEKLY_TABLE``).
- ``STITCH_RENORMALIZE_0_100`` (default **off**): set to ``1`` / ``true`` / ``yes`` /
  ``on`` to min–max renormalize each term’s daily series to 0–100 after calibration
  (forwards ``--renormalize-0-100`` to ``stitch_daily.py``). If unset, ``trends_daily.csv``
  keeps the calibrated, unbounded scale.
- ``STITCH_WEEKLY_REF_SOURCE``: set ``bq`` to calibrate from BigQuery instead of the fresh CSV
  (requires a resolved weekly table as above).

**Functions Framework (HTTP):** the HTTP target is ``run_trends_job``. At **runtime**,
``FUNCTION_TARGET=run_trends_job`` (or ``--target=run_trends_job`` on the CLI). The
buildpack reads ``project.toml`` in the **source root** for ``GOOGLE_FUNCTION_TARGET``,
``GOOGLE_FUNCTION_SOURCE`` (``main.py``), and ``GOOGLE_FUNCTION_SIGNATURE_TYPE=http``.

**Cloud Scheduler (one term per invocation):** send an HTTP request to the Cloud Run URL
with a **custom header** carrying the query string. The handler treats that as
``QUERY_TERM`` for this run only (it clears ``TRENDS_TERMS`` so a single term is used).
Header names are matched case-insensitively; the first non-empty value wins, in this order:

- ``X-Trends-Query-Term`` (recommended for Cloud Scheduler custom headers)
- ``X-Query-Term``
- ``Query-Term`` or ``Query_Term``
- ``QUERY_TERM``

If ``RUN_ID`` is unset/empty, the handler appends a short slug derived from the term to
the default timestamp so parallel jobs (one Scheduler job per term) get distinct
``runs/<RUN_ID>/`` prefixes on GCS. Prefer **Cloud Run concurrency = 1** (or one request
per instance) so per-request ``os.environ`` overrides do not overlap.

**Buildpack source root** must be that same flat folder (zip root = directory containing
``main.py``). Example: ``gcloud run deploy … --source=/path/to/that/folder``. If you
upload a parent directory instead, set build-time ``GOOGLE_FUNCTION_SOURCE`` to the path
of this file under that root (see
https://cloud.google.com/docs/buildpacks/service-specific-configs ).

For a one-shot **job**, invoke the service once; the handler runs ``main()`` synchronously.

You can still run ``python main.py`` locally; that bypasses HTTP and exits with ``main()``’s code.

Playwright: source-only **buildpack** deploys do **not** run ``playwright install``, so
Chromium is missing at runtime. Use the included **Dockerfile** (``playwright install
--with-deps chromium``) when deploying from source; Cloud Run picks it over buildpacks when
it is present in the upload root.
"""

from __future__ import annotations

import functions_framework
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from flask import jsonify

from terms_input import parse_terms_from_env, single_term_env

# App + data root in the container: directory containing this file (flat Cloud Run source).
_DEPLOY_ROOT = Path(__file__).resolve().parent
_FETCH = _DEPLOY_ROOT / "fetch_playwright.py"
_STITCH = _DEPLOY_ROOT / "stitch_daily.py"
_PLAN = _DEPLOY_ROOT / "plan_chunks.py"
_BQ = _DEPLOY_ROOT / "bq_reference_weekly.py"


def _resolved_bq_table(bq_key: str, ref_key: str) -> str:
    """Return ``project.dataset.table`` from ``BQ_*`` and ``*_TABLE_ID`` env vars."""
    project = os.environ.get("PROJECT_ID", "").strip()
    dataset = os.environ.get("DATASET_ID", "").strip()
    ref_table = os.environ.get(ref_key, "").strip()
    single = os.environ.get(bq_key, "").strip()

    def compose(p: str, d: str, t: str) -> str:
        if p and d and t:
            return f"{p}.{d}.{t}"
        return ""

    if single:
        parts = single.split(".")
        if len(parts) >= 3:
            return single
        if len(parts) == 1 and project and dataset:
            return compose(project, dataset, parts[0])
        if len(parts) == 2 and project:
            return f"{project}.{single}"
        if len(parts) == 2:
            return single
        return ""
    return compose(project, dataset, ref_table)


def _resolved_bq_weekly_table() -> str:
    return _resolved_bq_table("BQ_WEEKLY_TABLE", "REFERENCE_WEEKLY_TABLE_ID")


def _resolved_bq_trends_daily_table() -> str:
    return _resolved_bq_table("BQ_TRENDS_DAILY_TABLE", "TRENDS_DAILY_TABLE_ID")


def _resolved_bq_stitch_quality_table() -> str:
    return _resolved_bq_table("BQ_STITCH_QUALITY_TABLE", "STITCH_QUALITY_TABLE_ID")


def _data_root() -> Path:
    return Path(os.environ.get("TRENDS_DATA_DIR", str(_DEPLOY_ROOT))).resolve()


def _run_id() -> str:
    return os.environ.get("RUN_ID") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _slug_for_run_id(term: str, max_len: int = 80) -> str:
    """ASCII-ish slug for ``RUN_ID`` suffix (GCS path) when one term comes from an HTTP header."""
    parts: list[str] = []
    for c in term.strip():
        if c.isalnum() or c in "._-":
            parts.append(c)
        elif c.isspace():
            parts.append("_")
        else:
            parts.append("_")
    s = "".join(parts).strip("_") or "term"
    while "__" in s:
        s = s.replace("__", "_")
    return s[:max_len].rstrip("_") or "term"


def _query_term_from_http_request(request) -> Optional[str]:
    """Single Trends query from HTTP headers (Cloud Scheduler / load balancers).

    Flask/Werkzeug header lookup is case-insensitive. First non-empty wins.
    """
    h = request.headers
    for name in (
        "X-Trends-Query-Term",
        "X-Query-Term",
        "Query-Term",
        "Query_Term",
        "QUERY_TERM",
    ):
        raw = h.get(name)
        if raw is None:
            continue
        v = str(raw).strip()
        if v:
            return v
    return None


def _run_mode() -> str:
    m = os.environ.get("TRENDS_RUN_MODE", "full").strip().lower()
    if m in ("daily", "incremental"):
        return "daily"
    return "full"


def _daily_plan_date_bounds(env: dict) -> Optional[Tuple[date, date]]:
    """Compute inclusive [start, end] for ``daily`` mode from lookback and optional anchor."""
    raw = env.get("TRENDS_DAILY_LOOKBACK_DAYS", "180").strip()
    try:
        lookback = int(raw)
    except ValueError:
        print(f"TRENDS_DAILY_LOOKBACK_DAYS must be an integer (got {raw!r})", file=sys.stderr)
        return None
    if lookback < 7:
        print(
            f"TRENDS_DAILY_LOOKBACK_DAYS must be at least 7 (got {lookback})",
            file=sys.stderr,
        )
        return None
    if lookback < 35:
        print(
            f"WARN: TRENDS_DAILY_LOOKBACK_DAYS={lookback} is short for chunk overlap + weekly "
            "calibration; consider 90–180.",
            file=sys.stderr,
        )

    as_of_s = env.get("TRENDS_AS_OF_DATE", "").strip()
    if as_of_s:
        try:
            end_d = date.fromisoformat(as_of_s)
        except ValueError:
            print(f"TRENDS_AS_OF_DATE must be YYYY-MM-DD (got {as_of_s!r})", file=sys.stderr)
            return None
    else:
        end_d = datetime.now(timezone.utc).date()

    start_d = end_d - timedelta(days=lookback - 1)
    return start_d, end_d


def _maybe_plan_chunks(data: Path, env: dict) -> Optional[int]:
    # When ``_CHUNK_PLAN_DATES_PRESET`` is set, ``START_DATE`` / ``END_DATE`` are already set
    # (e.g. per-term full backfill from ``TRENDS_WEEKLY_START`` vs. rolling lookback).
    if _run_mode() == "daily" and not env.get("_CHUNK_PLAN_DATES_PRESET"):
        bounds = _daily_plan_date_bounds(env)
        if bounds is None:
            return 2
        start_d, end_d = bounds
        env["START_DATE"] = start_d.isoformat()
        env["END_DATE"] = end_d.isoformat()
        print(
            f"TRENDS_RUN_MODE=daily: planning window {env['START_DATE']} … {env['END_DATE']} "
            f"({env.get('TRENDS_DAILY_LOOKBACK_DAYS', '180').strip() or '180'} day lookback, UTC anchor)",
            flush=True,
        )

    start = env.get("START_DATE", "").strip()
    end = env.get("END_DATE", "").strip()
    if not start and not end:
        return None
    if not start or not end:
        print("Set both START_DATE and END_DATE, or neither.", file=sys.stderr)
        return 2
    out = data / "chunks.json"
    cmd = [
        sys.executable,
        str(_PLAN),
        "--start",
        start,
        "--end",
        end,
        "--output",
        str(out),
    ]
    plan_term = env.get("_PLAN_SAMPLE_TERM", "").strip()
    if plan_term:
        cmd.extend(["--terms", plan_term])
    for flag, key in (("--geo", "TRENDS_GEO"), ("--hl", "TRENDS_HL")):
        v = env.get(key, "").strip()
        if v:
            cmd.extend([flag, v])
    cd = env.get("TRENDS_CHUNK_DAYS", "").strip()
    if cd.isdigit():
        cmd.extend(["--chunk-days", cd])
    od = env.get("TRENDS_OVERLAP_DAYS", "").strip()
    if od.isdigit():
        cmd.extend(["--overlap-days", od])

    print(f"Planning chunks → {out}", flush=True)
    r = subprocess.run(cmd, cwd=str(data), env=env)
    if r.returncode != 0:
        return r.returncode
    env["TRENDS_WEEKLY_START"] = start
    env["TRENDS_WEEKLY_END"] = end
    return None


def _date_span_for_bq(data: Path, env: dict) -> Tuple[Optional[date], Optional[date]]:
    s, e = env.get("START_DATE", "").strip(), env.get("END_DATE", "").strip()
    if s and e:
        return date.fromisoformat(s), date.fromisoformat(e)
    plan_path = data / "chunks.json"
    if not plan_path.is_file():
        return None, None
    plan = json.loads(plan_path.read_text())
    if not plan:
        return None, None
    return date.fromisoformat(plan[0]["start"]), date.fromisoformat(plan[-1]["end"])


def _load_bq_module():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("bq_reference_weekly", _BQ)
    mod = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write_resolved_chunks(data: Path) -> Path:
    plan_path = data / "chunks.json"
    plan = json.loads(plan_path.read_text())
    resolved = []
    for c in plan:
        row = dict(c)
        row["filename"] = str(data / "chunks" / Path(c["filename"]).name)
        resolved.append(row)
    out = Path("/tmp/chunks_resolved.json")
    out.write_text(json.dumps(resolved, indent=2))
    return out


def _upload_gcs(bucket: str, run_id: str, data: Path) -> None:
    from google.cloud import storage

    client = storage.Client()
    b = client.bucket(bucket)
    prefix = f"runs/{run_id}/"

    for name in ("trends_daily.csv", "stitch_quality.json", "reference_weekly.csv"):
        p = data / name
        if p.is_file():
            b.blob(prefix + name).upload_from_filename(str(p))

    ch = data / "chunks"
    if ch.is_dir():
        for p in sorted(ch.glob("chunk_*.csv")):
            b.blob(prefix + "chunks/" + p.name).upload_from_filename(str(p))

    print(f"Uploaded to gs://{b.name}/{prefix}", flush=True)


def _backfill_new_terms_from_bq(env: dict) -> bool:
    """Whether daily mode should use full history for terms missing from ``trends_daily`` BQ."""
    raw = env.get("TRENDS_BACKFILL_NEW_TERMS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _inter_term_pause_sec() -> float:
    raw = os.environ.get("TRENDS_INTER_TERM_PAUSE_SEC", "8").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 8.0


def _merge_wide_frames(acc, path: Path):
    """Outer-join another Trends CSV (date index, one or more term columns) onto accumulator."""
    from stitch_daily import load_trends_csv

    df = load_trends_csv(str(path))
    if acc is None:
        return df
    return acc.join(df, how="outer")


def _merge_quality_json(acc: dict | None, path: Path) -> dict:
    q = json.loads(path.read_text())
    if acc is None:
        return q
    for sec in ("stitching", "calibration", "renormalization"):
        if sec not in q:
            continue
        acc.setdefault(sec, {})
        if isinstance(q[sec], dict):
            acc[sec].update(q[sec])
    return acc


def _write_merged_weekly_like_export(acc_ref, path: Path) -> None:
    """Match Google multiTimeline shape (preamble + ``Week`` column) for downstream parsers."""
    out_df = acc_ref.sort_index().reset_index()
    first_col = out_df.columns[0]
    out_df = out_df.rename(columns={first_col: "Week"})
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("Category: All categories\n\n")
        out_df.to_csv(f, index=False)


def main() -> int:
    data = _data_root()
    run_id = _run_id()
    env = {**os.environ, "TRENDS_DATA_DIR": str(data)}

    if not _FETCH.is_file():
        print(f"Missing {_FETCH}", file=sys.stderr)
        return 2
    if not _STITCH.is_file():
        print(f"Missing {_STITCH}", file=sys.stderr)
        return 2
    if _run_mode() == "daily" and not _PLAN.is_file():
        print(
            "TRENDS_RUN_MODE=daily requires plan_chunks.py beside main.py to build the rolling window.",
            file=sys.stderr,
        )
        return 2

    terms = parse_terms_from_env(env)
    if not terms:
        print(
            "Set TRENDS_TERMS (comma-separated or JSON array) or QUERY_TERM (single term).",
            file=sys.stderr,
        )
        return 2

    # Config anchor for “full history” backfill (``.env``); planning overwrites ``env`` copies later.
    trends_history_start_raw = env.get("TRENDS_WEEKLY_START", "2024-09-01").strip()

    bq_daily_early = _resolved_bq_trends_daily_table()
    daily_per_term_plans = (
        _run_mode() == "daily"
        and bool(bq_daily_early)
        and _backfill_new_terms_from_bq(env)
        and _PLAN.is_file()
    )

    env["_PLAN_SAMPLE_TERM"] = terms[0]

    print(f"TRENDS_DATA_DIR={data}", flush=True)
    print(f"TRENDS_RUN_MODE={_run_mode()}", flush=True)
    print(f"Terms ({len(terms)}): {terms}", flush=True)
    if _PLAN.is_file():
        if not daily_per_term_plans:
            rc = _maybe_plan_chunks(data, env)
            if rc is not None:
                return rc
        else:
            print(
                "TRENDS_RUN_MODE=daily with BQ daily table: chunk window is chosen per term "
                "(full history from TRENDS_WEEKLY_START when the term has no BQ rows).",
                flush=True,
            )
    else:
        print("plan_chunks.py not found; skipping dynamic chunk planning.", flush=True)

    if not daily_per_term_plans and not (data / "chunks.json").is_file():
        print(
            f"Missing {data / 'chunks.json'} — "
            "set START_DATE and END_DATE for a full run, use TRENDS_RUN_MODE=daily with "
            "plan_chunks, or ship chunks.json.",
            file=sys.stderr,
        )
        return 2

    bq_table = _resolved_bq_weekly_table()
    out_daily = data / "trends_daily.csv"
    out_quality = data / "stitch_quality.json"
    ref_disk = data / "reference_weekly.csv"

    use_bq_weekly_ref = os.environ.get("STITCH_WEEKLY_REF_SOURCE", "").strip().lower() == "bq"
    if use_bq_weekly_ref:
        if not bq_table:
            print(
                "STITCH_WEEKLY_REF_SOURCE=bq requires BQ_WEEKLY_TABLE or "
                "PROJECT_ID+DATASET_ID+REFERENCE_WEEKLY_TABLE_ID.",
                file=sys.stderr,
            )
            return 2

    ref_csv = ref_disk
    if use_bq_weekly_ref and not daily_per_term_plans:
        ref_csv = Path("/tmp/reference_weekly_for_stitch.csv")
        d0, d1 = _date_span_for_bq(data, env)
        print(f"Materializing weekly reference from BQ → {ref_csv} …", flush=True)
        _load_bq_module().materialize_table_to_stitch_csv(
            bq_table,
            ref_csv,
            start=d0,
            end=d1,
        )

    resolved: Path | None = None if daily_per_term_plans else _write_resolved_chunks(data)
    renorm = os.environ.get("STITCH_RENORMALIZE_0_100", "").strip().lower()
    renorm_on = renorm in ("1", "true", "yes", "on")

    if daily_per_term_plans:
        import bq_run_outputs as _bq_run_terms  # type: ignore  # same directory on Cloud Run

    acc_daily = None
    acc_ref = None
    acc_quality: dict | None = None
    pause_between = _inter_term_pause_sec()

    for ti, term in enumerate(terms):
        if ti and pause_between > 0:
            print(f"Pausing {pause_between}s between terms (rate limits) …", flush=True)
            time.sleep(pause_between)

        print(f"======== Term {ti + 1}/{len(terms)}: {term!r} ========", flush=True)
        term_env = single_term_env(env, term)

        if daily_per_term_plans:
            assert bq_daily_early is not None
            end_bounds = _daily_plan_date_bounds(term_env)
            if end_bounds is None:
                return 2
            lookback_start_d, end_d = end_bounds
            has_rows = _bq_run_terms.term_has_trends_daily_rows(bq_daily_early, term)
            if has_rows:
                start_d = lookback_start_d
                print(
                    f"Term {term!r}: found rows in daily BQ → window "
                    f"{start_d.isoformat()} … {end_d.isoformat()} (TRENDS_DAILY_LOOKBACK_DAYS).",
                    flush=True,
                )
            else:
                try:
                    hist = date.fromisoformat(trends_history_start_raw)
                except ValueError:
                    print(
                        "TRENDS_WEEKLY_START must be YYYY-MM-DD when backfilling a term with no "
                        f"daily BQ rows (got {trends_history_start_raw!r}).",
                        file=sys.stderr,
                    )
                    return 2
                start_d = hist
                print(
                    f"Term {term!r}: no daily BQ rows → window "
                    f"{start_d.isoformat()} … {end_d.isoformat()} (from TRENDS_WEEKLY_START).",
                    flush=True,
                )
            term_env["_PLAN_SAMPLE_TERM"] = term
            term_env["_CHUNK_PLAN_DATES_PRESET"] = "1"
            term_env["START_DATE"] = start_d.isoformat()
            term_env["END_DATE"] = end_d.isoformat()
            rc = _maybe_plan_chunks(data, term_env)
            if rc is not None:
                return rc
            if use_bq_weekly_ref:
                ref_csv = Path("/tmp/reference_weekly_for_stitch.csv")
                d0, d1 = _date_span_for_bq(data, term_env)
                print(f"Materializing weekly reference from BQ → {ref_csv} …", flush=True)
                _load_bq_module().materialize_table_to_stitch_csv(
                    bq_table,
                    ref_csv,
                    start=d0,
                    end=d1,
                )
            else:
                ref_csv = ref_disk
            resolved = _write_resolved_chunks(data)
        assert resolved is not None

        print("Playwright downloads …", flush=True)
        r = subprocess.run([sys.executable, str(_FETCH)], cwd=str(data), env=term_env)
        if r.returncode != 0:
            return r.returncode

        if bq_table and _BQ.is_file():
            print(f"Merging reference_weekly.csv → BigQuery `{bq_table}` …", flush=True)
            _load_bq_module().merge_weekly_csv_into_table(
                ref_disk,
                bq_table,
                run_id=run_id,
            )

        print("Stitch + calibrate …", flush=True)
        stitch_args = [
            sys.executable,
            str(_STITCH),
            "--chunks",
            str(resolved),
            "--reference-weekly",
            str(ref_csv),
            "--out",
            str(out_daily),
            "--quality-out",
            str(out_quality),
        ]
        if renorm_on:
            stitch_args.append("--renormalize-0-100")
        r = subprocess.run(
            stitch_args,
            cwd=str(data),
            env=term_env,
        )
        if r.returncode != 0:
            return r.returncode

        acc_daily = _merge_wide_frames(acc_daily, out_daily)
        acc_ref = _merge_wide_frames(acc_ref, ref_disk)
        acc_quality = _merge_quality_json(acc_quality, out_quality)

    if acc_daily is not None:
        acc_daily.sort_index().to_csv(out_daily)
    if acc_ref is not None:
        _write_merged_weekly_like_export(acc_ref, ref_disk)
    if acc_quality is not None:
        out_quality.write_text(json.dumps(acc_quality, indent=2), encoding="utf-8")

    bq_daily = _resolved_bq_trends_daily_table()
    bq_stitch = _resolved_bq_stitch_quality_table()
    if bq_daily or bq_stitch:
        import bq_run_outputs  # type: ignore  # same directory on Cloud Run

        if bq_daily:
            print(f"Loading trends_daily.csv → BigQuery `{bq_daily}` …", flush=True)
            bq_run_outputs.load_trends_daily_to_bq(out_daily, bq_daily, run_id=run_id)
        if bq_stitch:
            print(f"Loading stitch_quality.json → BigQuery `{bq_stitch}` …", flush=True)
            bq_run_outputs.load_stitch_quality_to_bq(out_quality, bq_stitch, run_id=run_id)

    bucket = os.environ.get("GCS_OUTPUT_BUCKET", "").strip()
    if bucket.startswith("gs://"):
        bucket = bucket[5:]
    if bucket:
        print("GCS upload …", flush=True)
        print(f"Uploading to gs://{bucket}/runs/{run_id}/ …", flush=True)
        _upload_gcs(bucket, run_id, data)

    print("Job finished OK.", flush=True)
    return 0


@functions_framework.http
def run_trends_job(request):
    """HTTP entry point for Cloud Run + Functions Framework.

    Optional **headers** (see module docstring): when set, the value is used as ``QUERY_TERM``
    for this request only; ``TRENDS_TERMS`` is removed so exactly one term runs. Env vars
    are restored after ``main()`` so the same instance can serve another term safely when
    concurrency is 1.
    """
    start = datetime.now(timezone.utc)
    print(f"HTTP job start (UTC): {start:%Y-%m-%d %H:%M:%S}", flush=True)

    term_from_header = _query_term_from_http_request(request)
    env_snapshot: dict[str, str] = {
        k: os.environ[k] for k in ("QUERY_TERM", "TRENDS_TERMS", "RUN_ID") if k in os.environ
    }
    try:
        if term_from_header:
            print(f"HTTP header term override: {term_from_header!r}", flush=True)
            os.environ["QUERY_TERM"] = term_from_header
            os.environ.pop("TRENDS_TERMS", None)
            if not (os.environ.get("RUN_ID") or "").strip():
                base = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
                os.environ["RUN_ID"] = f"{base}_{_slug_for_run_id(term_from_header)}"
                print(f"RUN_ID (from header term, no RUN_ID in env): {os.environ['RUN_ID']}", flush=True)

        rc = main()
    finally:
        for key in ("QUERY_TERM", "TRENDS_TERMS", "RUN_ID"):
            if key in env_snapshot:
                os.environ[key] = env_snapshot[key]
            else:
                os.environ.pop(key, None)

    end = datetime.now(timezone.utc)
    elapsed = end - start
    minutes, seconds = divmod(elapsed.total_seconds(), 60)
    print(f"Time to run: {int(minutes)} minutes, {seconds:.1f} seconds", flush=True)
    payload: dict[str, Any] = {
        "status": "ok" if rc == 0 else "error",
        "exit_code": rc,
        "duration_seconds": round(elapsed.total_seconds(), 3),
        "started_at_utc": start.isoformat(),
        "finished_at_utc": end.isoformat(),
    }
    if term_from_header:
        payload["query_term"] = term_from_header
    status = 200 if rc == 0 else 500
    return jsonify(payload), status


if __name__ == "__main__":
    import argparse

    _ap = argparse.ArgumentParser(description="Run Trends pipeline (optional CLI overrides env).")
    _ap.add_argument(
        "--run-mode",
        choices=("full", "daily"),
        default=None,
        help="full = START_DATE/END_DATE; daily = rolling window from TRENDS_DAILY_LOOKBACK_DAYS",
    )
    _ap.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        metavar="N",
        help="sets TRENDS_DAILY_LOOKBACK_DAYS when --run-mode daily",
    )
    _ap.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="sets TRENDS_AS_OF_DATE (UTC end of daily window)",
    )
    _cli, _rest = _ap.parse_known_args()
    if _cli.run_mode is not None:
        os.environ["TRENDS_RUN_MODE"] = _cli.run_mode
    if _cli.lookback_days is not None:
        os.environ["TRENDS_DAILY_LOOKBACK_DAYS"] = str(_cli.lookback_days)
    if _cli.as_of is not None:
        os.environ["TRENDS_AS_OF_DATE"] = _cli.as_of.strip()

    raise SystemExit(main())