"""LLM analysis of OLX listings with Claude.

For a listing we send Claude the title + full description (Romanian), up to
four photos (as URLs), the seller's profile and other active listings, and the
statistical market context our scorer already computes. Claude returns a
structured verdict (scam risk, condition, consistency, score, negotiation tip)
enforced by a Pydantic schema. Verdicts are cached in the ``llm_analysis``
table — each listing is analyzed once unless explicitly re-run.

Requires ``ANTHROPIC_API_KEY`` in the environment (systemd: EnvironmentFile).
"""

from __future__ import annotations

import json
from typing import Any, Literal

import anthropic
import requests
from pydantic import BaseModel, Field

from .fetcher import API_URL, _HEADERS
from .scorer import price_distribution, to_ron

MODEL = "claude-opus-4-8"
MAX_IMAGES = 4

_SYSTEM = """You are an expert analyst of second-hand marketplace listings on \
OLX.ro (Romania). Listings are written in Romanian. You assess a single \
listing for a buyer hunting genuine bargains, using the listing text, its \
photos, the seller's profile, and statistical market context.

Be concrete and evidence-based: cite what you actually see in the photos and \
text. Typical scams on OLX include: prices far below market to lure contact, \
stock/catalog photos instead of the real item, brand-new accounts with a \
single cheap high-value item, vague descriptions, urgency pressure, and \
requests to move off-platform. A very low price with a plausible explanation \
(damage, missing accessories, urgent relocation sale) is NOT automatically a \
scam — judge the whole picture.

Scoring rubric for verdict_score (0-100, buyer's perspective):
80-100 excellent deal, low risk, act fast; 60-79 good deal, minor caveats; \
40-59 fair, nothing special or notable uncertainty; 20-39 poor value or \
significant concerns; 0-19 avoid (likely scam, misleading, or bad value).
Write summary and negotiation_tip in English."""


class Verdict(BaseModel):
    scam_risk: Literal["low", "medium", "high"] = Field(
        description="Likelihood this listing is a scam or bait")
    red_flags: list[str] = Field(
        description="Concrete red flags observed; empty list if none")
    condition_summary: str = Field(
        description="Physical condition as evidenced by photos and text: "
                    "wear, damage, accessories, box, battery health if stated")
    photos_match_description: bool = Field(
        description="False if photos look stock/catalog or contradict the text")
    verdict_score: int = Field(
        description="0-100 overall buyer score per the rubric")
    summary: str = Field(
        description="Two-sentence overall assessment for the buyer")
    negotiation_tip: str = Field(
        description="One actionable negotiation angle grounded in the evidence")


def _seller_context(seller_id: int | None, exclude_id: int) -> dict[str, Any]:
    """One polite OLX call: the seller's other active listings."""
    if not seller_id:
        return {}
    try:
        resp = requests.get(
            API_URL, params=[("offset", "0"), ("limit", "20"),
                             ("user_id", str(seller_id))],
            headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        items = []
        for o in data.get("data") or []:
            if o.get("id") == exclude_id:
                continue
            pr = {p["key"]: p.get("value") for p in o.get("params", [])}
            price = (pr.get("price") or {}).get("label", "?")
            items.append(f"{price} — {o.get('title', '')[:60]}")
        return {
            "other_listings_count": data.get("metadata", {}).get("total_elements"),
            "other_listings_sample": items[:6],
        }
    except Exception:
        return {}  # seller context is best-effort


def _market_context(store, listing: dict[str, Any]) -> dict[str, Any]:
    """Median/quartiles for the listing's search + this listing's position."""
    active = store.active_for_search(listing["search_key"])
    dist = price_distribution(active)
    ron = to_ron(listing.get("price"), listing.get("currency"))
    ctx: dict[str, Any] = {"listing_price_ron": ron,
                           "comparable_listings": len(active)}
    if dist and ron:
        ctx.update({
            "market_median_ron": round(dist["median"]),
            "market_q1_ron": round(dist["q1"]),
            "market_q3_ron": round(dist["q3"]),
            "percent_under_median": round((dist["median"] - ron)
                                          / dist["median"] * 100),
        })
    return ctx


def _build_content(listing: dict[str, Any], market: dict[str, Any],
                   seller: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    try:
        photos = json.loads(listing.get("photos") or "[]")
    except ValueError:
        photos = []
    for url in photos[:MAX_IMAGES]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})

    info = {
        "title": listing.get("title"),
        "description": listing.get("description") or "(no description)",
        "price": f'{listing.get("price")} {listing.get("currency")}',
        "negotiable": bool(listing.get("negotiable")),
        "location": f'{listing.get("city")}, {listing.get("region")}',
        "posted": listing.get("created_time"),
        "seller": {
            "name": listing.get("seller_name"),
            "account_created": listing.get("seller_since"),
            "is_business": bool(listing.get("is_business")),
            **seller,
        },
        "market_context": market,
        "photo_count_total": len(photos),
    }
    content.append({"type": "text", "text":
                    "Analyze this OLX.ro listing:\n\n"
                    + json.dumps(info, ensure_ascii=False, indent=1)})
    return content


def analyze(store, listing: dict[str, Any],
            client: anthropic.Anthropic | None = None) -> dict[str, Any]:
    """Analyze one listing row end-to-end; save and return the verdict dict."""
    client = client or anthropic.Anthropic()
    market = _market_context(store, listing)
    seller = _seller_context(listing.get("seller_id"), listing["id"])

    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_SYSTEM,
        messages=[{"role": "user",
                   "content": _build_content(listing, market, seller)}],
        output_format=Verdict,
    )
    verdict = response.parsed_output.model_dump()
    store.save_analysis(listing["id"], MODEL, verdict)
    return verdict
