"""Market Velocity & "Best Time to Buy" Analytics for OLX Deal Finder.

Calculates key economic and market trends for tracked search categories:
  1. Price Velocity (7-day / 14-day median trajectory and momentum).
  2. Inventory Liquidity & Days on Market (average time before a listing sells or is removed).
  3. Price Cut Frequency & Discount Depth (% of listings with recorded price drops).
  4. Supply Flow (New listings volume per week).
  5. Actionable "Best Time to Buy" Score & Verdict Gauge (Buyer's Market vs Seller's Market).
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import scorer

if TYPE_CHECKING:
    from .store import Store


@dataclass
class MarketAnalytics:
    search_key: str
    current_median: float | None
    median_7d_ago: float | None
    price_change_7d_pct: float | None
    price_direction: str          # "dropping_fast", "dropping", "stable", "rising", "rising_fast"
    avg_days_on_market: float | None
    liquidity_level: str          # "fast", "moderate", "slow"
    liquidity_label: str
    active_count: int
    total_deals: int
    deals_ratio_pct: float
    price_drop_count: int
    price_drop_rate_pct: float    # % of active listings with at least one price cut
    avg_price_drop_ron: float
    new_last_7d: int
    recommendation_score: int     # 0 to 100
    verdict: str                  # "GREAT TIME TO BUY", "FAIR / BALANCED", "SELLER'S MARKET"
    verdict_badge: str            # "good", "fair", "caution"
    advice_text: str


def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def compute_market_analytics(store: Store, search_key: str) -> MarketAnalytics:
    """Analyze historical prices, listing lifespans, and price drops for a search."""
    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    # 1. Price trend from daily candlestick stats
    candles = store.daily_candles(search_key)
    current_median = None
    median_7d_ago = None
    price_change_7d_pct = None
    price_direction = "stable"

    if candles:
        valid_candles = [c for c in candles if c.get("median") is not None and c["median"] > 0]
        if valid_candles:
            current_median = valid_candles[-1]["median"]
            # Look for candle ~7 days ago
            today_dt = _parse_iso(valid_candles[-1]["day"])
            if today_dt:
                best_7d_candle = None
                for c in reversed(valid_candles[:-1]):
                    c_dt = _parse_iso(c["day"])
                    if c_dt:
                        diff_days = (today_dt - c_dt).total_seconds() / 86400.0
                        if diff_days >= 5:
                            best_7d_candle = c
                            break
                if not best_7d_candle and len(valid_candles) >= 2:
                    best_7d_candle = valid_candles[0]

                if best_7d_candle and best_7d_candle.get("median"):
                    median_7d_ago = best_7d_candle["median"]
                    if median_7d_ago and median_7d_ago > 0:
                        diff = current_median - median_7d_ago
                        price_change_7d_pct = round((diff / median_7d_ago) * 100.0, 1)

    if price_change_7d_pct is not None:
        if price_change_7d_pct <= -4.0:
            price_direction = "dropping_fast" if price_change_7d_pct <= -7.0 else "dropping"
        elif price_change_7d_pct >= 4.0:
            price_direction = "rising_fast" if price_change_7d_pct >= 7.0 else "rising"
        else:
            price_direction = "stable"

    # 2. Active listings and deal stats
    active_rows = store.active_for_search(search_key)
    scored = scorer.score_search(search_key, active_rows)
    active_count = len(active_rows)
    total_deals = len(scored.deals)
    deals_ratio_pct = round((total_deals / active_count * 100.0), 1) if active_count else 0.0

    if current_median is None and scored.median:
        current_median = scored.median

    # 3. Price drops activity among active listings
    active_ids = [r["id"] for r in active_rows]
    histories = store.histories(active_ids)
    price_drop_count = 0
    drop_amounts: list[float] = []

    for lid, history in histories.items():
        ron_series = []
        for h in history:
            v = scorer.to_ron(h.get("price"), h.get("currency"))
            if v and v > 0:
                ron_series.append(v)
        if len(ron_series) >= 2:
            first_p, last_p = ron_series[0], ron_series[-1]
            if last_p < first_p:
                price_drop_count += 1
                drop_amounts.append(first_p - last_p)

    price_drop_rate_pct = round((price_drop_count / active_count * 100.0), 1) if active_count else 0.0
    avg_price_drop_ron = round(statistics.mean(drop_amounts), 0) if drop_amounts else 0.0

    # 4. Inventory Turnover / Average Days on Market
    # Look at recent inactive listings (sold or removed) as well as active listings
    recent_listings = store.conn.execute(
        "SELECT first_seen, last_seen, active FROM listings "
        "WHERE search_key = ? AND first_seen >= ? ORDER BY last_seen DESC LIMIT 150",
        (search_key, cutoff_30d),
    ).fetchall()

    days_on_market_list: list[float] = []
    new_last_7d = 0

    for row in recent_listings:
        t_first = _parse_iso(row["first_seen"])
        t_last = _parse_iso(row["last_seen"])
        if t_first:
            if t_first >= now - timedelta(days=7):
                new_last_7d += 1
            if t_last and t_last >= t_first:
                dom = (t_last - t_first).total_seconds() / 86400.0
                # Discard < 2h glitches
                if dom >= 0.08:
                    days_on_market_list.append(dom)

    avg_dom = round(statistics.median(days_on_market_list), 1) if days_on_market_list else None

    if avg_dom is not None:
        if avg_dom <= 2.5:
            liquidity_level = "fast"
            liquidity_label = f"High Liquidity ({avg_dom:.1f}d avg) · Listings sell quickly"
        elif avg_dom <= 8.0:
            liquidity_level = "moderate"
            liquidity_label = f"Balanced Pace ({avg_dom:.1f}d avg on market)"
        else:
            liquidity_level = "slow"
            liquidity_label = f"High Buyer Leverage ({avg_dom:.1f}d avg) · Sellers holding stock"
    else:
        liquidity_level = "moderate"
        liquidity_label = "Steady Market Pace"

    # 5. Calculate "Best Time to Buy" Score (0 to 100)
    score = 50

    # Price momentum impact (+/- 25)
    if price_change_7d_pct is not None:
        if price_change_7d_pct <= -6.0:
            score += 24
        elif price_change_7d_pct <= -2.0:
            score += 14
        elif price_change_7d_pct >= 6.0:
            score -= 22
        elif price_change_7d_pct >= 2.0:
            score -= 12

    # Price drops frequency impact (+/- 15)
    if price_drop_rate_pct >= 25.0:
        score += 15
    elif price_drop_rate_pct >= 12.0:
        score += 8
    elif price_drop_rate_pct == 0 and active_count >= 10:
        score -= 6

    # Deal density impact (+/- 15)
    if deals_ratio_pct >= 18.0:
        score += 15
    elif deals_ratio_pct >= 8.0:
        score += 8
    elif deals_ratio_pct == 0 and active_count >= 10:
        score -= 8

    # Market leverage impact (+/- 10)
    if liquidity_level == "slow":
        score += 8
    elif liquidity_level == "fast" and price_direction in ("rising", "rising_fast"):
        score -= 10

    score = max(12, min(96, score))

    # Verdict & Advice synthesis
    if score >= 70:
        verdict = "BUYER'S MARKET (Great Time to Buy)"
        verdict_badge = "good"
        chg_txt = f"prices softened by {abs(price_change_7d_pct):.1f}%" if price_change_7d_pct and price_change_7d_pct < 0 else "prices are competitive"
        drop_txt = f"{price_drop_rate_pct:.0f}% of sellers have lowered prices (avg drop −{avg_price_drop_ron:.0f} RON)" if price_drop_count else "deals are active"
        advice_text = f"Favorable buying conditions: {chg_txt}, and {drop_txt}. You have solid negotiation leverage."
    elif score >= 45:
        verdict = "BALANCED MARKET (Fair Time to Buy)"
        verdict_badge = "fair"
        advice_text = f"Market is steady with {active_count} active listings. Look for items marked with genuine deal badges rather than general price trends."
    else:
        verdict = "SELLER'S MARKET (Wait or Pick Selectively)"
        verdict_badge = "caution"
        advice_text = "High demand or limited supply is putting upward pressure on prices. Verify listings with AI inspection or wait for fresh incoming stock."

    return MarketAnalytics(
        search_key=search_key,
        current_median=current_median,
        median_7d_ago=median_7d_ago,
        price_change_7d_pct=price_change_7d_pct,
        price_direction=price_direction,
        avg_days_on_market=avg_dom,
        liquidity_level=liquidity_level,
        liquidity_label=liquidity_label,
        active_count=active_count,
        total_deals=total_deals,
        deals_ratio_pct=deals_ratio_pct,
        price_drop_count=price_drop_count,
        price_drop_rate_pct=price_drop_rate_pct,
        avg_price_drop_ron=avg_price_drop_ron,
        new_last_7d=new_last_7d,
        recommendation_score=score,
        verdict=verdict,
        verdict_badge=verdict_badge,
        advice_text=advice_text,
    )


def render_market_card(a: MarketAnalytics) -> str:
    """Render a modern visual Market Velocity & Time to Buy analysis widget."""
    med_txt = f"{a.current_median:,.0f} RON".replace(",", ".") if a.current_median else "—"

    # Price trend pill
    if a.price_change_7d_pct is not None:
        if a.price_change_7d_pct < 0:
            trend_pill = f'<span class="badge b-deal" style="font-size:12px">📉 {a.price_change_7d_pct:+.1f}% 7-day</span>'
        elif a.price_change_7d_pct > 0:
            trend_pill = f'<span class="badge b-susp" style="font-size:12px">📈 {a.price_change_7d_pct:+.1f}% 7-day</span>'
        else:
            trend_pill = '<span class="badge" style="font-size:12px;background:rgba(255,255,255,0.1)">⚖ 0.0% 7-day</span>'
    else:
        trend_pill = '<span class="badge" style="font-size:12px;background:rgba(255,255,255,0.08)">⏳ Accumulating data</span>'

    # Verdict badge class
    v_class = "ai-good" if a.verdict_badge == "good" else "ai-mid" if a.verdict_badge == "fair" else "ai-bad"
    score_bar_color = "#10b981" if a.verdict_badge == "good" else "#f59e0b" if a.verdict_badge == "fair" else "#f43f5e"

    dom_txt = f"{a.avg_days_on_market:.1f} days" if a.avg_days_on_market else "—"
    drops_txt = f"{a.price_drop_rate_pct:.0f}% ({a.price_drop_count} items)" if a.price_drop_count else "0%"
    avg_cut_txt = f"−{a.avg_price_drop_ron:,.0f} RON".replace(",", ".") if a.avg_price_drop_ron else "—"

    return f"""<div class="mng-box" style="margin:12px 0 16px;border-left:4px solid {score_bar_color}">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
    <div>
      <div style="font-size:11px;font-weight:700;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.5px">Market Intelligence</div>
      <h2 style="margin:2px 0 0;font-size:17px;display:flex;align-items:center;gap:8px">
        {html.escape(a.search_key)}
        {trend_pill}
      </h2>
    </div>
    <div style="text-align:right">
      <span class="badge {v_class}" style="font-size:12px;padding:4px 10px">
        {a.verdict} ({a.recommendation_score}/100)
      </span>
    </div>
  </div>

  <div style="background:var(--bg-surface-hover);border-radius:var(--radius-pill);height:6px;overflow:hidden;margin-bottom:12px">
    <div style="background:{score_bar_color};height:100%;width:{a.recommendation_score}%;border-radius:var(--radius-pill);transition:width 0.5s ease"></div>
  </div>

  <p style="font-size:12.5px;color:var(--text-primary);line-height:1.5;margin-bottom:12px">
    💡 {html.escape(a.advice_text)}
  </p>

  <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:8px">
    <div style="background:var(--bg-surface-elevated);padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-subtle)">
      <div style="font-size:10.5px;color:var(--text-tertiary)">Current Median</div>
      <div style="font-size:14px;font-weight:700;font-family:var(--font-mono);margin-top:2px">{med_txt}</div>
    </div>
    <div style="background:var(--bg-surface-elevated);padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-subtle)">
      <div style="font-size:10.5px;color:var(--text-tertiary)">Avg Time on Market</div>
      <div style="font-size:14px;font-weight:700;margin-top:2px">{dom_txt}</div>
    </div>
    <div style="background:var(--bg-surface-elevated);padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-subtle)">
      <div style="font-size:10.5px;color:var(--text-tertiary)">Price Cut Rate</div>
      <div style="font-size:14px;font-weight:700;margin-top:2px">{drops_txt}</div>
    </div>
    <div style="background:var(--bg-surface-elevated);padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-subtle)">
      <div style="font-size:10.5px;color:var(--text-tertiary)">Avg Price Cut</div>
      <div style="font-size:14px;font-weight:700;font-family:var(--font-mono);margin-top:2px">{avg_cut_txt}</div>
    </div>
    <div style="background:var(--bg-surface-elevated);padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-subtle)">
      <div style="font-size:10.5px;color:var(--text-tertiary)">New (Last 7 Days)</div>
      <div style="font-size:14px;font-weight:700;margin-top:2px">+{a.new_last_7d} listings</div>
    </div>
  </div>
</div>"""


def render_mini_market_chip(a: MarketAnalytics) -> str:
    """Render a compact market velocity pill for top headers or category cards."""
    color = "var(--accent-deal)" if a.verdict_badge == "good" else "var(--accent-drop)" if a.verdict_badge == "fair" else "var(--accent-susp)"
    bg = "var(--accent-deal-bg)" if a.verdict_badge == "good" else "var(--accent-drop-bg)" if a.verdict_badge == "fair" else "var(--accent-susp-bg)"
    arrow = "↘" if (a.price_change_7d_pct and a.price_change_7d_pct < 0) else "↗" if (a.price_change_7d_pct and a.price_change_7d_pct > 0) else "⚖"
    pct_txt = f"{a.price_change_7d_pct:+.1f}% 7d" if a.price_change_7d_pct is not None else "stable"
    return (f'<span class="chip" style="background:{bg};color:{color};border-color:{color};font-weight:600">'
            f'{arrow} {a.verdict.split("(")[0].strip()} · {pct_txt}</span>')
