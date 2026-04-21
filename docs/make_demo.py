"""Generate a demo image showing the chunking + stitching problem and fix.

Synthetic data — no client info. Three panels:
1. Raw chunks as downloaded: each independently normalized to 0-100 (different scales)
2. Naïve concat: stair-step discontinuities where chunks join
3. Stitched + calibrated: continuous daily series with weekly reference overlaid
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.grid": True,
    "grid.alpha": 0.25,
    "axes.spines.top": False,
    "axes.spines.right": False,
})

np.random.seed(42)

# 1) "True" daily search interest: trend + weekly seasonality + a holiday spike
days = pd.date_range("2024-09-29", "2025-04-15", freq="D")
n = len(days)
t = np.arange(n)
base = 30 + 10 * np.sin(2 * np.pi * t / 365)              # annual seasonality
weekly = 4 * np.sin(2 * np.pi * t / 7)                    # day-of-week effect
noise = np.random.normal(0, 2.0, n)
spike = np.zeros(n)
# Black Friday–style spike around day 60
spike_center = 60
spike += 25 * np.exp(-0.5 * ((t - spike_center) / 3) ** 2)
# Boxing Day spike around day 90
spike += 18 * np.exp(-0.5 * ((t - 90) / 3) ** 2)
true_daily = np.clip(base + weekly + spike + noise, 1, None)
true = pd.Series(true_daily, index=days, name="true")

# 2) Simulate Google Trends chunking. Each chunk gets rescaled so its own max = 100.
chunk_ranges = [
    ("2024-09-29", "2024-12-12"),
    ("2024-11-28", "2025-02-10"),
    ("2025-01-27", "2025-04-15"),
]
chunks = []
for start, end in chunk_ranges:
    c = true.loc[start:end].copy()
    scale = 100.0 / c.max()
    chunks.append(c * scale)

# 3) Stitch: chain median-ratio on overlaps
def stitch(chunks):
    aligned = [chunks[0].copy()]
    cumulative = 1.0
    for i in range(1, len(chunks)):
        prev, curr = aligned[-1], chunks[i].copy()
        overlap = prev.index.intersection(curr.index)
        ratio = float(np.median(prev.loc[overlap] / curr.loc[overlap]))
        cumulative *= ratio
        aligned.append(chunks[i] * cumulative)
    merged = pd.concat(aligned).groupby(level=0).mean().sort_index()
    return merged

stitched = stitch(chunks)

# 4) Simulate a weekly reference download (independent 0-100 normalization of full range)
weekly_ref = true.resample("W").mean()
weekly_ref = weekly_ref * (100.0 / weekly_ref.max())

# Calibrate stitched to weekly reference
stitched_weekly = stitched.resample("W").mean()
common = stitched_weekly.index.intersection(weekly_ref.index)
cal_scalar = float(np.median(weekly_ref.loc[common] / stitched_weekly.loc[common]))
calibrated = stitched * cal_scalar

# 5) Also build naïve concat (no rescaling) for the "bad" panel
naive = pd.concat(chunks).groupby(level=0).last().sort_index()

# Plot ---------------------------------------------------------------
fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)

colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

# Panel 1: raw chunks as downloaded
ax = axes[0]
for i, c in enumerate(chunks):
    ax.plot(c.index, c.values, color=colors[i], lw=1.2, label=f"Chunk {i+1}")
    # Mark the boundaries
    ax.axvline(c.index[0], color=colors[i], alpha=0.15, lw=0.8)
    ax.axvline(c.index[-1], color=colors[i], alpha=0.15, lw=0.8)
ax.set_title(
    "Raw chunks as downloaded from Google Trends\n"
    "Each chunk is independently normalized 0–100 — same date, different values across chunks",
    fontsize=11, loc="left",
)
ax.set_ylabel("Trends index\n(per-chunk scale)")
ax.legend(loc="upper right", frameon=False, ncol=3, fontsize=9)

# Panel 2: naive concat
ax = axes[1]
ax.plot(naive.index, naive.values, color="#d62728", lw=1.2)
for c in chunks[1:]:
    ax.axvline(c.index[0], color="k", alpha=0.3, lw=0.8, ls="--")
ax.set_title(
    "Naïve concatenation — stair-step discontinuities at chunk joins\n"
    "(wrong: would inject artificial jumps into any downstream model)",
    fontsize=11, loc="left",
)
ax.set_ylabel("Trends index\n(mixed scales)")

# Panel 3: stitched + calibrated, with weekly reference
ax = axes[2]
ax.plot(calibrated.index, calibrated.values, color="#2ca02c", lw=1.2, label="Stitched daily (calibrated)")
ax.plot(weekly_ref.index, weekly_ref.values, color="k", lw=1.5, alpha=0.55, label="Weekly reference")
ax.set_title(
    "Stitched + globally calibrated via median-ratio algorithm\n"
    "Continuous daily series; weekly reference overlay anchors the absolute scale",
    fontsize=11, loc="left",
)
ax.set_ylabel("Trends index\n(calibrated scale)")
ax.set_xlabel("Date")
ax.legend(loc="upper right", frameon=False, fontsize=9)

plt.suptitle(
    "google-trends-browser-fetch — overlapping chunks → stitched daily series",
    fontsize=13, fontweight="bold", y=0.995,
)
plt.tight_layout(rect=[0, 0, 1, 0.98])

out = "/Users/huiyanwan/.claude/skills/google-trends-browser-fetch/assets/demo-stitching.png"
import os
os.makedirs(os.path.dirname(out), exist_ok=True)
plt.savefig(out, dpi=150, bbox_inches="tight")
print(f"wrote {out}")
