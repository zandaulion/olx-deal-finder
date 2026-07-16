"""Turn raw listings into scored deals.

Three things make a naive "below median = deal" useless on OLX:
  1. Prices come in mixed currencies (EUR and RON) -> normalise to RON.
  2. Broken / parts / accessory listings drag the median down and masquerade
     as deals -> drop them from the distribution (keyword filter).
  3. Suspiciously-cheap listings are usually the WRONG model, broken, or a
     scam rather than a bargain (e.g. a Flip 6 in Fold 6 results) -> a price
     floor separates "too cheap to be real" from a genuine deal.

A deal must sit in a believable band: clearly below the typical price for its
model+condition, but not so low it's implausible. ``deal_score`` is how far
below the clean median it sits (0.25 == 25% under).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable

# Rough EUR->RON; refresh occasionally. Kept here so it's easy to config later.
EUR_TO_RON = 4.98

# Title words that mark a listing as not a clean, working phone.
JUNK_KEYWORDS = (
    "defect", "defecta", "piese", "piesa", "dezmembr", "display",
    "carcasa", "carcasă", "doar ecran", "doar sticla", "spart", "sparta",
    "husa", "husă", "folie", "incarcator", "încărcător", "cablu",
    "nefunctional", "nefuncțional", "pentru piese", "placa de baza",
)

DEAL_THRESHOLD = 0.20     # must be >=20% under the clean median
MIN_PRICE_RATIO = 0.60    # below 60% of median -> "too cheap", likely not real
MIN_SAMPLE = 5            # need at least this many clean prices to judge deals


def to_ron(price: float | None, currency: str | None) -> float | None:
    if price is None:
        return None
    if currency == "EUR":
        return round(price * EUR_TO_RON, 0)
    return float(price)


def is_junk(title: str | None) -> bool:
    if not title:
        return False
    low = title.lower()
    return any(kw in low for kw in JUNK_KEYWORDS)


@dataclass
class ScoredListing:
    raw: dict[str, Any]
    price_ron: float | None
    deal_score: float   # fraction under median; higher = better deal
    is_deal: bool
    suspicious: bool    # implausibly cheap: wrong model / broken / scam
    junk: bool          # keyword-flagged (parts/accessory/broken)


@dataclass
class SearchDeals:
    search_key: str
    median: float | None
    q1: float | None
    sample_size: int    # clean listings used for the distribution
    listings: list[ScoredListing]

    @property
    def deals(self) -> list[ScoredListing]:
        return [l for l in self.listings if l.is_deal]

    @property
    def suspicious(self) -> list[ScoredListing]:
        return [l for l in self.listings if l.suspicious]


def score_search(search_key: str, listings: Iterable[dict[str, Any]],
                 threshold: float = DEAL_THRESHOLD) -> SearchDeals:
    """Score every listing in one search against its clean-price distribution."""
    items = list(listings)
    clean_prices: list[float] = []
    for it in items:
        ron = to_ron(it.get("price"), it.get("currency"))
        if ron is not None and ron > 0 and not is_junk(it.get("title")):
            clean_prices.append(ron)

    n = len(clean_prices)
    median = statistics.median(clean_prices) if n else None
    # First quartile: a genuine deal should be at/below where the cheap
    # quarter of the market sits, not merely a hair under the median.
    if n >= 4:
        q1 = statistics.quantiles(clean_prices, n=4)[0]
    else:
        q1 = median

    can_judge = n >= MIN_SAMPLE and median is not None
    deal_ceiling = min(median * (1 - threshold), q1) if can_judge else None
    floor = median * MIN_PRICE_RATIO if median else None

    scored: list[ScoredListing] = []
    for it in items:
        ron = to_ron(it.get("price"), it.get("currency"))
        junk = is_junk(it.get("title"))
        score = (median - ron) / median if (median and ron and ron > 0) else 0.0

        suspicious = bool(
            not junk and ron and floor is not None and ron < floor
        )
        is_deal = bool(
            can_judge and not junk and ron
            and floor <= ron <= deal_ceiling  # type: ignore[operator]
        )
        scored.append(ScoredListing(
            raw=it, price_ron=ron, deal_score=score,
            is_deal=is_deal, suspicious=suspicious, junk=junk,
        ))

    # Deals first (best score first), then everything else by score.
    scored.sort(key=lambda l: (l.is_deal, l.deal_score), reverse=True)
    return SearchDeals(search_key, median, q1, n, scored)
