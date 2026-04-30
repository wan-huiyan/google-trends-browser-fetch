"""Load stitched ``trends_daily.csv`` and ``stitch_quality.json`` into BigQuery.

**03_trends_daily** (typical name): one row per ``(day_start, term)`` with
``calibrated_value`` (FLOAT64), same ``term`` text as ``02_stitch_quality``.
The CSV remains wide; this module **melts** it to long for BigQuery.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from bq_reference_weekly import (
    _bq_query_location,
    _fully_qualified_table_id,
    ensure_dataset_exists,
    load_staging_table_from_rows,
    snapshot_upsert_long_format,
)


def term_has_trends_daily_rows(table_id: str, term: str) -> bool:
    """True if ``trends_daily`` has at least one row for ``term`` (any ``day_start``).

    Used to choose a short rolling lookback vs. a full-history fetch for new terms.
    If the table does not exist yet, returns False (treat as no data).
    """
    term = (term or "").strip()
    if not term:
        return False
    client = bigquery.Client()
    table_id = _fully_qualified_table_id(table_id, default_project=client.project)
    try:
        client.get_table(table_id)
    except NotFound:
        return False
    loc = _bq_query_location(client, table_id)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("term", "STRING", term)]
    )
    sql = f"SELECT 1 FROM `{table_id}` WHERE term = @term LIMIT 1"
    rows = list(client.query(sql, job_config=job_config, location=loc).result())
    return bool(rows)


def _sanitize_col(name: str) -> str:
    n = re.sub(r"[^0-9A-Za-z_]", "_", str(name).strip())
    if n and n[0].isdigit():
        n = f"c_{n}"
    return n or "col"


def _trends_daily_table_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("day_start", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("term", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("calibrated_value", "FLOAT64", mode="NULLABLE"),
    ]


TRENDS_DAILY_REQUIRED_FIELDS = frozenset(
    f.name for f in _trends_daily_table_schema()
)


def _ensure_trends_daily_table(
    client: bigquery.Client,
    table_id: str,
) -> None:
    """Create the long-format daily table if missing; error if a legacy wide table exists."""
    try:
        t = client.get_table(table_id)
    except NotFound:
        ensure_dataset_exists(client, table_id)
        t_new = bigquery.Table(table_id, schema=_trends_daily_table_schema())
        t_new.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="day_start",
        )
        client.create_table(t_new)
        print(f"Created BigQuery table {table_id}", flush=True)
        return
    names = {f.name for f in t.schema}
    if names != TRENDS_DAILY_REQUIRED_FIELDS:
        raise ValueError(
            f"Table {table_id} has unexpected schema (expected one row per day/term: "
            f"{sorted(TRENDS_DAILY_REQUIRED_FIELDS)}). If this is a legacy wide daily table, "
            "recreate the table with the new schema (term + calibrated_value)."
        )


def _stitch_quality_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("term", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("median_ratio", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("std_ratio", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("n_joins", "INT64", mode="NULLABLE"),
        bigquery.SchemaField("calib_scalar", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("daily_weekly_corr", "FLOAT64", mode="NULLABLE"),
    ]


def _ensure_stitch_quality_table(client: bigquery.Client, table_id: str) -> None:
    try:
        client.get_table(table_id)
    except NotFound:
        ensure_dataset_exists(client, table_id)
        client.create_table(bigquery.Table(table_id, schema=_stitch_quality_schema()))
        print(f"Created BigQuery table {table_id}", flush=True)


def load_trends_daily_to_bq(
    csv_path: Path,
    table_id: str,
    *,
    run_id: str,
) -> None:
    """Melt wide ``trends_daily.csv`` into BigQuery; upserts on ``(day_start, term)``.

    Existing rows for other keys are kept; this batch updates or inserts its keys only.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)
    client = bigquery.Client()
    table_id = _fully_qualified_table_id(table_id, default_project=client.project)

    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Expected a date index in trends_daily.csv (first column).")
    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "day_start"})
    out["day_start"] = pd.to_datetime(out["day_start"], errors="coerce").apply(
        lambda x: x.date().isoformat() if pd.notna(x) else None
    )
    if out["day_start"].isna().all():
        raise ValueError("Could not parse day_start in trends_daily.csv")
    raw_metrics = [c for c in out.columns if c != "day_start"]
    if not raw_metrics:
        raise ValueError("trends_daily.csv has no metric columns to load")
    renames = {c: _sanitize_col(c) for c in raw_metrics}
    out = out.rename(columns=renames)
    sani_to_label = {renames[c]: c for c in raw_metrics}
    mcols = [renames[c] for c in raw_metrics]

    _ensure_trends_daily_table(client, table_id)

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for _, r in out.iterrows():
        ds = r.get("day_start")
        if ds is None or (isinstance(ds, float) and pd.isna(ds)):
            continue
        for sc in mcols:
            v = r.get(sc)
            term = sani_to_label.get(sc, sc)
            if pd.isna(v):
                cal: Optional[float] = None
            else:
                cal = float(v)
            rows.append(
                {
                    "day_start": str(ds),
                    "run_id": run_id,
                    "ingested_at": now,
                    "term": str(term).strip(),
                    "calibrated_value": cal,
                }
            )

    if not rows:
        raise ValueError("no daily rows to load after parsing")

    t = client.get_table(table_id)
    loc = _bq_query_location(client, table_id)
    p, d, _ = table_id.split(".", 2)
    temp_id = f"{p}.{d}._dmerge_{uuid.uuid4().hex[:12]}"
    try:
        load_staging_table_from_rows(client, temp_id, list(t.schema), rows)
    except Exception:
        try:
            client.delete_table(temp_id, not_found_ok=True)
        except Exception:
            pass
        raise
    try:
        snapshot_upsert_long_format(
            client,
            table_id,
            temp_id,
            partition_field="day_start",
            key_date_field="day_start",
            key_term_field="term",
            location=loc,
        )
    finally:
        client.delete_table(temp_id, not_found_ok=True)


def load_stitch_quality_to_bq(
    json_path: Path,
    table_id: str,
    *,
    run_id: str,
) -> None:
    """One row per term: stitching + calibration metrics; replaces rows for this ``run_id``."""
    if not json_path.is_file():
        raise FileNotFoundError(json_path)
    data = json.loads(json_path.read_text())
    st = data.get("stitching", {}) or {}
    cal = data.get("calibration", {}) or {}
    terms = sorted(set(st) | set(cal))
    if not terms:
        raise ValueError("stitch_quality has no per-term data")

    client = bigquery.Client()
    table_id = _fully_qualified_table_id(table_id, default_project=client.project)
    _ensure_stitch_quality_table(client, table_id)

    def _float_opt(key: str, d: dict) -> Optional[float]:
        v = d.get(key)
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v) if v == v else None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for t in terms:
        s = st.get(t, {}) or {}
        c = cal.get(t, {}) or {}
        rows.append(
            {
                "run_id": run_id,
                "term": t,
                "ingested_at": now,
                "median_ratio": _float_opt("median_ratio", s),
                "std_ratio": _float_opt("std_ratio", s),
                "n_joins": int(s["n_joins"]) if s.get("n_joins") is not None else None,
                "calib_scalar": _float_opt("scalar", c),
                "daily_weekly_corr": _float_opt("daily_weekly_corr", c),
            }
        )

    t = client.get_table(table_id)
    loc = _bq_query_location(client, table_id)
    p, d, _ = table_id.split(".", 2)
    temp_id = f"{p}.{d}._smerge_{uuid.uuid4().hex[:12]}"
    try:
        load_staging_table_from_rows(client, temp_id, list(t.schema), rows)
    except Exception:
        try:
            client.delete_table(temp_id, not_found_ok=True)
        except Exception:
            pass
        raise
    try:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
        client.query(
            f"""
            CREATE OR REPLACE TABLE `{table_id}` AS
            SELECT * FROM `{table_id}` WHERE run_id != @run_id
            UNION ALL
            SELECT * FROM `{temp_id}`
            """,
            job_config=job_config,
            location=loc,
        ).result()
    finally:
        client.delete_table(temp_id, not_found_ok=True)
