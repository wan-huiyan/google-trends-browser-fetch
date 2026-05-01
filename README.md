# google-trends-browser-fetch

Fetch Google Trends data via browser automation, with multi-chunk daily-resolution stitching for date ranges longer than 90 days.

[![GitHub release](https://img.shields.io/github/v/release/wan-huiyan/google-trends-browser-fetch)](https://github.com/wan-huiyan/google-trends-browser-fetch/releases)
[![license](https://img.shields.io/github/license/wan-huiyan/google-trends-browser-fetch)](LICENSE)
[![last commit](https://img.shields.io/github/last-commit/wan-huiyan/google-trends-browser-fetch)](https://github.com/wan-huiyan/google-trends-browser-fetch/commits)
[![python](https://img.shields.io/badge/python-3.9--3.12-yellow)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-skill-orange)](https://claude.com/claude-code)

[![Pipeline overview — animated, interactive Path 1 + scheduled Path 2 by @kate-wheatley](https://raw.githubusercontent.com/wan-huiyan/google-trends-browser-fetch/main/docs/pipeline-flow.gif?v=1)](https://raw.githubusercontent.com/wan-huiyan/google-trends-browser-fetch/main/docs/pipeline-pixel-animated.svg?v=1)

The skill works two ways. **Path 1 — interactive (attended):** you + an LLM + Chrome on your laptop, for one-off analysis. **Path 2 — scheduled (headless):** Cloud Scheduler + Cloud Run + BigQuery, for hands-off nightly refreshes into a warehouse — designed and built by [@kate-wheatley](https://github.com/kate-wheatley).

![Demo: chunk stitching](docs/demo-stitching.png)

The panel above shows the problem this skill solves. Google Trends normalises every query to 0–100 within its own date range, so downloading overlapping chunks gives you the *same day* at *different values* (top). Naïve concatenation produces stair-step discontinuities that break any downstream model (middle). The skill's median-ratio stitching + weekly calibration produces a continuous daily series anchored to a known reference scale (bottom).

## Why this skill exists

Two facts that aren't obvious until you hit them:

1. **`pytrends` is archived.** The community Python library was archived in April 2025 after Google tightened anti-scraping. The official Google Trends API launched July 2025 is gated alpha. For most users, **browser-driven CSV export is the only reliable path** as of April 2026.
2. **Google Trends silently downgrades to weekly for long ranges.** Anything ≥ 90 days returns weekly. To get daily over, say, 18 months, you have to download overlapping ~75-day chunks and stitch them — which is fiddly because each chunk has its own independent 0–100 normalisation.

This skill codifies both: the browser workflow (interactive on your laptop or headless on Cloud Run) and the stitching algorithm.

## ⚠️ One-time Chrome setup before first multi-chunk fetch

Chrome blocks the 2nd download from a site by default. Without this change, only the first chunk arrives and the rest silently fail. Flip this once:

**Chrome → Settings → Privacy and security → Site settings → Additional permissions → Automatic downloads →** select **"Sites can ask to automatically download multiple files"**.

Or, tighter: the first time Chrome blocks a download you'll see a small icon in the URL bar — click it and choose "Always allow trends.google.com to download multiple files".

This applies to Path 1 (interactive) — Path 2 runs headless in a container and downloads to a writable directory inside the image.

## Quick start

```
You: I need Google Trends data for "nike" and "running shoes" in the UK,
     daily resolution, from Sep 2024 to Mar 2026, for a BSTS model.

Claude: (invokes google-trends-browser-fetch)
        → Plans 9 overlapping 75-day chunks + 1 weekly reference
        → Drives Chrome to download all 10 CSVs
        → Runs stitch_daily.py (median-ratio + weekly calibration)
        → Writes data/trends_daily.csv + data/trends_daily.provenance.md
        → Reports: stitching std=0.03, daily-weekly r=0.66 ✓
```

## Installation

These steps cover **Path 1** (interactive). For **Path 2** (scheduled, headless), see the [`gcloud run deploy` snippet](#path-2-scheduled-production-cloud-run--cloud-scheduler) in the Path 2 section.

### Claude Code

```bash
# Plugin install (recommended)
/plugin marketplace add wan-huiyan/google-trends-browser-fetch
/plugin install google-trends-browser-fetch@wan-huiyan-google-trends-browser-fetch

# Or clone directly
git clone https://github.com/wan-huiyan/google-trends-browser-fetch.git \
  ~/.claude/skills/google-trends-browser-fetch
```

### Cursor / other IDEs

Cursor doesn't have Anthropic's Claude-in-Chrome extension, but a generic browser MCP fills the gap. Full setup in ~5 minutes:

**1. Clone the skill:**
```bash
git clone https://github.com/wan-huiyan/google-trends-browser-fetch.git \
  ~/.cursor/skills/google-trends-browser-fetch
```

**2. Install a browser MCP** (pick one):

- **Recommended: [Browser MCP](https://browsermcp.io/)** — uses your signed-in Chrome session, so Google Trends rate limits don't bite on multi-chunk fetches.
  1. Install the [Browser MCP Chrome extension](https://chromewebstore.google.com/detail/browser-mcp-automate-your/bjfgambnhccakkhmkepdoekmckoijdlc)
  2. Add to `.cursor/mcp.json` (or via `Cursor Settings → Tools → New MCP Server`):
     ```json
     {
       "mcpServers": {
         "browsermcp": {
           "command": "npx",
           "args": ["@browsermcp/mcp@latest"]
         }
       }
     }
     ```
  3. Open the extension popup → click **Connect** once to bind it to the MCP server.

- **Alternative: [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)** — no extension required (launches its own Chrome via Puppeteer). Anonymous session means you'll hit Trends rate limits faster on 5+ chunk fetches. Config:
   ```json
   {
     "mcpServers": {
       "chrome-devtools": {
         "command": "npx",
         "args": ["-y", "chrome-devtools-mcp@latest"]
       }
     }
   }
   ```

**3. Restart Cursor** and ask it to fetch Trends data — it'll pick up the skill and drive the browser through whichever MCP you installed. The stitching scripts (`plan_chunks.py`, `stitch_daily.py`) are pure Python and work identically regardless of which browser-automation path you chose.

## Compatibility

The skill supports two paths. They share the same chunk-planning + median-ratio stitching code under the hood — the difference is whether a human has to be at the laptop.

| Path | What it is | When to use |
|---|---|---|
| **1. Interactive (attended)** | A signed-in Chrome session on your laptop, driven by an LLM or a local script. Drivers: Claude-in-Chrome, Browser MCP, chrome-devtools-mcp, or a local Playwright run. | One-off analysis, ad-hoc exploration, when you want to inspect each download. |
| **2. Scheduled (headless)** | Headless Playwright in a Cloud Run container, triggered per-term by Cloud Scheduler, upserting into BigQuery. No laptop required. | Recurring refresh into a warehouse; production pipelines; nightly dashboards. |

Both modes enforce the same one-`q`-per-URL rule, run the same stitching algorithm, and produce the same calibrated daily output. See [SKILL.md](plugins/google-trends-browser-fetch/SKILL.md) for Path 1 setup and the [Path 2 section below](#path-2-scheduled-production-cloud-run--cloud-scheduler) for the headless Cloud Run service.

<details>
<summary><strong>Within Path 1: which driver fits my setup?</strong> (API keys, Bedrock/Vertex, Cursor — verified April 2026)</summary>

The Anthropic-first-party "Claude in Chrome" extension has specific gating. If you hit a "not available" message, check the table below — then fall back to Browser MCP / chrome-devtools-mcp, which covers the gaps.

| Your setup | Claude-in-Chrome? | What to use |
|---|---|---|
| Claude Code **CLI** (`claude --chrome`) + Pro/Max/Team/Enterprise plan | ✅ Yes — primary documented path | Claude-in-Chrome |
| Claude Code **VS Code extension** + paid plan | ✅ Yes | Claude-in-Chrome |
| Claude **desktop app** + paid plan | ✅ Yes (⚠️ [known conflict](https://github.com/anthropics/claude-code/issues/46869): if desktop + CLI both registered, desktop wins and CLI errors "extension not connected") | Claude-in-Chrome |
| **Cursor / Windsurf / other IDEs** | ❌ No — Anthropic-specific extension | Browser MCP / chrome-devtools-mcp |
| **Anthropic API key / console credits / pay-as-you-go** | ❌ Explicitly excluded from Claude-in-Chrome | Browser MCP / chrome-devtools-mcp |
| **Bedrock / Vertex AI / Microsoft Foundry** | ❌ Explicitly excluded — [need separate claude.ai account](https://code.claude.com/docs/en/chrome) | Browser MCP, or a separate claude.ai account |
| **Brave / Arc / other Chromium** browsers | ❌ Only Chrome + Edge supported | Install Chrome, or use a local Playwright script |
| **WSL** (Windows Subsystem for Linux) | ❌ Not supported | Local Playwright script |

Sources: [Claude Code Chrome docs](https://code.claude.com/docs/en/chrome), [claude.com/claude-for-chrome](https://claude.com/claude-for-chrome). Requirements verified April 2026 — check the official docs for the current state before assuming.

</details>

## Path 2: Scheduled production (Cloud Run + Cloud Scheduler)

> **Huge shoutout to [@kate-wheatley](https://github.com/kate-wheatley)**, who designed and built this entire path in [PR #1](https://github.com/wan-huiyan/google-trends-browser-fetch/pull/1) — promoting the skill from a one-off interactive workflow into a hands-off scheduled production pipeline. The 13-file service below is hers end to end: container, HTTP entrypoint, BQ upsert design, scheduling pattern, and all the env-driven configuration plumbing.

For unattended, scheduled scraping, the [`cloud-run-google-trends-scraper/`](cloud-run-google-trends-scraper/) directory ships a Cloud Run service that runs the same chunking + stitching pipeline inside a container with headless Playwright, then upserts results into BigQuery. Designed for one query term per invocation (matching the one-`q`-per-URL rule the skill enforces interactively).

**What it gives you:**

- **HTTP entrypoint** (`run_trends_job`, Functions Framework) — Cloud Scheduler hits the service URL once per term, passing the term in a custom header (`X-Trends-Query-Term`).
- **Two run modes**:
  - `full` — explicit `START_DATE` / `END_DATE`, full refresh.
  - `daily` (alias `incremental`) — rolling lookback ending at `TRENDS_AS_OF_DATE` (or today, UTC). BigQuery upserts new days without wiping history. First-seen terms get auto-backfilled from `TRENDS_WEEKLY_START` for full history.
- **BigQuery sinks** — weekly reference, daily stitched, and stitching-quality tables (auto-created on first run).
- **Optional GCS artifacts** — full per-run output (chunk CSVs, stitched daily, quality JSON) under `gs://<bucket>/runs/<RUN_ID>/`.
- **Per-term parallelism** — run one Cloud Scheduler job per term; recommended `Cloud Run concurrency = 1` so per-request env overrides don't overlap.

**Quick deploy** (full env reference in [`cloud-run-google-trends-scraper/.env.example`](cloud-run-google-trends-scraper/.env.example)):

```bash
cd cloud-run-google-trends-scraper
gcloud run deploy google-trends-scraper \
  --source=. \
  --region=europe-west2 \
  --concurrency=1 \
  --set-env-vars=PROJECT_ID=...,DATASET_ID=google_trends,TRENDS_GEO=GB,TRENDS_HL=en-GB
```

Then create one Cloud Scheduler job per term, with HTTP target = the service URL and a custom header `X-Trends-Query-Term: <your term>`.

**When to use Path 2 vs Path 1:** use Path 2 when you need recurring data (e.g. nightly refresh into a warehouse for a BSTS pipeline). Use Path 1 for one-off interactive analysis where you want to inspect each step.

**Design highlights worth calling out** (all credit to [@kate-wheatley](https://github.com/kate-wheatley)):

- **Idempotent incremental refresh** — `daily` mode never wipes history; it computes a rolling lookback window long enough to satisfy the stitching algorithm's overlap requirements, then upserts only new days into BigQuery. This is the right primitive for a scheduled pipeline and is genuinely tricky to get right with chunked, normalised data.
- **First-seen-term auto-backfill** — when a brand-new term appears in `TRENDS_TERMS` and has no rows in BQ, the service silently widens *that term's* window to a full historical backfill from `TRENDS_WEEKLY_START`, while existing terms stay on the cheap rolling lookback. New terms join the dataset without operator intervention.
- **One-term-per-invocation, scheduler-friendly** — the per-request `X-Trends-Query-Term` header (with case-insensitive fallback aliases) lets a single Cloud Run service back N independent Cloud Scheduler jobs (one per term) with `concurrency=1` so per-request env overrides can't race.
- **Dual table-id resolution** — every BQ table env var accepts either a fully-qualified `project.dataset.table` or a bare table id that combines with `PROJECT_ID` + `DATASET_ID`, so the same image works across dev/prod without rewrites.
- **Pluggable weekly reference** — `STITCH_WEEKLY_REF_SOURCE=bq` lets the calibration step reuse a previously-loaded weekly table instead of re-downloading, reducing Trends rate-limit exposure on every run.

## What you get

**Path 1 — interactive scripts and references:**

- **`scripts/plan_chunks.py`** — generates overlapping chunk date ranges + parameterised Trends URLs. Example: `--start 2024-09-29 --end 2026-03-15 --chunk-days 75 --overlap-days 15` → 9 chunks covering 18 months.
- **`scripts/stitch_daily.py`** — chain-median-ratio cross-normalisation on overlaps, then a global scalar calibration against a weekly reference. Reports per-term stability and daily-vs-weekly correlation.
- **`references/stitching-math.md`** — the why behind median-ratio (vs OLS, mean ratio, max ratio).
- **`references/provenance-template.md`** — companion `.provenance.md` template so every downloaded CSV has traceable origin.
- **`references/url-examples.md`** — the `date` / `geo` / `q` / `hl` parameter cookbook.

**Path 2 — Cloud Run service** (under [`cloud-run-google-trends-scraper/`](cloud-run-google-trends-scraper/), all by [@kate-wheatley](https://github.com/kate-wheatley)):

- **`main.py`** — Functions Framework HTTP entrypoint (`run_trends_job`); parses env / headers, dispatches per-term pipeline runs, drives `full` vs `daily` modes, handles GCS artifact upload.
- **`fetch_playwright.py`** — headless Playwright driver that navigates Trends, plans clicks, downloads chunk CSVs, retries on rate-limit / captcha, and persists each download with provenance.
- **`plan_chunks.py`** / **`stitch_daily.py`** — same chunking + stitching algorithm as Path 1, vendored into the container so the image is self-contained.
- **`bq_reference_weekly.py`** — loads / refreshes the weekly-reference table in BigQuery; supports the `STITCH_WEEKLY_REF_SOURCE=bq` calibration mode so subsequent runs don't have to re-download it.
- **`bq_run_outputs.py`** — idempotent upsert of `trends_daily` and `stitch_quality` into BigQuery; auto-creates tables on first run, never wipes history.
- **`terms_input.py`** — resolves `TRENDS_TERMS` (CSV string or JSON array) and per-request `X-Trends-Query-Term` header; enforces one-`q`-per-URL.
- **`Dockerfile`**, **`requirements.txt`**, **`project.toml`**, **`.env.example`**, **`.dockerignore`** — container build, deps, Functions Framework manifest, full env reference, and a sane ignore list for source uploads.

## With skill vs without

| | Without | With |
|---|---|---|
| Time budget (one-off) | Several hours (figure out that pytrends is dead, discover the 90-day cutoff, re-download after realising you have weekly not daily) | 10–15 minutes |
| Chunk planning | Hand-roll date math, inevitably wrong overlap | `plan_chunks.py` |
| Cross-chunk scale | Naïve concat → stair-steps → silent model bias | Median-ratio stitch + weekly calibration |
| Provenance | Forgotten; someone else inherits and can't reproduce | `.provenance.md` with URL + method + chunk map |
| Quality check | Eyeball the plot | Quantitative: stitching std, daily-weekly r, calibration scalar |
| Recurring refresh into a warehouse | Cron + babysit a laptop + manual BQ reconcile (or: just rebuild from scratch every week) | Cloud Scheduler → Cloud Run → idempotent BQ upsert; first-seen terms auto-backfill; never wipes history |

## How it works

1. **Decide resolution** — weekly enough? Single download, done. Daily and range < 90 days? Single download. Daily and range ≥ 90 days? Use the stitched path.
2. **Plan chunks** — overlapping ~75-day windows with ~15-day overlaps (enough signal for robust median-ratio, not so much you download redundant data).
3. **Download weekly reference** — one extra download covering the full range, used for the final calibration to a stable scale.
4. **Download each chunk** — drive the browser to each URL, click the "Interest over time" CSV button, rename the file.
5. **Stitch** — chain-compose median ratios across overlaps so every chunk lives on chunk 0's scale; merge with mean on overlap days.
6. **Calibrate** — compute `scalar = median(weekly_ref / stitched.resample("W").mean())` per term; apply.
7. **Sanity-check** — per-term ratio std < 0.15, daily-weekly r in 0.5–0.8, visual join inspection.

In **Path 2**, steps 2–7 run inside a Cloud Run container per Cloud Scheduler invocation; step 7's output is then upserted into BigQuery instead of written to a local CSV. The `daily` mode short-circuits the chunk plan to a rolling lookback window so each scheduled run only fetches new days, not the full history.

## Design decisions

- **Why median ratio, not OLS?** Overlaps are short (~15 days) and contain occasional spikes (Black Friday, holidays). Single-parameter median is scale-invariant and outlier-robust; OLS injects a baseline shift that isn't really there. See [references/stitching-math.md](plugins/google-trends-browser-fetch/references/stitching-math.md).
- **Why a separate weekly reference?** Chain-stitching fixes *relative* scales across chunks but the whole series is in chunk-0's arbitrary units. The weekly reference anchors to a single known scale.
- **Why two paths?** Path 1 (attended Chrome) is right for one-off analysis where you want eyes on each download; Path 2 (headless Cloud Run) is right for recurring refreshes into a warehouse. Within Path 1, multiple drivers (Claude-in-Chrome, Browser MCP, chrome-devtools-mcp, local Playwright) cover the gating differences across IDE / plan / browser combinations.
- **Why `~75-day` chunks, not `~85`?** Hard cap is 89 days (anything ≥ 90 returns weekly). 75 leaves margin for timezone/rendering edge cases.

## Limitations

- Does NOT bypass Google's rate limiting. Expect captchas on anonymous sessions; sign in.
- Does NOT guarantee exogeneity. `trend_category` (e.g., "running shoes") is usually exogenous to a brand's promotion; `trend_<brand>` usually is NOT — brand search spikes during the brand's own promotions. See provenance template.
- Does NOT replace official Google Trends API when you have access to it. If your org has it, use it.
- Does NOT work with mobile-only browsers or headless scripts at scale (Trends detects and blocks).
- Does NOT auto-recover from Google UI redesigns. The CSV button location is stable enough that `javascript_tool` fallback finds it by aria-label / SVG icon, but major redesigns may require the skill to be updated.
- **Path 2 specifically**: headless Cloud Run runs an anonymous Trends session, so expect more captchas / rate limits than a signed-in laptop session. Cookie injection is possible (mount a Google session into the container) but is not wired up out of the box — for high-volume term lists, plan around per-term rate limits rather than parallelism.

## Pitfalls to avoid

- **Don't recommend `pytrends`.** Archived April 2025.
- **Don't generate synthetic data that looks real.** Every Trends CSV must trace to an actual browser download.
- **Don't interpret weekly-interpolated-to-daily as true daily.** It has autocorr ≈ 0.99 (flat within each week); adds nothing to a BSTS that already has weekly seasonality.
- **Don't forget Trends indices are relative, not absolute.** You cannot compare two independent downloads without a calibration step.
- **Don't click the page-wide download menu.** It exports a multi-sheet zip. Click the CSV icon on the specific "Interest over time" card.

## Related skills

- **[causal-impact-campaign](https://github.com/wan-huiyan/causal-impact-campaign)** — BSTS-based causal impact estimation; Trends daily series is the canonical exogenous covariate for this skill's models. Direct downstream consumer.
- **[gcp-cloud-run](https://github.com/wan-huiyan/gcp-cloud-run)** — production Cloud Run patterns (Functions Framework, buildpack source layouts, concurrency, IAM). Useful background for understanding or extending Path 2.
- **[gcp-pipeline-cost-analysis](https://github.com/wan-huiyan/gcp-pipeline-cost-analysis)** — estimate monthly GCP spend before turning on Path 2 at scale (Cloud Run invocations × Playwright cold-starts × BQ storage).

## Dependencies

**Shared (both paths):**

| | Required? | If missing |
|---|---|---|
| Python 3.9+ with pandas + numpy | Yes, for stitching | Weekly single-download still works; daily stitched does not |
| One run mode (Path 1 attended Chrome, OR Path 2 Cloud Run) | One required | Can't fetch — pick whichever fits your workflow |

**Path 1 — interactive (attended Chrome):**

| | Required? | If missing |
|---|---|---|
| Local Chrome or Edge | Yes | Brave/Arc/Firefox not supported by Claude-in-Chrome; fall back to a local Playwright script |
| A driver: Claude-in-Chrome / Browser MCP / chrome-devtools-mcp / local Playwright | One required | See the [within-Path-1 driver table](#compatibility) above |
| Signed-in Google account in the browser | Strongly recommended | Anonymous sessions rate-limit faster |

**Path 2 — scheduled (headless Cloud Run):**

| | Required? | If missing |
|---|---|---|
| GCP project with Cloud Run + Cloud Scheduler enabled | Yes | Path 2 won't deploy |
| BigQuery dataset (one for `weekly_reference`, `trends_daily`, `stitch_quality`) | Yes | Service runs but has nowhere to upsert; tables are auto-created on first run |
| GCS bucket | Optional | Without it, per-run chunk CSVs and quality JSON aren't archived (the BQ tables still get the final stitched output) |
| Cookie injection for signed-in headless | Optional | Anonymous container sessions hit rate limits sooner; for high-volume term lists, mount a Google session

<details>
<summary>Quality checklist (what this skill guarantees)</summary>

- [x] Every downloaded CSV has a companion `.provenance.md` with source URL, date, method
- [x] Stitched daily series has quantitative quality metrics (ratio std, daily-weekly r)
- [x] Chunk planning is deterministic and reproducible from start/end dates
- [x] Fails loudly when overlaps are too short (< 3 days) or produce wild ratios (std > 0.15)
- [x] No synthetic data fallback — if download fails, the script errors, never makes up numbers
- [x] Two run modes: interactive (attended Chrome) and scheduled (headless Cloud Run), sharing the same chunking + stitching code
</details>

## Version history

- **v1.1.0** (2026-04-30) — **major new capability: Path 2 (scheduled, headless) via Cloud Run + Cloud Scheduler**, designed and built end-to-end by [@kate-wheatley](https://github.com/kate-wheatley) ([#1](https://github.com/wan-huiyan/google-trends-browser-fetch/pull/1) — 13 files, ~2.9k lines). Adds the `cloud-run-google-trends-scraper/` service: containerised headless-Playwright pipeline, HTTP entrypoint via Functions Framework, `full` (date-range refresh) and `daily` (rolling-lookback incremental upsert) modes, BigQuery sinks for daily / weekly-reference / stitching-quality with auto-backfill for first-seen terms, scheduler-friendly per-term invocation via `X-Trends-Query-Term` header, dual table-id resolution, optional GCS artifact archival, and a pluggable BQ-or-CSV weekly reference. Promotes the skill from a one-off interactive workflow to a hands-off scheduled production pipeline. Also collapses the prior three interactive sub-paths (A/B/C) into a single Path 1 (attended Chrome, multiple drivers) and drops the manual-only path from the table.
- **v1.0.0** (2026-04-21) — initial release. Three interactive sub-paths (now consolidated into Path 1), 5 files, synthetic-demo screenshot, anonymised from a real 9-chunk 18-month UK retail project.

## License

[MIT](LICENSE) © 2026 Huiyan Wan
