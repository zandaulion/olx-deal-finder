#!/usr/bin/env python3
"""Run one sync cycle: fetch each configured search, diff against the DB,
and print what's new / changed / gone.

    python run.py                      # use searches.yaml + olxdeals.db
    python run.py --config other.yaml --db other.db
    python run.py --quiet              # summary only
"""

from __future__ import annotations

import argparse
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

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


NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/monitoring")


def _post_ntfy(title: str, body: str, priority: str, tags: str) -> None:
    """POST one message to ntfy (fail-soft — never breaks the sync)."""
    if not NTFY_URL:
        return
    try:
        requests.post(NTFY_URL, data=body.encode("utf-8"), timeout=10,
                      headers={"Title": title, "Priority": priority, "Tags": tags})
    except Exception as exc:
        print(f"ntfy notify failed: {exc}")


# ntfy carries operations, not deals -- new listings go to the PWA over web
# push (see notify_new_deals). The sync runs hourly, so posting a healthy
# summary every time produced 24 messages a day and trained the eye to swipe
# the topic away, which is exactly when a real failure gets missed.
#
# So: report problems, report the recovery that closes them, and otherwise say
# nothing. Repeats of an *ongoing* failure are rate-limited rather than
# suppressed -- a problem that persists for a day should not go quiet after its
# first mention.
NTFY_REPEAT_HOURS = float(os.environ.get("NTFY_REPEAT_HOURS", "6"))
# Off by default. Silence cannot distinguish "healthy" from "the timer stopped
# firing", so set this to e.g. 24 to keep one liveness ping a day.
NTFY_HEARTBEAT_HOURS = float(os.environ.get("NTFY_HEARTBEAT_HOURS", "0"))


def _hours_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return float("inf")


def notify_ntfy(summary: list[dict], db_path: str | None = None) -> None:
    """Publish sync *problems* to ntfy. Healthy syncs are silent."""
    ok = [s for s in summary if s["ok"]]
    bad = [s for s in summary if not s["ok"]]
    total_new = sum(s.get("new", 0) for s in ok)
    lines = []
    for s in summary:
        if s["ok"]:
            extra = f", {s['removed']} gone" if s.get("removed") else ""
            lines.append(f"{s['key']}: {s.get('new', 0)} new{extra}")
        else:
            lines.append(f"{s['key']}: FAILED — {s['error'][:70]}")
    body = "\n".join(lines) or "no active searches (all paused)"
    now = datetime.now(timezone.utc).isoformat()

    # Without a database we cannot tell a new failure from a continuing one.
    # Degrade to reporting every failure rather than staying quiet.
    if not db_path:
        if bad:
            _post_ntfy(f"OLX sync: {len(ok)}/{len(summary)} ok, {len(bad)} FAILED",
                       body, "max", "rotating_light")
        return

    with Store(db_path) as store:
        was_failing = store.get_meta("ntfy_state") == "fail"
        if bad:
            if not was_failing or _hours_since(
                    store.get_meta("ntfy_last_fail")) >= NTFY_REPEAT_HOURS:
                _post_ntfy(
                    f"OLX sync: {len(ok)}/{len(summary)} ok, {len(bad)} FAILED",
                    body, "max", "rotating_light")
                store.set_meta("ntfy_last_fail", now)
            store.set_meta("ntfy_state", "fail")
            return

        store.set_meta("ntfy_state", "ok")
        if was_failing:  # close the loop so a fixed problem is known to be fixed
            _post_ntfy(f"OLX sync recovered: {len(ok)} ok, {total_new} new",
                       body, "low", "white_check_mark")
            store.set_meta("ntfy_last_beat", now)
            return

        if NTFY_HEARTBEAT_HOURS and _hours_since(
                store.get_meta("ntfy_last_beat")) >= NTFY_HEARTBEAT_HOURS:
            _post_ntfy(f"OLX sync alive: {len(ok)} ok, {total_new} new",
                       body, "min", "white_check_mark")
            store.set_meta("ntfy_last_beat", now)


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

    fetcher = OlxFetcher(delay=args.delay, jitter=args.jitter)
    push = Push(Path(args.db).resolve().with_name("vapid_key.pem"))

    summary: list[dict] = []
    try:
        failures = _run_all(args, fetcher, push, summary)
    except Exception as exc:  # total, unexpected crash — alert loudly, then exit
        _post_ntfy("OLX sync CRASHED", str(exc)[:200], "max", "rotating_light")
        raise

    notify_ntfy(summary, args.db)
    if failures:
        raise SystemExit(1)


def _run_all(args, fetcher, push, summary: list[dict],
             progress_cb=None) -> int:
    """Run the sync for every active search; return the failure count."""
    failures = 0
    with Store(args.db) as store:
        # Refresh the EUR→RON rate (~once/day) so conversions stay accurate.
        scorer.EUR_TO_RON = fx.refresh(store)
        active_specs = [s for s in load_searches(args.config) if not s.paused]
        total_searches = len(active_specs)
        total_new = 0
        total_deals = 0

        if progress_cb:
            progress_cb({
                "running": True,
                "step": 0,
                "total": total_searches,
                "current_key": "",
                "new_count": 0,
                "deal_count": 0,
                "message": f"Starting sync for {total_searches} active searches...",
            })

        for i, spec in enumerate(active_specs):
            if i:  # gentle gap between searches to avoid burst-throttling
                time.sleep(random.uniform(1.5, 3.5))
            if progress_cb:
                progress_cb({
                    "running": True,
                    "step": i + 1,
                    "total": total_searches,
                    "current_key": spec.key,
                    "new_count": total_new,
                    "deal_count": total_deals,
                    "message": f"Fetching ({i+1}/{total_searches}): {spec.key}",
                })
            started = time.monotonic()
            try:
                # Fetch fully before touching the DB: a mid-pagination failure
                # then aborts this search cleanly (no partial removal-marking).
                listings = fetcher.fetch_all(spec)
            except Exception as exc:  # fail-soft: one search can't kill the rest
                failures += 1
                dur = int((time.monotonic() - started) * 1000)
                store.record_run(spec.key, ok=False, duration_ms=dur, error=str(exc))
                summary.append({"key": spec.key, "ok": False, "error": str(exc)})
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
            summary.append({"key": spec.key, "ok": True,
                            "new": len(result.new), "removed": len(result.removed)})
            total_new += len(result.new)
            sd = score_search(spec.key, active)
            new_deals = [sl for sl in sd.listings
                         if sl.raw["id"] in {l["id"] for l in result.new} and sl.is_deal]
            total_deals += len(new_deals)
            report(result, args.quiet)
            if progress_cb:
                progress_cb({
                    "running": True,
                    "step": i + 1,
                    "total": total_searches,
                    "current_key": spec.key,
                    "new_count": total_new,
                    "deal_count": total_deals,
                    "message": f"Finished {spec.key} (+{len(result.new)} new)",
                })

        if progress_cb:
            progress_cb({
                "running": False,
                "step": total_searches,
                "total": total_searches,
                "current_key": "",
                "new_count": total_new,
                "deal_count": total_deals,
                "message": f"Sync complete: {total_new} new items, {total_deals} deals found",
            })
    return failures


if __name__ == "__main__":
    main()
