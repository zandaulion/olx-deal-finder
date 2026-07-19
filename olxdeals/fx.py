"""Daily EUR→RON exchange rate.

Fetched from the ECB (via Frankfurter), with a fallback source, and cached in
the DB ``meta`` table. The hourly sync calls :func:`refresh`, which only hits
the network when the cached rate is missing or older than a day — so the rate
updates ~once daily with no extra scheduler. Everything that converts EUR→RON
reads ``scorer.EUR_TO_RON``, which callers set from :func:`current`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

DEFAULT_RATE = 4.98  # fallback if never fetched (matches scorer default)

_SOURCES = (
    ("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=RON",
     lambda d: (d.get("rates") or {}).get("RON")),
    ("https://open.er-api.com/v6/latest/EUR",
     lambda d: (d.get("rates") or {}).get("RON")),
)


def fetch_eur_ron() -> float | None:
    """Fetch the current EUR→RON rate; None if all sources fail."""
    for url, extract in _SOURCES:
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "olxdeals/1.0"})
            resp.raise_for_status()
            rate = extract(resp.json())
            if rate and 3.0 < float(rate) < 8.0:  # sanity band for EUR/RON
                return round(float(rate), 4)
        except Exception:
            continue
    return None


def current(store, default: float = DEFAULT_RATE) -> float:
    v = store.get_meta("eur_ron")
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def refresh(store, max_age_hours: float = 20) -> float:
    """Update the cached rate if stale/missing, then return the current rate."""
    ts = store.get_meta("eur_ron_ts")
    fresh = False
    if ts:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(ts)).total_seconds()
            fresh = age < max_age_hours * 3600
        except ValueError:
            fresh = False
    if not fresh:
        rate = fetch_eur_ron()
        if rate:
            store.set_meta("eur_ron", rate)
            store.set_meta("eur_ron_ts", datetime.now(timezone.utc).isoformat())
    return current(store)
