#!/usr/bin/env python3
"""Run one sync cycle: fetch each configured search, diff against the DB,
and print what's new / changed / gone.

    python run.py                      # use searches.yaml + olxdeals.db
    python run.py --config other.yaml --db other.db
    python run.py --quiet              # summary only
"""

from __future__ import annotations

import argparse

from olxdeals.config import load_searches
from olxdeals.fetcher import OlxFetcher
from olxdeals.store import Store, SyncResult


def _fmt_price(v: float | None, cur: str | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0f} {cur or ''}".strip()


def report(result: SyncResult, quiet: bool) -> None:
    print(
        f"[{result.search_key}] seen={result.total_seen} "
        f"new={len(result.new)} price_changes={len(result.price_changes)} "
        f"removed={len(result.removed)} unchanged={result.unchanged}"
    )
    if quiet:
        return
    for item in result.new:
        print(f"  + NEW   {_fmt_price(item['price'], item['currency']):>12}  "
              f"{item['title'][:55]}\n          {item['url']}")
    for ch in result.price_changes:
        arrow = "↓" if (ch.new_price or 0) < (ch.old_price or 0) else "↑"
        print(f"  {arrow} PRICE {_fmt_price(ch.old_price, ch.listing['currency'])} "
              f"-> {_fmt_price(ch.new_price, ch.listing['currency'])}  "
              f"{ch.listing['title'][:45]}\n          {ch.listing['url']}")
    if result.removed:
        print(f"  - {len(result.removed)} listing(s) no longer in results")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync OLX searches into the local DB")
    ap.add_argument("--config", default="searches.yaml")
    ap.add_argument("--db", default="olxdeals.db")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between API pages (be polite)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    specs = load_searches(args.config)
    fetcher = OlxFetcher(delay=args.delay)

    with Store(args.db) as store:
        for spec in specs:
            listings = fetcher.fetch_all(spec)
            result = store.sync(listings, spec.key)
            report(result, args.quiet)


if __name__ == "__main__":
    main()
