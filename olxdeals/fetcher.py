"""Fetch listings from OLX.ro's public JSON API (/api/v1/offers).

The API returns clean JSON with no auth required. We paginate politely with a
small delay between pages and a hard cap on total pages, then normalise each
raw offer into a flat ``Listing`` dict that the rest of the pipeline consumes.
"""

from __future__ import annotations

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

# OLX honours ~50 offers per page; the API caps a query at 1000 total results.
PAGE_LIMIT = 50
MAX_RESULTS = 1000


@dataclass
class SearchSpec:
    """One configured search — what to track and how to filter it."""

    key: str  # stable identifier, e.g. "iphone_13_used"
    category_id: int = 0
    query: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    # e.g. {"model": ["iphone_13"], "state": ["used"]}
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
            for i, value in enumerate(values):
                params.append((f"filter_enum_{name}[{i}]", str(value)))
        if self.price_from is not None:
            params.append(("filter_float_price:from", str(self.price_from)))
        if self.price_to is not None:
            params.append(("filter_float_price:to", str(self.price_to)))
        if self.region_id is not None:
            params.append(("region_id", str(self.region_id)))
        return params


def _best_photo(offer: dict[str, Any]) -> str | None:
    photos = offer.get("photos") or []
    if not photos:
        return None
    link = photos[0].get("link")
    if not link:
        return None
    # Photo links are templated, e.g. ".../image;s={width}x{height}".
    return link.replace("{width}", "800").replace("{height}", "600")


def normalise(offer: dict[str, Any], search_key: str) -> dict[str, Any]:
    """Flatten a raw OLX offer into the shape the store persists."""
    params = {p["key"]: p.get("value") for p in offer.get("params", [])}
    price = params.get("price") or {}
    model = params.get("model") or {}
    state = params.get("state") or {}
    location = offer.get("location") or {}
    city = (location.get("city") or {}).get("name")
    region = (location.get("region") or {}).get("name")

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
        "photo": _best_photo(offer),
        "created_time": offer.get("created_time"),
        "last_refresh_time": offer.get("last_refresh_time"),
    }


class OlxFetcher:
    """Polite, paginating client for a single OLX search."""

    def __init__(self, delay: float = 1.0, timeout: float = 25.0):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    def iter_offers(self, spec: SearchSpec) -> Iterator[dict[str, Any]]:
        """Yield normalised listings for a search, page by page."""
        offset = 0
        seen = 0
        while offset < MAX_RESULTS:
            params = spec.build_params(offset, PAGE_LIMIT)
            resp = self.session.get(API_URL, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("data") or []
            if not batch:
                break
            for offer in batch:
                yield normalise(offer, spec.key)
                seen += 1
            # Stop when the API says there's no next page.
            if not (payload.get("links") or {}).get("next"):
                break
            offset += PAGE_LIMIT
            time.sleep(self.delay)

    def fetch_all(self, spec: SearchSpec) -> list[dict[str, Any]]:
        return list(self.iter_offers(spec))
