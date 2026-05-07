"""BigQuery sink for Google Trends related queries (rising and top).

Target table: ``google_trends.05_related_queries``
Schema: run_date DATE, term STRING, related_query STRING, query_type STRING,
        value_raw STRING, value_numeric FLOAT64, value_is_breakout BOOL,
        run_id STRING, ingested_at TIMESTAMP.

Partitioned by ``run_date``, clustered by ``term``, ``query_type``.

Upsert key: ``(run_date, term, related_query, query_type)`` — idempotent;
re-running the scraper for the same date replaces existing rows for that
(date, term) combination without touching other dates.
"""

from __future__ import annotations

import uuid
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import bigquery


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("run_date", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("term", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("related_query", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("query_type", "STRING", mode="REQUIRED"),   # "rising" | "top"
        bigquery.SchemaField("value_raw", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("value_numeric", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("value_is_breakout", "BOOL", mode="NULLABLE"),
        bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
    ]


# ---------------------------------------------------------------------------
# Table management
# ---------------------------------------------------------------------------

def _fully_qualified(table_id: str, default_project: str) -> str:
    parts = table_id.strip().split(".")
    if len(parts) == 3:
        return table_id
    if len(parts) == 2:
        return f"{default_project}.{table_id}"
    return table_id


def _ensure_dataset(client: bigquery.Client, table_id: str) -> None:
    parts = table_id.split(".")
    if len(parts) < 2:
        return
    dataset_ref = f"{parts[0]}.{parts[1]}"
    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        ds = bigquery.Dataset(dataset_ref)
        ds.location = "europe-west2"
        client.create_dataset(ds, exists_ok=True)
        print(f"Created dataset {dataset_ref}", flush=True)


def ensure_table(client: bigquery.Client, table_id: str) -> None:
    """Create the related queries table if it does not already exist."""
    try:
        client.get_table(table_id)
        return
    except NotFound:
        pass

    _ensure_dataset(client, table_id)
    table = bigquery.Table(table_id, schema=_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="run_date",
    )
    table.clustering_fields = ["term", "query_type"]
    client.create_table(table)
    print(f"Created BigQuery table {table_id}", flush=True)


# ---------------------------------------------------------------------------
# Dynamic term resolution from existing trends_daily table
# ---------------------------------------------------------------------------

def get_terms_from_bq(daily_table_id: str) -> list[str]:
    """Return distinct terms from ``trends_daily`` in alphabetical order.

    Falls back gracefully if the table doesn't exist (returns empty list).
    """
    client = bigquery.Client()
    try:
        client.get_table(daily_table_id)
    except NotFound:
        print(f"  trends_daily table {daily_table_id!r} not found; no terms", flush=True)
        return []

    sql = f"SELECT DISTINCT term FROM `{daily_table_id}` ORDER BY term"
    rows = list(client.query(sql).result())
    terms = [r.term for r in rows if r.term]
    print(f"  Found {len(terms)} terms in {daily_table_id}: {terms}", flush=True)
    return terms


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _load_staging(
    client: bigquery.Client,
    staging_id: str,
    rows: list[dict[str, Any]],
) -> None:
    schema = _schema()
    staging_table = bigquery.Table(staging_id, schema=schema)
    client.create_table(staging_table, exists_ok=True)
    errors = client.insert_rows_json(staging_id, rows)
    if errors:
        raise RuntimeError(f"Staging insert errors: {errors[:3]}")


def load_related_queries_to_bq(
    rows: list[dict[str, Any]],
    table_id: str,
    *,
    run_id: str,
) -> None:
    """Upsert ``rows`` into ``table_id`` on ``(run_date, term, related_query, query_type)``.

    Replaces existing rows for the same (run_date, term, query_type) combinations
    present in this batch; all other rows in the table are untouched.
    """
    if not rows:
        print("No related query rows to load.", flush=True)
        return

    client = bigquery.Client()
    table_id = _fully_qualified(table_id, client.project)
    ensure_table(client, table_id)

    # Determine location from target table
    try:
        t = client.get_table(table_id)
        location = t.location or "europe-west2"
    except Exception:
        location = "europe-west2"

    parts = table_id.split(".", 2)
    staging_id = f"{parts[0]}.{parts[1]}._rqmerge_{uuid.uuid4().hex[:12]}"

    try:
        _load_staging(client, staging_id, rows)

        # Build set of (run_date, term, query_type) combos in this batch
        combos = list({(r["run_date"], r["term"], r["query_type"]) for r in rows})
        # Deduplicate via MERGE on the four-column key
        merge_sql = f"""
            MERGE `{table_id}` T
            USING `{staging_id}` S
            ON  T.run_date       = S.run_date
            AND T.term           = S.term
            AND T.related_query  = S.related_query
            AND T.query_type     = S.query_type
            WHEN MATCHED THEN
              UPDATE SET
                value_raw        = S.value_raw,
                value_numeric    = S.value_numeric,
                value_is_breakout = S.value_is_breakout,
                run_id           = S.run_id,
                ingested_at      = S.ingested_at
            WHEN NOT MATCHED THEN
              INSERT ROW
        """
        client.query(merge_sql, location=location).result()
        print(
            f"Upserted {len(rows)} rows into {table_id} "
            f"({len(set(r['term'] for r in rows))} terms, run_id={run_id})",
            flush=True,
        )
    finally:
        client.delete_table(staging_id, not_found_ok=True)
