"""Discover OLX category ids and model keys from real listings.

OLX's filter-metadata endpoints are locked down, but every offer returned by
the search API carries its own ``category.id`` and ``params.model.key``. So we
just run a free-text query and tally what comes back — giving you the exact
values to paste into a search's "Category id" and "OLX model key" fields.

    python -m olxdeals.discover "iphone 15 pro"
    python -m olxdeals.discover "bmw seria 3" --pages 3
"""

from __future__ import annotations

import argparse
from collections import Counter

from .fetcher import API_URL, PAGE_LIMIT, _HEADERS
import requests


def discover(query: str, pages: int = 2) -> tuple[Counter, Counter]:
    session = requests.Session()
    session.headers.update(_HEADERS)
    categories: Counter = Counter()   # (id, type) -> count
    models: Counter = Counter()       # (key, label) -> count

    for i in range(pages):
        params = [("offset", str(i * PAGE_LIMIT)), ("limit", str(PAGE_LIMIT)),
                  ("category_id", "0"), ("query", query)]
        resp = session.get(API_URL, params=params, timeout=25)
        resp.raise_for_status()
        batch = resp.json().get("data") or []
        if not batch:
            break
        for offer in batch:
            cat = offer.get("category") or {}
            categories[(cat.get("id"), cat.get("type"))] += 1
            for p in offer.get("params", []):
                if p.get("key") == "model":
                    val = p.get("value") or {}
                    if isinstance(val, dict) and val.get("key"):
                        models[(val["key"], val.get("label"))] += 1
    return categories, models


def main() -> None:
    ap = argparse.ArgumentParser(description="Find OLX category ids & model keys")
    ap.add_argument("query", help='free-text search, e.g. "iphone 15 pro"')
    ap.add_argument("--pages", type=int, default=2, help="pages to sample (50/page)")
    args = ap.parse_args()

    categories, models = discover(args.query, args.pages)

    print(f'\nResults for "{args.query}" (sampled {sum(categories.values())} listings)\n')
    print("CATEGORY IDs (paste into 'Category id'):")
    for (cid, ctype), n in categories.most_common(8):
        print(f"  {cid!s:>6}  {ctype or '':<14} ×{n}")
    print("\nMODEL KEYS (paste into 'OLX model key'):")
    if not models:
        print("  (none — this category may not expose a model filter)")
    for (key, label), n in models.most_common(15):
        print(f"  {key:<28} {label or '':<20} ×{n}")
    print()


if __name__ == "__main__":
    main()
