# Google Trends URL examples

The explore URL uses four query params. Get them right and you get a reliable deep link; get them wrong and you silently get different data than expected.

```
https://trends.google.com/trends/explore?date=<START>%20<END>&geo=<GEO>&q=<TERMS>&hl=<LANG>
```

## `date`

Two ISO dates separated by a URL-encoded space (`%20`). Both dates are inclusive. Google Trends returns:
- **Weekly** data for ranges ≥ 90 days
- **Daily** data for ranges < 90 days
- **Hourly** data for ranges ≤ 7 days

Special values:
- `today 12-m` → last 12 months (weekly)
- `today 5-y` → last 5 years (weekly)
- `all` → since 2004 (monthly)

Prefer explicit ISO dates for reproducibility.

## `geo`

ISO country code. Can be drilled down to sub-regions (e.g. `GB-ENG`, `US-CA`).

Omit the `geo` param entirely for worldwide. **Empty string is not worldwide** — it's an error in some views.

Worked examples:
- `geo=GB` — United Kingdom
- `geo=US-CA` — California, United States
- `geo=DE-BE` — Berlin, Germany

## `q`

Search terms, comma-separated, URL-encoded. Up to **5 terms** per comparison; more and Google silently drops the overflow.

Terms can be:
- **Search terms** (default): `nike` matches all queries containing "nike"
- **Topics**: prefix with their entity ID (e.g. `/m/01c7q4`). Topics group variant spellings + translations. Get these from the Trends UI autocomplete. Topics are more robust to spelling variants but less transparent.
- **Quoted phrases**: `"nike"` matches exactly

For a brand-vs-category comparison:
```
q=nike,running shoes
```

For a brand-vs-competitors comparison:
```
q=nike,adidas,puma
```

## `hl`

UI language. Doesn't change data, but changes the CSV locale — `en-GB` gives you `DD/MM/YYYY` dates in some views, `en-US` gives you `MM/DD/YYYY`. Stick with one.

Recommended: `hl=en-GB` for UK/EU work, `hl=en-US` otherwise.

## Worked URLs

### Single term, worldwide, full range (weekly)
```
https://trends.google.com/trends/explore?date=2024-01-01%202025-12-31&q=running%20shoes&hl=en-US
```

### Two-term comparison, UK, 18 months (weekly, co-normalized)
```
https://trends.google.com/trends/explore?date=2024-09-29%202026-03-15&geo=GB&q=nike,running shoes&hl=en-GB
```
The CSV will have co-normalized columns: both terms on the same 0-100 scale, so you can compute `brand_share = term1 / (term1 + term2)`.

### Same two terms, single 75-day chunk (daily)
```
https://trends.google.com/trends/explore?date=2025-02-01%202025-04-15&geo=GB&q=nike,running shoes&hl=en-GB
```
Now the CSV has one row per day.

### Topic instead of search term
```
https://trends.google.com/trends/explore?date=today%2012-m&geo=US&q=/m/01d74z&hl=en-US
```
(`/m/01d74z` is the "Nike, Inc." topic.)

## Common mistakes

1. **Forgetting `%20` between dates** — you'll land on the homepage with no filter applied.
2. **Using `+` instead of `%20`** — Trends sometimes accepts `+` but it's inconsistent; stick with `%20`.
3. **More than 5 terms** — the 6th+ term is silently dropped. No error.
4. **Mixing `geo=` omitted vs `geo=` empty** — omit entirely for worldwide; empty string can break the page.
5. **Expecting daily data at 90 days exactly** — the cutoff is strict. Use 89 days or less to guarantee daily.
