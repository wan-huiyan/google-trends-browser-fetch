"""Generate a client-facing demo image for the chunking + stitching story.

Synthetic data (no client info). Three panels, soft palette, annotation callouts.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import rcParams

# ---------- Palette (warm, soft, client-facing) ----------
BG       = "#FBF8F3"   # off-white / warm paper
INK      = "#2D3142"   # soft dark navy (primary text)
SUBTLE   = "#6B6F80"   # secondary text
GRID     = "#E8E2D8"   # barely-there grid
CHUNK_A  = "#4A90A4"   # dusty teal
CHUNK_B  = "#E8955F"   # warm coral
CHUNK_C  = "#7BA05B"   # sage green
BAD      = "#D96B6B"   # muted red for "wrong"
GOOD     = "#5B9A78"   # clean green for "right"
REF      = "#3F4458"   # weekly reference

rcParams.update({
    "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 10,
    "text.color": INK,
    "axes.edgecolor": GRID,
    "axes.labelcolor": SUBTLE,
    "axes.labelsize": 10,
    "axes.titleweight": "semibold",
    "axes.titlesize": 12,
    "axes.titlecolor": INK,
    "axes.titlelocation": "left",
    "axes.titlepad": 14,
    "xtick.color": SUBTLE,
    "ytick.color": SUBTLE,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.spines.bottom": True,
    "axes.linewidth": 0.8,
    "legend.frameon": False,
    "legend.fontsize": 9,
    "figure.facecolor": BG,
    "axes.facecolor": BG,
    "savefig.facecolor": BG,
})

# ---------- Synthetic "true" series ----------
np.random.seed(42)
days = pd.date_range("2024-09-29", "2025-04-15", freq="D")
n = len(days)
t = np.arange(n)
base = 30 + 10 * np.sin(2 * np.pi * t / 365)
weekly = 4 * np.sin(2 * np.pi * t / 7)
noise = np.random.normal(0, 2.0, n)
spike = 25 * np.exp(-0.5 * ((t - 60) / 3) ** 2) + 18 * np.exp(-0.5 * ((t - 90) / 3) ** 2)
true = pd.Series(np.clip(base + weekly + spike + noise, 1, None), index=days)

# ---------- Chunking (each chunk independently normalised to 0-100) ----------
chunk_ranges = [
    ("2024-09-29", "2024-12-12"),
    ("2024-11-28", "2025-02-10"),
    ("2025-01-27", "2025-04-15"),
]
chunks = [true.loc[s:e] * (100.0 / true.loc[s:e].max()) for s, e in chunk_ranges]

def stitch(chunks):
    aligned = [chunks[0].copy()]
    cumulative = 1.0
    for i in range(1, len(chunks)):
        prev = aligned[-1]
        overlap = prev.index.intersection(chunks[i].index)
        ratio = float(np.median(prev.loc[overlap] / chunks[i].loc[overlap]))
        cumulative *= ratio
        aligned.append(chunks[i] * cumulative)
    return pd.concat(aligned).groupby(level=0).mean().sort_index()

stitched = stitch(chunks)
weekly_ref = true.resample("W").mean() * (100.0 / (true.resample("W").mean().max()))
sw = stitched.resample("W").mean()
common = sw.index.intersection(weekly_ref.index)
scalar = float(np.median(weekly_ref.loc[common] / sw.loc[common]))
calibrated = stitched * scalar
naive = pd.concat(chunks).groupby(level=0).last().sort_index()

# ---------- Figure ----------
fig = plt.figure(figsize=(11.5, 11.5), facecolor=BG)
gs = fig.add_gridspec(
    3, 1, hspace=0.9,
    left=0.09, right=0.96, top=0.83, bottom=0.06,
)
axes = [fig.add_subplot(gs[i]) for i in range(3)]

# ---------- Global title bar ----------
fig.text(
    0.09, 0.955,
    "From noisy chunks to a clean daily signal",
    fontsize=20, fontweight="bold", color=INK, ha="left", va="center",
)
fig.text(
    0.09, 0.920,
    "How google-trends-browser-fetch joins overlapping Google Trends downloads into a continuous series",
    fontsize=11, color=SUBTLE, ha="left", va="center",
)

colors = [CHUNK_A, CHUNK_B, CHUNK_C]

def style_axis(ax):
    ax.tick_params(axis="both", length=0)
    ax.set_axisbelow(True)

def add_step_label(ax, step_num, label, color):
    """Step number + label as panel header, above the title."""
    ax.text(
        0.0, 1.28, f"STEP {step_num}",
        transform=ax.transAxes,
        fontsize=9, fontweight="bold", color=color,
        ha="left", va="center",
    )
    ax.text(
        0.065, 1.28, f"· {label}",
        transform=ax.transAxes,
        fontsize=9, color=SUBTLE, ha="left", va="center",
    )

# ----- Panel 1: raw chunks -----
ax = axes[0]
style_axis(ax)
for i, c in enumerate(chunks):
    ax.plot(c.index, c.values, color=colors[i], lw=2.0, label=f"Chunk {i+1}", alpha=0.9)
    ax.fill_between(c.index, 0, c.values, color=colors[i], alpha=0.08)
ax.set_ylabel("Trends index\n(per-chunk 0–100)")
ax.set_ylim(bottom=0)
ax.legend(loc="upper right", ncol=3, handlelength=1.2)

add_step_label(ax, 1, "the problem", CHUNK_A)
ax.set_title("Three overlapping chunks, each normalised to its own 0–100 scale")

# Annotation: pointer at the overlap, callout
overlap_date = pd.Timestamp("2024-12-05")
ax.annotate(
    "Same date, three different values\nGoogle re-normalises every download",
    xy=(overlap_date, 80), xytext=(pd.Timestamp("2024-10-15"), 98),
    fontsize=9.5, color=INK, ha="left",
    bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=CHUNK_A, lw=1.2),
    arrowprops=dict(
        arrowstyle="-|>", color=CHUNK_A, lw=1.2,
        connectionstyle="arc3,rad=0.2",
    ),
)

# ----- Panel 2: naive concat -----
ax = axes[1]
style_axis(ax)
ax.plot(naive.index, naive.values, color=BAD, lw=2.0)
ax.fill_between(naive.index, 0, naive.values, color=BAD, alpha=0.1)
ax.set_ylabel("Trends index\n(mixed scales)")
ax.set_ylim(bottom=0)

# Vertical dashed line at each chunk join
for i, c in enumerate(chunks[1:], 1):
    ax.axvline(c.index[0], color=SUBTLE, alpha=0.35, lw=0.8, ls="--")

add_step_label(ax, 2, "a naïve attempt", BAD)
ax.set_title("Just paste them together → stair-step discontinuities at every join")

ax.annotate(
    "Artificial jumps would fool any\nmodel into seeing trend shifts\nthat don't exist",
    xy=(pd.Timestamp("2025-01-27"), naive.loc["2025-01-27"]),
    xytext=(pd.Timestamp("2025-02-20"), 90),
    fontsize=9.5, color=INK, ha="left",
    bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=BAD, lw=1.2),
    arrowprops=dict(
        arrowstyle="-|>", color=BAD, lw=1.2,
        connectionstyle="arc3,rad=-0.2",
    ),
)

# ----- Panel 3: stitched + calibrated -----
ax = axes[2]
style_axis(ax)
ax.fill_between(calibrated.index, 0, calibrated.values, color=GOOD, alpha=0.12)
ax.plot(calibrated.index, calibrated.values, color=GOOD, lw=2.2, label="Stitched daily")
ax.plot(weekly_ref.index, weekly_ref.values, color=REF, lw=2.0, alpha=0.75,
        label="Weekly reference", linestyle=(0, (4, 2)))
ax.set_ylabel("Trends index\n(calibrated scale)")
ax.set_xlabel("Date", color=SUBTLE)
ax.set_ylim(bottom=0)
ax.legend(loc="upper right", handlelength=2.2)

add_step_label(ax, 3, "what this skill does", GOOD)
ax.set_title("Median-ratio stitch + weekly calibration → one continuous, anchored series")

ax.annotate(
    "Smooth joins · daily resolution\nanchored to a known scale",
    xy=(pd.Timestamp("2025-01-27"), calibrated.loc["2025-01-27"]),
    xytext=(pd.Timestamp("2025-02-20"), 90),
    fontsize=9.5, color=INK, ha="left",
    bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=GOOD, lw=1.2),
    arrowprops=dict(
        arrowstyle="-|>", color=GOOD, lw=1.2,
        connectionstyle="arc3,rad=-0.2",
    ),
)

# ---------- Footer ----------
fig.text(
    0.96, 0.02,
    "github.com/wan-huiyan/google-trends-browser-fetch",
    fontsize=8.5, color=SUBTLE, ha="right", va="center", style="italic",
)

out = "/Users/huiyanwan/.claude/skills/google-trends-browser-fetch/assets/demo-stitching.png"
os.makedirs(os.path.dirname(out), exist_ok=True)
plt.savefig(out, dpi=160, bbox_inches="tight", facecolor=BG)
print(f"wrote {out}")
