"""Plan overlapping Google Trends chunk date ranges.

Google Trends returns daily data only for ranges < 90 days. For longer windows,
download overlapping ~75-day chunks (with ~15-day overlaps for cross-normalization)
and stitch them with stitch_daily.py.

Usage:
    python plan_chunks.py --start 2024-09-29 --end 2026-03-15 \
        --chunk-days 75 --overlap-days 15 \
        --geo GB --terms nike,running shoes \
        [--out-dir data/trends_chunks]

Outputs a JSON array of {url, start, end, filename} objects to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote


def parse_iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def plan_chunks(
    start: date,
    end: date,
    chunk_days: int,
    overlap_days: int,
    geo: str,
    terms: str,
    out_dir: str,
    hl: str,
) -> list[dict]:
    if chunk_days <= overlap_days:
        raise ValueError("chunk_days must exceed overlap_days")
    if chunk_days >= 90:
        raise ValueError(
            "chunk_days must be <90 to force daily resolution from Google Trends"
        )

    step = chunk_days - overlap_days
    chunks: list[dict] = []
    i = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        start_s, end_s = cursor.isoformat(), chunk_end.isoformat()
        date_param = f"{start_s} {end_s}"
        url = (
            "https://trends.google.com/trends/explore"
            f"?date={quote(date_param)}"
            f"&geo={quote(geo)}"
            f"&q={quote(terms)}"
            f"&hl={quote(hl)}"
        )
        chunks.append(
            {
                "index": i,
                "start": start_s,
                "end": end_s,
                "url": url,
                "filename": f"{out_dir}/chunk_{i:02d}_{start_s}_{end_s}.csv",
            }
        )
        if chunk_end >= end:
            break
        cursor = cursor + timedelta(days=step)
        i += 1
    return chunks


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="ISO date, e.g. 2024-09-29")
    p.add_argument("--end", required=True, help="ISO date, e.g. 2026-03-15")
    p.add_argument("--chunk-days", type=int, default=75)
    p.add_argument("--overlap-days", type=int, default=15)
    p.add_argument("--geo", default="", help="ISO country code, e.g. GB. Empty = worldwide")
    p.add_argument("--terms", required=True, help="Comma-separated search terms (max 5)")
    p.add_argument("--out-dir", default="data/trends_chunks")
    p.add_argument("--hl", default="en-GB", help="UI language hl param")
    args = p.parse_args()

    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    if len(terms) > 5:
        print("ERROR: Google Trends allows at most 5 comparison terms", file=sys.stderr)
        return 1

    chunks = plan_chunks(
        start=parse_iso(args.start),
        end=parse_iso(args.end),
        chunk_days=args.chunk_days,
        overlap_days=args.overlap_days,
        geo=args.geo,
        terms=",".join(terms),
        out_dir=args.out_dir.rstrip("/"),
        hl=args.hl,
    )
    json.dump(chunks, sys.stdout, indent=2)
    sys.stdout.write("\n")
    print(
        f"\n[plan_chunks] {len(chunks)} chunks "
        f"({args.chunk_days}d with {args.overlap_days}d overlap) "
        f"from {args.start} to {args.end}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
