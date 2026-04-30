"""Parse configured Trends query terms: always a list of strings, one term per URL."""

from __future__ import annotations

import json
import os
from typing import Any


def parse_terms_from_env(environ: dict[str, str] | None = None) -> list[str]:
    """Return the list of query strings to run **sequentially**, one Google Trends URL each.

    **Rules**

    - If ``TRENDS_TERMS`` is non-empty: parse it (JSON array **or** comma-separated list).
      ``QUERY_TERM`` is ignored in that case.
    - Else if ``QUERY_TERM`` is non-empty: return a single-element list.
    - Else: return ``[]``.

    JSON array example: ``TRENDS_TERMS='["brake pads","car battery"]'`` (best when a term
    contains commas).
    """
    env = environ if environ is not None else os.environ
    raw_multi = env.get("TRENDS_TERMS", "").strip()
    if raw_multi:
        if raw_multi.startswith("["):
            try:
                data: Any = json.loads(raw_multi)
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()]
            except json.JSONDecodeError:
                pass
        return [t.strip() for t in raw_multi.split(",") if t.strip()]
    qt = env.get("QUERY_TERM", "").strip()
    if qt:
        return [qt]
    return []


def single_term_env(environ: dict[str, str], term: str) -> dict[str, str]:
    """Child-process env: exactly one term via ``QUERY_TERM``; strip multi-term vars."""
    out = {**environ, "QUERY_TERM": term}
    out.pop("TRENDS_TERMS", None)
    return out
