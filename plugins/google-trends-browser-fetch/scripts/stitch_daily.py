"""Stitch overlapping Google Trends daily chunks into a single calibrated series.

Algorithm (see references/stitching-math.md for the why):
  1. Load each chunk CSV; each has its own 0-100 normalization per term.
  2. For consecutive chunks i, i+1: compute median(chunk[i+1] / chunk[i]) on
     their overlap window, per term. That ratio is how much to divide chunk[i+1]
     by to align its scale to chunk[i]. Compose these ratios cumulatively so
     all chunks live in chunk[0]'s scale.
  3. Concatenate aligned chunks, taking the mean on overlap days.
  4. Load the full-range weekly reference (separate single download covering
     the whole period). Compute weekly aggregates from the stitched daily and
     use median(weekly_ref / stitched_weekly) as a single global calibration
     scalar per term. Apply it.
  5. Report per-term stitching std (stability across joins) and daily-vs-weekly
     correlation (should be 0.5-0.8 for meaningful daily signal).

Usage:
    python stitch_daily.py --chunks chunks.json \
        --reference-weekly reference_weekly.csv \
        --out trends_daily_stitched.csv

Each chunk CSV is the raw Google Trends "multiTimeline" export:
  - header rows (2-3 lines)
  - then "Day,TERM1: (United Kingdom),TERM2: (United Kingdom),..."
  - one row per day
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load_trends_csv(path: str) -> pd.DataFrame:
    """Load a Google Trends multiTimeline CSV; return DataFrame indexed by date.

    Handles the 1-3 header lines Google prepends (e.g., "Category: All categories").
    Columns are renamed to bare terms (strips "TERM: (Country)" suffix).
    """
    for skip in (0, 1, 2, 3):
        try:
            df = pd.read_csv(path, skiprows=skip)
            date_col = next(
                (c for c in df.columns if c.strip().lower() in ("day", "week", "date")),
                None,
            )
            if date_col is None:
                continue
            df = df.rename(columns={date_col: "date"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date")
            # "<1" values → numeric (treat as 0.5)
            for col in df.columns:
                df[col] = (
                    df[col]
                    .astype(str)
                    .str.replace("<1", "0.5", regex=False)
                    .pipe(pd.to_numeric, errors="coerce")
                )
            # strip "TERM: (Country)" → "TERM"
            df.columns = [c.split(":")[0].strip() for c in df.columns]
            return df
        except Exception:
            continue
    raise ValueError(f"Could not parse Trends CSV: {path}")


def stitch(chunks: list[pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    """Chain-align chunks via median-ratio on overlaps; return stitched frame + quality."""
    if not chunks:
        raise ValueError("no chunks provided")
    terms = list(chunks[0].columns)

    scale = {t: 1.0 for t in terms}
    ratio_log: dict[str, list[float]] = {t: [] for t in terms}
    aligned = [chunks[0].copy()]

    for i in range(1, len(chunks)):
        prev, curr = aligned[-1], chunks[i].copy()
        overlap = prev.index.intersection(curr.index)
        if len(overlap) < 3:
            print(
                f"WARN: chunks {i-1}/{i} overlap is only {len(overlap)} days — "
                "consider wider overlap in plan_chunks.py",
                file=sys.stderr,
            )
        for t in terms:
            a, b = prev.loc[overlap, t], curr.loc[overlap, t]
            mask = (a > 0) & (b > 0) & a.notna() & b.notna()
            if mask.sum() == 0:
                ratio = 1.0
                print(f"WARN: term '{t}' chunk {i} has no valid overlap; ratio=1", file=sys.stderr)
            else:
                ratio = float(np.median(a[mask] / b[mask]))
            scale[t] *= ratio
            ratio_log[t].append(ratio)
            curr[t] = curr[t] * scale[t]
        aligned.append(curr)

    merged = pd.concat(aligned).groupby(level=0).mean().sort_index()
    quality = {
        t: {
            "median_ratio": float(np.median(ratio_log[t])) if ratio_log[t] else 1.0,
            "std_ratio": float(np.std(ratio_log[t])) if ratio_log[t] else 0.0,
            "n_joins": len(ratio_log[t]),
        }
        for t in terms
    }
    return merged, quality


def calibrate_to_weekly(daily: pd.DataFrame, weekly_ref: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Rescale stitched daily so its weekly means match the weekly reference."""
    terms = [t for t in daily.columns if t in weekly_ref.columns]
    if not terms:
        raise ValueError(
            "No shared term columns between stitched daily and weekly reference. "
            f"Daily: {list(daily.columns)}, Weekly: {list(weekly_ref.columns)}"
        )
    stitched_weekly = daily.resample("W").mean()
    ref = weekly_ref.resample("W").mean() if not weekly_ref.index.freq else weekly_ref
    corr = {}
    calibrated = daily.copy()
    for t in terms:
        common = stitched_weekly.index.intersection(ref.index)
        a, b = stitched_weekly.loc[common, t], ref.loc[common, t]
        mask = (a > 0) & (b > 0) & a.notna() & b.notna()
        if mask.sum() == 0:
            print(f"WARN: term '{t}' has no valid calibration window; skipping", file=sys.stderr)
            corr[t] = {"scalar": 1.0, "daily_weekly_corr": float("nan")}
            continue
        scalar = float(np.median(b[mask] / a[mask]))
        calibrated[t] = calibrated[t] * scalar
        # recompute correlation after calibration
        recalc = calibrated[t].resample("W").mean().loc[common]
        corr[t] = {
            "scalar": scalar,
            "daily_weekly_corr": float(recalc[mask].corr(b[mask])),
        }
    return calibrated, corr


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--chunks", required=True, help="chunks.json from plan_chunks.py")
    p.add_argument("--reference-weekly", required=True, help="full-range weekly CSV")
    p.add_argument("--out", required=True, help="output stitched daily CSV")
    p.add_argument(
        "--quality-out",
        default=None,
        help="optional path to write stitching quality metrics as JSON",
    )
    args = p.parse_args()

    chunks_plan = json.loads(Path(args.chunks).read_text())
    dfs = []
    for c in chunks_plan:
        path = c["filename"] if "filename" in c else c["path"]
        if not Path(path).exists():
            print(f"ERROR: missing chunk file {path}", file=sys.stderr)
            return 2
        dfs.append(load_trends_csv(path))

    stitched, stitch_quality = stitch(dfs)
    weekly_ref = load_trends_csv(args.reference_weekly)
    calibrated, calib_quality = calibrate_to_weekly(stitched, weekly_ref)

    calibrated.to_csv(args.out)

    quality = {"stitching": stitch_quality, "calibration": calib_quality}
    if args.quality_out:
        Path(args.quality_out).write_text(json.dumps(quality, indent=2))

    print("\n=== Stitching quality ===", file=sys.stderr)
    for t, q in stitch_quality.items():
        print(
            f"  {t}: median_ratio={q['median_ratio']:.3f}, "
            f"std={q['std_ratio']:.3f}, joins={q['n_joins']}",
            file=sys.stderr,
        )
    print("\n=== Calibration quality ===", file=sys.stderr)
    for t, q in calib_quality.items():
        print(
            f"  {t}: scalar={q['scalar']:.3f}, "
            f"daily-weekly r={q['daily_weekly_corr']:.3f}",
            file=sys.stderr,
        )
    print(f"\n→ wrote {args.out} ({len(calibrated)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
