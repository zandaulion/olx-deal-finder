#!/usr/bin/env python3
"""Run one sync cycle: fetch each configured search, diff against the DB,
and print what's new / changed / gone.

    python run.py                      # use searches.yaml + olxdeals.db
    python run.py --config other.yaml --db other.db
    python run.py --quiet              # summary only
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from olxdeals import fx, scorer
from olxdeals.config import load_searches
from olxdeals.fetcher import OlxFetcher
from olxdeals.push import Push
from olxdeals.scorer import price_distribution, score_search
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


def notify_new_deals(store, push, search_key, active, result) -> None:
    """Push a batched notification when newly-appeared listings are deals."""
    subs = store.all_subscriptions()
    if not subs or not result.new:
        return
    new_ids = {l["id"] for l in result.new}
    sd = score_search(search_key, active)
    new_deals = [sl for sl in sd.listings
                 if sl.raw["id"] in new_ids and sl.is_deal]
    if not new_deals:
        return
    cheapest = min(new_deals, key=lambda s: s.price_ron or float("inf"))
    body = f"from {cheapest.price_ron:.0f} RON — {cheapest.raw['title'][:70]}"
    # Enrich with the LLM verdict when the analysis already ran this sync.
    analysis = store.get_analyses([cheapest.raw["id"]]).get(cheapest.raw["id"])
    if analysis and analysis.get("score") is not None:
        body += (f"\nAI: {analysis['score']}/100 · "
                 f"{(analysis.get('summary') or '')[:90]}")
    n = len(new_deals)
    payload = {
        "title": f"{n} new deal{'s' if n > 1 else ''} · {search_key}",
        "body": body,
        "url": f"/?search={search_key}",
        "tag": f"deal-{search_key}",
    }
    for endpoint in push.notify_all(subs, payload):
        store.remove_subscription(endpoint)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync OLX searches into the local DB")
    ap.add_argument("--config", default="searches.yaml")
    ap.add_argument("--db", default="olxdeals.db")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="base seconds between API pages (be polite)")
    ap.add_argument("--jitter", type=float, default=0.5,
                    help="extra random delay added to each wait, in seconds")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    specs = load_searches(args.config)
    fetcher = OlxFetcher(delay=args.delay, jitter=args.jitter)
    push = Push(Path(args.db).resolve().with_name("vapid_key.pem"))

    failures = 0
    with Store(args.db) as store:
        # Refresh the EUR→RON rate (~once/day) so conversions stay accurate.
        scorer.EUR_TO_RON = fx.refresh(store)
        for spec in specs:
            if spec.paused:
                print(f"[{spec.key}] paused — skipped")
                continue
            started = time.monotonic()
            try:
                # Fetch fully before touching the DB: a mid-pagination failure
                # then aborts this search cleanly (no partial removal-marking).
                listings = fetcher.fetch_all(spec)
            except Exception as exc:  # fail-soft: one search can't kill the rest
                failures += 1
                dur = int((time.monotonic() - started) * 1000)
                store.record_run(spec.key, ok=False, duration_ms=dur, error=str(exc))
                print(f"[{spec.key}] FETCH FAILED: {exc}")
                continue
            result = store.sync(listings, spec.key)
            active = store.active_for_search(spec.key)
            # Snapshot the current price distribution for the daily trend chart.
            dist = price_distribution(active)
            if dist:
                store.record_stats(spec.key, dist)
            notify_new_deals(store, push, spec.key, active, result)
            dur = int((time.monotonic() - started) * 1000)
            store.record_run(spec.key, ok=True, duration_ms=dur, result=result)
            report(result, args.quiet)

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
