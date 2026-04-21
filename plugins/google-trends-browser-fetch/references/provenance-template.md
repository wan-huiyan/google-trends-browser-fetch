# Provenance: <FILENAME>.csv

<!-- Every Google Trends CSV in your repo should have a companion .provenance.md
     written from this template. Fill in every field. Don't trust data without it. -->

## File

- **Source:** Google Trends (`<paste exact URL, e.g. https://trends.google.com/trends/explore?date=2024-09-29%202026-03-15&geo=GB&q=nike,running shoes&hl=en-GB>`)
- **Download date:** YYYY-MM-DD
- **Method:** Browser export via `<Claude in Chrome | Browser MCP | chrome-devtools-mcp | manual human download>` → CSV download button on "Interest over time" card
- **Search terms:** `<term1>`, `<term2>`, ...
- **Geography:** `<country name and/or ISO code, e.g. United Kingdom (GB), or "worldwide">`
- **Date range:** `<YYYY-MM-DD>` to `<YYYY-MM-DD>` (`<N>` rows, `<weekly|daily>`)
- **Processing:** `<describe any interpolation, stitching, calibration, or column derivations>`
- **Original filename:** `<whatever Google gave it, e.g. multiTimeline.csv>`

## Columns

| Column | Meaning | Units |
|---|---|---|
| date | Calendar date | ISO |
| `<term>` | `<raw trends index 0-100, or derived metric>` | `<index | ratio | ...>` |

<!-- For derived columns like `brand_share = nike / (nike + shoes)`, document the formula. -->

## If this file is a stitched daily series

Fill in the chunk-to-file mapping:

| Chunk | Date range | Source file |
|---|---|---|
| 0 | YYYY-MM-DD to YYYY-MM-DD | `multiTimeline (N).csv` |
| ... | ... | ... |

And the quality metrics from `stitch_daily.py`:

- Per-term stitching median ratio and std across chunk joins: `<paste>`
- Global calibration scalar: `<paste>`
- Daily-vs-weekly correlation: `<paste>`

## Exogeneity note

<!-- If this file is being used as a covariate in a causal-inference model,
     document why you believe it's exogenous to your intervention.
     Brand-level search is usually NOT exogenous to a brand's own promotion.
     Category-level search (e.g. "shoes") usually IS. -->

`<explain why this signal can be trusted not to spike in response to the treatment>`

## History

- **vN (YYYY-MM-DD):** `<brief change note — e.g. "added weekly calibration step", or "v1 was fabricated, replaced with real download">`
