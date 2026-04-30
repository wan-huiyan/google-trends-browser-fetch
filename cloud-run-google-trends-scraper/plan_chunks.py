"""Build ``chunks.json`` for Google Trends daily chunk downloads + stitch.

Each chunk is an inclusive ``[start, end]`` date range. The next chunk starts
``chunk_days - overlap_days`` days after the previous chunk start so adjacent
chunks share ``overlap_days`` days (required for ``stitch_daily``).

Exactly **one** query string per chunk URL (Google normalisation is per-URL; multi-term
compare is not used here). Pass ``--terms`` with that single term (same term ``main.py``
uses when invoking Playwright for that pipeline step).

Example::

    python plan_chunks.py --start 2024-09-01 --end 2026-03-31 \\
        --terms "brake pads" --output chunks.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _explore_url(start: date, end: date, geo: str, term: str, hl: str) -> str:
    date_param = f"{start.isoformat()} {end.isoformat()}"
    return (
        "https://trends.google.com/trends/explore"
        f"?date={quote(date_param)}&geo={quote(geo)}&q={quote(term)}&hl={quote(hl)}"
    )


def plan_chunks(
    *,
    start: date,
    end: date,
    chunk_days: int,
    overlap_days: int,
    geo: str,
    term: str,
    hl: str,
    chunks_dir_name: str,
) -> list[dict]:
    if end < start:
        raise ValueError("end must be on or after start")
    if chunk_days < 10:
        raise ValueError("chunk_days too small")
    if overlap_days < 1 or overlap_days >= chunk_days:
        raise ValueError("overlap_days must be in [1, chunk_days-1]")
    if not (term or "").strip():
        raise ValueError("term must be non-empty")

    q = term.strip()

    step = chunk_days - overlap_days
    out: list[dict] = []
    cur = start
    idx = 0
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        fn = f"chunk_{idx:02d}_{cur.isoformat()}_{chunk_end.isoformat()}.csv"
        out.append(
            {
                "index": idx,
                "start": cur.isoformat(),
                "end": chunk_end.isoformat(),
                "url": _explore_url(cur, chunk_end, geo, q, hl),
                "filename": str(Path(chunks_dir_name) / fn),
            }
        )
        if chunk_end >= end:
            break
        cur = cur + timedelta(days=step)
        idx += 1
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="range start YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="range end YYYY-MM-DD (inclusive)")
    p.add_argument("--chunk-days", type=int, default=75, help="length of each chunk in days (inclusive)")
    p.add_argument("--overlap-days", type=int, default=15, help="overlap between consecutive chunks")
    p.add_argument("--geo", default="GB", help="Trends geo code")
    p.add_argument("--terms", default="brake pads", help="single Trends query string for chunk URLs")
    p.add_argument("--hl", default="en-GB", help="language / locale")
    p.add_argument(
        "--chunks-dir",
        default="chunks",
        help="relative ``filename`` prefix inside chunks.json (under TRENDS_DATA_DIR)",
    )
    p.add_argument("--output", "-o", type=Path, required=True, help="write chunks.json here")
    args = p.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    try:
        plan = plan_chunks(
            start=start,
            end=end,
            chunk_days=args.chunk_days,
            overlap_days=args.overlap_days,
            geo=args.geo,
            term=args.terms,
            hl=args.hl,
            chunks_dir_name=args.chunks_dir.strip().strip("/") or "chunks",
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2))
    print(f"Wrote {len(plan)} chunks → {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
