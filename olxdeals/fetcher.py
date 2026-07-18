"""Fetch listings from OLX.ro's public JSON API (/api/v1/offers).

The API returns clean JSON with no auth required. We paginate politely with a
small delay between pages and a hard cap on total pages, then normalise each
raw offer into a flat ``Listing`` dict that the rest of the pipeline consumes.
"""

from __future__ import annotations

import json
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

API_URL = "https://www.olx.ro/api/v1/offers/"

# A realistic desktop UA keeps us on the happy path with OLX's edge.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}

# OLX caps a page at 50 offers (100+ is rejected) and a query at 1000 total.
PAGE_LIMIT = 50
MAX_RESULTS = 1000

# Transient statuses worth backing off and retrying rather than failing.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF = 30.0


@dataclass
class SearchSpec:
    """One configured search — what to track and how to filter it."""

    key: str  # stable identifier, e.g. "iphone_13_used"
    category_id: int = 0
    query: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    # enum filters, e.g. {"model": ["iphone_13"], "petrol": ["diesel"]}
    ranges: dict[str, dict] = field(default_factory=dict)
    # numeric range filters, e.g. {"year": {"from": 2017, "to": 2020},
    #                              "rulaj_pana": {"to": 200000}}
    price_from: int | None = None
    price_to: int | None = None
    region_id: int | None = None

    def build_params(self, offset: int, limit: int) -> list[tuple[str, str]]:
        """Build the flat query params OLX expects (order-insensitive)."""
        params: list[tuple[str, str]] = [
            ("offset", str(offset)),
            ("limit", str(limit)),
            ("category_id", str(self.category_id)),
        ]
        if self.query:
            params.append(("query", self.query))
        for name, values in self.filters.items():
            vals = list(values)
            # OLX silently drops an enum filter given >1 value (returns
            # everything). Keep only the first and warn, so a stray second
            # value can't quietly widen the search to the whole category.
            if len(vals) > 1:
                print(f"[olxdeals] filter '{name}' has multiple values "
                      f"{vals}; OLX ignores multi-value enums — using only "
                      f"'{vals[0]}'.", file=sys.stderr)
                vals = vals[:1]
            for i, value in enumerate(vals):
                params.append((f"filter_enum_{name}[{i}]", str(value)))
        if self.price_from is not None:
            params.append(("filter_float_price:from", str(self.price_from)))
        if self.price_to is not None:
            params.append(("filter_float_price:to", str(self.price_to)))
        for name, bounds in self.ranges.items():
            if bounds.get("from") is not None:
                params.append((f"filter_float_{name}:from", str(bounds["from"])))
            if bounds.get("to") is not None:
                params.append((f"filter_float_{name}:to", str(bounds["to"])))
        if self.region_id is not None:
            params.append(("region_id", str(self.region_id)))
        return params


def _photo_urls(offer: dict[str, Any]) -> list[str]:
    """All photo URLs, resolved from OLX's templated links."""
    urls = []
    for p in offer.get("photos") or []:
        link = p.get("link")
        if link:
            # Photo links are templated, e.g. ".../image;s={width}x{height}".
            urls.append(link.replace("{width}", "800").replace("{height}", "600"))
    return urls


def normalise(offer: dict[str, Any], search_key: str) -> dict[str, Any]:
    """Flatten a raw OLX offer into the shape the store persists."""
    params = {p["key"]: p.get("value") for p in offer.get("params", [])}
    price = params.get("price") or {}
    model = params.get("model") or {}
    state = params.get("state") or {}
    location = offer.get("location") or {}
    city = (location.get("city") or {}).get("name")
    region = (location.get("region") or {}).get("name")
    seller = offer.get("user") or {}
    photos = _photo_urls(offer)

    return {
        "id": offer["id"],
        "search_key": search_key,
        "url": offer.get("url"),
        "title": offer.get("title"),
        "price": price.get("value"),
        "currency": price.get("currency"),
        "negotiable": bool(price.get("negotiable")),
        "previous_price": price.get("previous_value"),
        "model": model.get("key") if isinstance(model, dict) else None,
        "state": state.get("key") if isinstance(state, dict) else None,
        "city": city,
        "region": region,
        "is_business": bool((offer.get("business")) or False),
        "photo": photos[0] if photos else None,
        "photos": json.dumps(photos) if photos else None,
        "description": offer.get("description"),
        "seller_id": seller.get("id"),
        "seller_name": seller.get("name"),
        "seller_since": seller.get("created"),
        "created_time": offer.get("created_time"),
        "last_refresh_time": offer.get("last_refresh_time"),
    }


class OlxFetcher:
    """Polite, paginating client for a single OLX search."""

    def __init__(self, delay: float = 1.0, jitter: float = 0.5,
                 timeout: float = 25.0, max_retries: int = 3):
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped."""
        return min(2.0 ** attempt, MAX_BACKOFF) + random.uniform(0, self.jitter)

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        ra = resp.headers.get("Retry-After")
        if ra and ra.strip().isdigit():
            return float(ra.strip())
        return None

    def _get(self, params: list[tuple[str, str]]) -> dict[str, Any]:
        """GET one page, retrying transient failures with backoff."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(API_URL, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = self._retry_after(resp)
                time.sleep(wait if wait is not None else self._backoff(attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:  # pragma: no cover - defensive
            raise last_exc
        raise RuntimeError("exhausted retries without a response")

    def iter_offers(self, spec: SearchSpec) -> Iterator[dict[str, Any]]:
        """Yield normalised listings for a search, page by page."""
        offset = 0
        while offset < MAX_RESULTS:
            payload = self._get(spec.build_params(offset, PAGE_LIMIT))
            batch = payload.get("data") or []
            if not batch:
                break
            for offer in batch:
                yield normalise(offer, spec.key)
            # Stop when the API says there's no next page.
            if not (payload.get("links") or {}).get("next"):
                break
            offset += PAGE_LIMIT
            time.sleep(self.delay + random.uniform(0, self.jitter))

    def fetch_all(self, spec: SearchSpec) -> list[dict[str, Any]]:
        return list(self.iter_offers(spec))
