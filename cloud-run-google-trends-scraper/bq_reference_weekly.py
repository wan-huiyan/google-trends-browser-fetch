"""BigQuery helpers for the weekly Google Trends reference series.

The stitcher expects a *file* in Google’s multiTimeline shape (first column ``Week``,
``Day``, or ``Date``, plus one or more ``"term: (Region)"`` metric columns). Preamble
lines such as ``Category: All categories`` are skipped. Recommended pattern:

1. **Write**: after Playwright saves ``reference_weekly.csv``, call
   ``merge_weekly_csv_into_table`` to replace those weeks in BigQuery.
2. **Read**: before stitch, call ``materialize_table_to_stitch_csv`` to export
   rows to a temp CSV that ``stitch_daily.py`` can load unchanged.

Table id: **always** ``project.dataset.table`` (three dot-separated parts). You may pass
``dataset.table`` only if the BigQuery client has a default project (ADC / metadata).
A bare table name like ``reference_weekly`` is invalid for ``BQ_WEEKLY_TABLE``.
On Cloud Run, ``main.py`` can build the full id from ``PROJECT_ID`` + ``DATASET_ID`` +
``REFERENCE_WEEKLY_TABLE_ID`` when ``BQ_WEEKLY_TABLE`` is unset.

**Long table** (one row per ``(week_start, term)``; ``term`` matches
``stitch_quality`` / the bare term in the multiTimeline header)::

    week_start DATE,
    run_id STRING,
    ingested_at TIMESTAMP,
    term STRING,
    google_trends_value INT64

**Multi-term exports** (several ``"term: (Region)"`` columns) become one row per
term per week; the ``term`` string is taken from the header (text before ``:``).
"""

from __future__ import annotations

import csv
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from google.api_core.exceptions import NotFound
from google.cloud import bigquery


def _fully_qualified_table_id(raw: str, *, default_project: Optional[str]) -> str:
    """Normalize ``BQ_WEEKLY_TABLE`` / API args to ``project.dataset.table``."""
    t = raw.strip().strip("`").strip()
    if not t:
        raise ValueError("BigQuery table id is empty")
    if t.lower().startswith("gs://"):
        raise ValueError(
            f"BigQuery table id must be project.dataset.table, not a GCS URI (got {raw!r})"
        )
    parts = t.split(".")
    if len(parts) == 3:
        return t
    if len(parts) == 2:
        if not default_project:
            raise ValueError(
                f"BigQuery table id {raw!r} looks like dataset.table; use project.dataset.table "
                "or set Application Default Credentials with a default GCP project."
            )
        return f"{default_project}.{t}"
    raise ValueError(
        "BigQuery table id must be project.dataset.table (three parts: project, dataset, "
        f"table). For example my-gcp-project.analytics.reference_weekly. Got {raw!r}"
    )


def ensure_dataset_exists(client, fully_qualified_table_id: str) -> None:
    """Create the parent dataset if it does not exist (BigQuery has no table without a dataset)."""
    from google.cloud import bigquery

    parts = fully_qualified_table_id.split(".", 2)
    if len(parts) != 3:
        return
    project, dataset_id, _ = parts
    ref = f"{project}.{dataset_id}"
    try:
        client.get_dataset(ref)
    except NotFound:
        client.create_dataset(bigquery.Dataset(ref), exists_ok=True)
        print(f"Created BigQuery dataset `{ref}`", flush=True)


def _bq_query_location(client, fully_qualified_table_id: str) -> str:
    p, d, _ = fully_qualified_table_id.split(".", 2)
    return client.get_dataset(f"{p}.{d}").location


def load_staging_table_from_rows(
    client: bigquery.Client,
    temp_id: str,
    schema: list[bigquery.SchemaField],
    rows: list[dict[str, Any]],
    *,
    batch_size: int = 2000,
) -> None:
    """Create a temporary table and load rows with ``tabledata.insertAll`` (not file upload).

    The BigQuery *load* job (``load_table_from_dataframe`` / resumable upload) requires
    ``bigquery.tables.updateData`` on the destination, which stricter org policies can
    deny. Streaming insert uses a different path and is typically included in
    **BigQuery Data Editor** the same as queries.
    """
    if not rows:
        raise ValueError("load_staging_table_from_rows: rows must be non-empty")
    tbl = bigquery.Table(temp_id, schema=schema)
    client.create_table(tbl, exists_ok=False, timeout=120.0)
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        errors = client.insert_rows_json(temp_id, batch)
        if errors:
            try:
                client.delete_table(temp_id, not_found_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"insert_rows_json into {temp_id!r} failed: {errors[:5]!r}")


def snapshot_upsert_long_format(
    client: bigquery.Client,
    dest_table_id: str,
    temp_table_id: str,
    *,
    partition_field: str,
    key_date_field: str,
    key_term_field: str,
    location: str,
) -> None:
    """Rebuild ``dest`` by upserting on ``(key_date_field, key_term_field)``.

    Rows already in ``dest`` whose key appears in ``temp`` are dropped from the kept
    set and replaced by rows from ``temp``. All other rows are retained (append-only
    for new keys; update for keys present in this batch).

    Implemented as ``CREATE OR REPLACE … AS`` over a snapshot read of ``dest`` plus
    ``UNION ALL`` staging — no DML ``DELETE`` on the live table (streaming-buffer safe).
    """
    sql = f"""
    CREATE OR REPLACE TABLE `{dest_table_id}` PARTITION BY `{partition_field}` AS
    WITH sk AS (
      SELECT DISTINCT `{key_date_field}`, `{key_term_field}` FROM `{temp_table_id}`
    )
    SELECT T.*
    FROM `{dest_table_id}` AS T
    LEFT JOIN sk
      ON T.`{key_date_field}` = sk.`{key_date_field}`
      AND T.`{key_term_field}` = sk.`{key_term_field}`
    WHERE sk.`{key_date_field}` IS NULL
    UNION ALL
    SELECT * FROM `{temp_table_id}`
    """
    client.query(sql, location=location).result()


def _bare_term_from_trends_header(h: str) -> str:
    """Strip ``TERM: (Region)`` to ``TERM``; same as ``stitch_daily`` column names."""
    t = h.strip()
    if ":" in t:
        t = t.split(":", 1)[0]
    return t.strip()


def trends_export_column_header(term: str, *, region: Optional[str] = None) -> str:
    """Header cell like Google exports: ``term: (United Kingdom)``."""
    region = (region or os.environ.get("TRENDS_REGION_LABEL", "United Kingdom")).strip()
    return f"{term.strip()}: ({region})"


_DATE_COLUMN_NAMES = frozenset(
    {
        "week",
        "day",
        "date",
        "semana",
        "settimana",
        "woche",
        "semaine",
    }
)


def _normalize_first_header(cell: str) -> str:
    return cell.strip().lstrip("\ufeff").strip('"').lower()


def _parse_trends_date_cell(s: str) -> date:
    """First column: ISO date or Google ``YYYY-MM-DD - YYYY-MM-DD`` week range."""
    s = str(s).strip().strip('"')
    if not s:
        raise ValueError("empty date cell")
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s*-\s*\d{4}-\d{2}-\d{2}", s)
    if m:
        return date.fromisoformat(m.group(1))
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return date.fromisoformat(m.group(1))
    raise ValueError(f"Unrecognized date/week cell: {s!r}")


def _find_trends_table_header_in_file(path: Path, lines: list[str]) -> tuple[int, list[str]]:
    for i, raw_line in enumerate(lines):
        line = raw_line.strip().lstrip("\ufeff").strip()
        if not line or line.lower().startswith("category:") or line.startswith("#"):
            continue
        try:
            headers = next(csv.reader([raw_line.rstrip("\r\n")]))
        except Exception:
            continue
        if not headers:
            continue
        first = _normalize_first_header(headers[0])
        if first in _DATE_COLUMN_NAMES:
            return i, [h.strip() for h in headers]
    preview = "".join(lines[:12])[:800].replace("\n", "\\n")
    raise ValueError(
        f"No Week/Day/Date header row in {path} (file may be HTML, empty, or wrong export). "
        f"Start of file:\n{preview}"
    )


def _parse_google_week_csv(path: Path) -> tuple[list[tuple[date, tuple[float, ...]]], list[str]]:
    """Parse a Trends multiTimeline export (Week or Day + metric columns)."""
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        text = f.read()
    if not text.strip():
        raise ValueError(f"Empty file: {path}")
    stripped = text.lstrip().lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        raise ValueError(f"File looks like HTML, not CSV: {path}")
    lines = text.splitlines()
    header_idx, headers = _find_trends_table_header_in_file(path, lines)

    metric_headers = [h for h in headers[1:] if h.strip()]
    if not metric_headers:
        raise ValueError("No metric columns after date column")

    def cell(x: str) -> float:
        s = str(x).strip()
        if s == "<1":
            return 0.5
        if not s:
            return float("nan")
        return float(s)

    rdr = csv.reader(lines[header_idx + 1 :])
    rows: list[tuple[date, tuple[float, ...]]] = []
    for raw in rdr:
        if not raw or not str(raw[0]).strip():
            continue
        try:
            ds = _parse_trends_date_cell(str(raw[0]))
        except ValueError:
            continue
        vals = []
        for j in range(len(metric_headers)):
            vals.append(cell(raw[j + 1]) if len(raw) > j + 1 else float("nan"))
        rows.append((ds, tuple(vals)))
    return rows, metric_headers


def write_stitcher_weekly_csv(
    rows: Iterable[tuple[date, tuple[float, ...]]],
    out: Path,
    *,
    metric_headers: list[str],
) -> None:
    """Write CSV in the shape ``load_trends_csv`` accepts."""
    if not metric_headers:
        raise ValueError("metric_headers required")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        f.write("Category: All categories\n\n")
        f.write("Week," + ",".join(metric_headers) + "\n")
        for tup in rows:
            w, vals = tup[0], tup[1]
            if any(v != v for v in vals):
                continue
            parts = [w.isoformat()] + [str(int(round(v))) for v in vals]
            f.write(",".join(parts) + "\n")

def _weekly_bq_schema():
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("week_start", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("run_id", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("term", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("google_trends_value", "INT64", mode="NULLABLE"),
    ]


WEEKLY_REQUIRED_FIELDS = frozenset(f.name for f in _weekly_bq_schema())


def merge_weekly_csv_into_table(
    csv_path: Path,
    table_id: str,
    *,
    run_id: str,
) -> None:
    """Upsert weekly rows into BigQuery on ``(week_start, term)``.

    Loads this CSV batch into a temp table, then rebuilds the destination so existing
    rows with other keys are kept; keys in this batch get new ``google_trends_value`` /
    ``run_id`` / ``ingested_at``.
    """
    parsed, metric_headers = _parse_google_week_csv(csv_path)
    if not parsed:
        raise ValueError("no weekly rows parsed from CSV")
    term_labels = [_bare_term_from_trends_header(h) for h in metric_headers]

    client = bigquery.Client()
    table_id = _fully_qualified_table_id(table_id, default_project=client.project)
    _ensure_weekly_reference_table(client, table_id)

    loc = _bq_query_location(client, table_id)
    t = client.get_table(table_id)

    now = datetime.now(timezone.utc).isoformat()
    out_rows: list[dict] = []
    for w, vals in parsed:
        for j, v in enumerate(vals):
            if v != v:
                continue
            out_rows.append(
                {
                    "week_start": w.isoformat(),
                    "run_id": run_id,
                    "ingested_at": now,
                    "term": term_labels[j],
                    "google_trends_value": int(round(v)),
                }
            )

    if not out_rows:
        return

    p, d, _ = table_id.split(".", 2)
    temp_id = f"{p}.{d}._wmerge_{uuid.uuid4().hex[:12]}"
    try:
        load_staging_table_from_rows(client, temp_id, list(t.schema), out_rows)
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
            partition_field="week_start",
            key_date_field="week_start",
            key_term_field="term",
            location=loc,
        )
    finally:
        client.delete_table(temp_id, not_found_ok=True)


def _ensure_weekly_reference_table(
    client,
    table_id: str,
) -> None:
    """Create the long-format weekly table if missing; error on legacy wide schema."""
    from google.cloud import bigquery

    try:
        t = client.get_table(table_id)
    except NotFound:
        _create_weekly_table(client, table_id)
        return
    names = {f.name for f in t.schema}
    if names != WEEKLY_REQUIRED_FIELDS:
        raise ValueError(
            f"Table {table_id} has unexpected schema (expected {sorted(WEEKLY_REQUIRED_FIELDS)}). "
            "If this is a legacy wide weekly table, drop or recreate the table for the new "
            "long format (term + google_trends_value)."
        )


def _create_weekly_table(client, table_id: str) -> None:
    """Create the weekly reference table: one row per (week, term)."""
    from google.cloud import bigquery

    ensure_dataset_exists(client, table_id)
    table = bigquery.Table(table_id, schema=_weekly_bq_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="week_start",
    )
    table = client.create_table(table)
    print(f"Created table {table.project}.{table.dataset_id}.{table.table_id}", flush=True)


def _term_order_for_materialize() -> list[str]:
    """Order of terms for columns in the stitched reference CSV (must match BQ ``term`` values)."""
    from terms_input import parse_terms_from_env

    parts = parse_terms_from_env(os.environ)
    if not parts:
        raise ValueError(
            "Set TRENDS_TERMS or QUERY_TERM to match the ``term`` text stored in BigQuery, "
            "or pass materialize term_order= explicitly."
        )
    return parts


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def materialize_table_to_stitch_csv(
    table_id: str,
    out: Path,
    *,
    week_start_col: str = "week_start",
    term_order: Optional[list[str]] = None,
    stitch_metric_headers: Optional[list[str]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> None:
    """Query long-format weekly BQ and write a wide multiTimeline-style CSV for ``stitch_daily``."""
    from google.cloud import bigquery

    if term_order is None:
        term_order = _term_order_for_materialize()
    n = len(term_order)

    client = bigquery.Client()
    table_id = _fully_qualified_table_id(table_id, default_project=client.project)
    where = ""
    if start is not None and end is not None:
        where = (
            f"WHERE {week_start_col} BETWEEN DATE('{start.isoformat()}') "
            f"AND DATE('{end.isoformat()}')"
        )

    sql = f"""
    SELECT {week_start_col} AS week_start, term, google_trends_value
    FROM `{table_id}`
    {where}
    ORDER BY 1, 2
    """
    by_week: dict[date, dict[str, float]] = {}
    for r in client.query(sql).result():
        wk = _as_date(r["week_start"])
        te = (r["term"] or "").strip()
        gv = r["google_trends_value"]
        if gv is None:
            continue
        by_week.setdefault(wk, {})[te] = float(gv)

    rows_out: list[tuple[date, tuple[float, ...]]] = []
    for wk in sorted(by_week.keys()):
        tmap = by_week[wk]
        vals: list[float] = []
        for t in term_order:
            v = tmap.get(t)
            if v is None:
                vals.append(float("nan"))
            else:
                vals.append(v)
        rows_out.append((wk, tuple(vals)))

    if stitch_metric_headers is None:
        if n == 1:
            stitch_metric_headers = [trends_export_column_header(term_order[0])]
        else:
            stitch_metric_headers = [trends_export_column_header(t) for t in term_order]
    if len(stitch_metric_headers) != n:
        raise ValueError("stitch_metric_headers length must match term_order")
    write_stitcher_weekly_csv(rows_out, out, metric_headers=stitch_metric_headers)
