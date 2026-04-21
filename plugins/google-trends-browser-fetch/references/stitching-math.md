# Stitching math: why median-ratio

Google Trends normalizes every query to its own 0-100 range. The max of whatever you selected becomes 100. This means:
- Two independent downloads of the same term over different date ranges are on **different scales**.
- Any chunk's Black-Friday spike-of-100 is not comparable to the neighboring chunk's 100 unless both spikes are actually the same.

To join chunks into a single consistent series, we need to rescale every chunk except the first so that chunks agree on their overlap region.

## Why median-ratio, not OLS

For each pair of consecutive chunks with overlap `O`, define:

```
ratio = median( chunk_prev[O] / chunk_curr[O] )
```

Then multiply `chunk_curr` (and all later chunks transitively) by that ratio so they sit in `chunk_prev`'s scale.

Alternatives considered and rejected:

| Approach | Problem |
|---|---|
| OLS linear fit `curr = a·prev + b` | Two free parameters overfit short overlaps; injects a baseline shift that isn't really there. |
| Mean ratio | Sensitive to outliers (a single Black-Friday spike dominates). |
| Max ratio | Extremely outlier-sensitive by construction. |
| L2-optimal single scalar | Same sensitivity as mean, plus it's scale-dependent. |

Median ratio is a single-parameter, scale-invariant, outlier-robust estimator. It's the right default when overlaps are short (~10-15 days) and occasional spikes are expected.

## Zero-handling

Trends reports integer 0-100 and `<1` for very low values. Zeros in a ratio denominator explode. Two standard fixes:

1. **Mask**: drop overlap days where either chunk is 0. Simple, correct, and is what `stitch_daily.py` does.
2. **Floor**: treat `<1` and 0 as 0.5. Keeps more overlap days but may bias the ratio if one chunk has many near-zero values.

For rarely-searched terms (daily values typically ≤ 5), many zeros are possible and the ratio becomes unstable. The script logs a warning when the ratio std dev exceeds the noise you'd expect from the chunk noise floor.

## Calibration step

Chain-stitching fixes relative scales but the whole series is still in chunk-0's arbitrary units. To put it on a known scale, download a single weekly reference covering the whole range and apply a final global scalar:

```
global_scalar = median( weekly_ref / resample_to_weekly(stitched_daily) )
calibrated = stitched_daily * global_scalar
```

Why re-resample the stitched daily to weekly before comparing? Because the weekly reference is actually "weekly means of the daily scale Google internally has." Comparing weekly-to-weekly is apples-to-apples; comparing weekly to raw daily values is not.

## Quality diagnostics

Two metrics to watch:

- **`std_ratio`** across joins: if chunks are telling a consistent story, the ratios shouldn't wander. For a well-chosen chunk width and clean signal, std < 0.10 is typical. Std > 0.15 means the chunks disagree enough that you should widen overlaps or look for a rendering/download error.
- **`daily_weekly_corr`**: the stitched daily should correlate with the weekly reference in the 0.5-0.8 range. Above 0.95 means your "daily" is effectively just smoothed weekly (no new info). Below 0.3 means you have a bug.

## When not to bother

If your model already uses weekly seasonality (e.g., BSTS `nseasons=7`), weekly-interpolated-to-daily adds ~nothing. The model can infer day-of-week effects from the target variable itself. Daily stitched is worth the effort only when:

1. The signal has genuine within-week variation that's exogenous to the target, and
2. The target variable might respond at daily granularity to that variation.

Classic case: search-interest in a category (shoes) as a proxy for category-wide demand, controlling for the brand's own activity. Within-week daily wiggles reflect real demand fluctuations.
