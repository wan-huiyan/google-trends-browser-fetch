"""Stitch overlapping Google Trends daily chunks into a single calibrated series.

Algorithm (see references/stitching-math.md for the why):
  1. Load each chunk CSV; each has its own 0-100 normalization per term.
  2. For consecutive chunks i, i+1: compute median(aligned[i] / raw[i+1]) on
     their overlap window, per term. Multiply raw chunk i+1 by that ratio so
     it matches aligned[i] (which is already in chunk[0]'s scale). Chain joins;
     each step uses one ratio, not a cumulative product applied to raw values.
  3. Concatenate aligned chunks, taking the mean on overlap days.
  4. Load the full-range weekly reference (separate single download covering
     the whole period). Aggregate stitched daily and the reference to **ISO weeks**
     (week starts Monday, same calendar-week notion as ``explore_url_range`` date spans).
     Use only **complete** ISO weeks (seven daily rows in the stitched series). Compute
     median(weekly_ref / stitched_weekly) as a single global calibration scalar per term.
     Apply it.
  5. Optionally, renormalize each term to [0, 100] (``--renormalize-0-100``; off by
     default). (Weekly agreement is computed before this step; see
     ``renormalization`` in the quality JSON when the option is on.)
  6. Report per-term stitching std (stability across joins) and daily-vs-weekly
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
                (
                    c
                    for c in df.columns
                    if c.strip().lower() in ("day", "week", "date", "time")
                ),
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
            ratio_log[t].append(ratio)
            # Align curr to prev's scale using this join's ratio only. `prev` is
            # already chained to chunk[0]; ratios are median(prev/curr_raw) on
            # overlap — do not multiply by cumulative products of prior ratios.
            curr[t] = curr[t] * ratio
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


def _iso_week_monday_from_components(iso_year: int, iso_week: int) -> pd.Timestamp:
    """Monday 00:00 of the given ISO year / ISO week (matches ISO calendar weeks)."""
    return pd.Timestamp.fromisocalendar(int(iso_year), int(iso_week), 1)


def _daily_mean_by_iso_week(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Mean daily → one row per ISO week; index = Monday. ``counts`` = rows per week."""
    if daily.empty:
        return daily, pd.Series(dtype=int)
    ic = daily.index.isocalendar()
    key = pd.MultiIndex.from_arrays([ic.year.to_numpy(), ic.week.to_numpy()], names=["iy", "iw"])
    df = daily.copy()
    df.index = key
    means = df.groupby(level=[0, 1]).mean()
    counts = df.groupby(level=[0, 1]).size()
    mondays = pd.DatetimeIndex(
        [_iso_week_monday_from_components(int(y), int(w)) for y, w in means.index],
        name="iso_week_monday",
    )
    means.index = mondays
    counts.index = mondays
    return means.sort_index(), counts.sort_index()


def _weekly_ref_to_iso_weekly(weekly_ref: pd.DataFrame) -> pd.DataFrame:
    """Reference weekly CSV → one row per ISO week (Monday index); duplicate weeks averaged."""
    if weekly_ref.empty:
        return weekly_ref
    ic = weekly_ref.index.isocalendar()
    key = pd.MultiIndex.from_arrays([ic.year.to_numpy(), ic.week.to_numpy()])
    wr = weekly_ref.copy()
    wr.index = key
    out = wr.groupby(level=[0, 1]).mean(numeric_only=True)
    mondays = pd.DatetimeIndex(
        [_iso_week_monday_from_components(int(y), int(w)) for y, w in out.index],
        name="iso_week_monday",
    )
    out.index = mondays
    return out.sort_index()


def calibrate_to_weekly(daily: pd.DataFrame, weekly_ref: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Rescale stitched daily so ISO-week means match the weekly reference (complete weeks only)."""
    terms = [t for t in daily.columns if t in weekly_ref.columns]
    if not terms:
        raise ValueError(
            "No shared term columns between stitched daily and weekly reference. "
            f"Daily: {list(daily.columns)}, Weekly: {list(weekly_ref.columns)}"
        )
    stitched_weekly, n_days = _daily_mean_by_iso_week(daily)
    ref = _weekly_ref_to_iso_weekly(weekly_ref)

    corr = {}
    calibrated = daily.copy()
    for t in terms:
        common = stitched_weekly.index.intersection(ref.index)
        a = stitched_weekly.loc[common, t]
        b = ref.loc[common, t]
        nd = n_days.reindex(common)
        mask = (nd == 7) & (a > 0) & (b > 0) & a.notna() & b.notna()
        if mask.sum() == 0:
            print(
                f"WARN: term '{t}' has no valid calibration window (complete ISO weeks + ref); skipping",
                file=sys.stderr,
            )
            corr[t] = {"scalar": 1.0, "daily_weekly_corr": float("nan"), "n_weeks_used": 0}
            continue
        scalar = float(np.median(b[mask] / a[mask]))
        calibrated[t] = calibrated[t] * scalar
        recalc_weekly, _ = _daily_mean_by_iso_week(calibrated[[t]])
        recalc = recalc_weekly.reindex(common)[t]
        corr[t] = {
            "scalar": scalar,
            "daily_weekly_corr": float(recalc[mask].corr(b[mask])),
            "n_weeks_used": int(mask.sum()),
        }
    return calibrated, corr


def renormalize_to_0_100(daily: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Affine map each term column to [0, 100] using the sample min/max of finite values.

    Stitched + calibrated values can sit outside 0–100 because of ratio products and
    weekly matching; this is a *display-scale* renormalization that does not re-align
    to the weekly reference (the calibration metrics in ``stitch_quality.json`` are
    computed *before* this step).

    Constant (or all-NaN) non-zero columns: filled with 50.0; all-zero with 0.0.
    """
    out = daily.copy()
    meta: dict[str, dict] = {}
    for c in out.columns:
        s = out[c]
        s_num = pd.to_numeric(s, errors="coerce")
        valid = s_num.replace([np.inf, -np.inf], np.nan).dropna()
        if valid.empty:
            meta[c] = {"flat": True, "reason": "all_nan_or_non_numeric"}
            out[c] = s_num
            continue
        vmin, vmax = float(valid.min()), float(valid.max())
        if vmax > vmin:
            out[c] = (s_num - vmin) / (vmax - vmin) * 100.0
            meta[c] = {"min_before": vmin, "max_before": vmax, "flat": False}
        else:
            if vmin == 0.0 and vmax == 0.0:
                out[c] = 0.0
            else:
                out[c] = 50.0
            meta[c] = {
                "flat": True,
                "value_before": vmin,
            }
    return out, meta


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
    p.add_argument(
        "--renormalize-0-100",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="After calibration, map each term min→0 and max→100 (default: off; use --renormalize-0-100 to enable).",
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

    renorm_meta: dict = {}
    final_daily = calibrated
    if args.renormalize_0_100:
        final_daily, renorm_meta = renormalize_to_0_100(calibrated)

    final_daily.to_csv(args.out)

    quality: dict = {"stitching": stitch_quality, "calibration": calib_quality}
    if renorm_meta:
        quality["renormalization"] = renorm_meta
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
        nw = q.get("n_weeks_used")
        extra = f", complete_ISO_weeks={nw}" if nw is not None else ""
        print(
            f"  {t}: scalar={q['scalar']:.3f}, "
            f"daily-weekly r={q['daily_weekly_corr']:.3f}{extra}",
            file=sys.stderr,
        )
    if renorm_meta:
        print("\n=== Renormalization to [0, 100] (per-term min→0, max→100) ===", file=sys.stderr)
        for t, m in renorm_meta.items():
            if m.get("flat"):
                print(f"  {t}: {m}", file=sys.stderr)
            else:
                print(
                    f"  {t}: range [{m['min_before']:.4g}, {m['max_before']:.4g}] → [0, 100]",
                    file=sys.stderr,
                )
    print(f"\n→ wrote {args.out} ({len(final_daily)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())