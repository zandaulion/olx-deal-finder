"""LLM analysis of OLX listings with Gemini Flash 3.8.

For a listing we send Gemini the title + full description (Romanian), up to
four photos, the seller's profile and other active listings, and the
statistical market context our scorer already computes. Gemini returns a
structured verdict (scam risk, condition, consistency, score, negotiation tip)
enforced by a Pydantic schema. Verdicts are cached in the ``llm_analysis``
table — each listing is analyzed once unless explicitly re-run.

Requires ``GEMINI_API_KEY`` in the environment (systemd: EnvironmentFile).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal

from curl_cffi import requests  # browser-TLS client (OLX blocks plain requests)
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .fetcher import API_URL, IMPERSONATE, _HEADERS
from .scorer import price_distribution, to_ron

MODEL = "gemini-3.8-flash"
MAX_IMAGES = 4

# Gemini 3.8 Flash pricing (USD per token). Thinking tokens are billed as output.
PRICE_IN = 0.75 / 1_000_000
PRICE_OUT = 3.75 / 1_000_000


def cost_usd(input_tokens: int | None, output_tokens: int | None) -> float:
    return (input_tokens or 0) * PRICE_IN + (output_tokens or 0) * PRICE_OUT

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

Your own knowledge of product line-ups has a training cutoff and is very \
likely behind the market. A phone, console or car released after it will look \
unfamiliar to you, and an unfamiliar model name is not evidence of a fake. \
Never state that a product does not exist, is not an official release, or is \
not in a manufacturer's line-up: use the search tool to check first, and if \
you still cannot confirm it, say the name is unverified and judge the listing \
on what the photos and text actually show.

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
            headers=_HEADERS, impersonate=IMPERSONATE, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        items = []
        for o in data.get("data") or []:
            if o.get("id") == exclude_id:
                continue
            pr = {p["key"]: p.get("value") for p in o.get("params", [])}
            price = (pr.get("price") or {}).get("label", "?")
            title = o.get("title", "")[:60]
            items.append(f"{price} — {title}")
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


def _fetch_image_part(url: str) -> types.Part | None:
    """Download an image via curl_cffi and wrap it in a Gemini Part."""
    try:
        resp = requests.get(url, headers=_HEADERS, impersonate=IMPERSONATE, timeout=10)
        if resp.status_code == 200 and resp.content:
            mime = resp.headers.get("content-type") or "image/jpeg"
            mime = mime.split(";")[0].strip() or "image/jpeg"
            return types.Part.from_bytes(data=resp.content, mime_type=mime)
    except Exception:
        pass
    return None


def _build_content(listing: dict[str, Any], market: dict[str, Any],
                   seller: dict[str, Any]) -> list[Any]:
    parts: list[Any] = []
    try:
        photos = json.loads(listing.get("photos") or "[]")
    except ValueError:
        photos = []
    for url in photos[:MAX_IMAGES]:
        part = _fetch_image_part(url)
        if part is not None:
            parts.append(part)

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
    prompt_text = (
        "Analyze this OLX.ro listing:\n\n"
        + json.dumps(info, ensure_ascii=False, indent=1)
    )
    parts.append(types.Part.from_text(text=prompt_text))
    return parts


def analyze(store, listing: dict[str, Any],
            client: genai.Client | None = None) -> dict[str, Any]:
    """Analyze one listing row end-to-end; save and return the verdict dict."""
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        client = genai.Client(api_key=api_key) if api_key else genai.Client()
    market = _market_context(store, listing)
    seller = _seller_context(listing.get("seller_id"), listing["id"])

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=_build_content(listing, market, seller),
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    response_mime_type="application/json",
                    response_schema=Verdict,
                    max_output_tokens=16000,
                    # Grounding, because the model's product knowledge is older
                    # than the market it is judging. Without it a genuine phone
                    # released after the cutoff reads as a counterfeit: the
                    # Galaxy S25 Edge was written off as "does not exist in
                    # Samsung's lineup" and its real dual-camera bump cited as
                    # proof of a clone. Search fixes the facts; the paragraph
                    # in _SYSTEM covers what search cannot settle.
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            break
        except Exception as exc:
            if attempt < 2 and any(k in str(exc) for k in ("503", "429", "UNAVAILABLE")):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    if response.parsed is not None:
        verdict = response.parsed.model_dump()
    else:
        verdict = Verdict.model_validate_json(response.text).model_dump()

    u = response.usage_metadata
    in_tokens = u.prompt_token_count if u else 0
    out_tokens = ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0
    usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}
    store.save_analysis(listing["id"], MODEL, verdict, usage,
                        cost_usd(in_tokens, out_tokens))
    return verdict
