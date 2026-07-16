"""Zero-dependency local dashboard for OLX deals + search management.

Serves a mobile-friendly page built from the SQLite DB, plus a management
page to add/edit/delete searches (written back to searches.yaml) and a
"Sync now" button. The DB and config are read fresh on every request.

    python -m olxdeals.dashboard --db olxdeals.db --config searches.yaml \
        --host 0.0.0.0 --port 8000

Binding to 0.0.0.0 makes it reachable over Tailscale from your phone. Note:
anyone on your tailnet can edit searches and trigger syncs — that's the
intended trust boundary.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .discover import discover
from .scorer import EUR_TO_RON, score_search, to_ron
from .store import Store

_CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0f1115; color:#e6e6e6; }
  header { padding:12px 16px; background:#161a20; position:sticky; top:0;
           border-bottom:1px solid #262c36; z-index:5; }
  header h1 { margin:0; font-size:18px; }
  header .sub { color:#8a93a2; font-size:12px; margin-top:2px; }
  nav { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
  nav a, .btn { display:inline-block; padding:6px 12px; border-radius:8px;
        font-size:13px; text-decoration:none; background:#20252e; color:#cbd3df;
        border:1px solid #2c333f; cursor:pointer; }
  nav a.active { background:#2f5fd0; color:#fff; border-color:#2f5fd0; }
  .btn-go { background:#1e5f3c; color:#c9f5d9; border-color:#1e5f3c; }
  .btn-del { background:#4a1f1f; color:#f0b6b6; border-color:#5a2a2a; }
  .search { padding:10px 16px 4px; font-size:13px; color:#8a93a2; }
  .search b { color:#cbd3df; }
  .menu { margin:10px 16px 0; }
  .menu > summary { list-style:none; cursor:pointer; padding:9px 12px;
          border-radius:8px; background:#20252e; border:1px solid #2c333f;
          color:#e6e6e6; font-size:14px; display:flex; align-items:center; gap:10px; }
  .menu > summary::-webkit-details-marker { display:none; }
  .menu > summary .burger { font-size:16px; color:#8a93a2; }
  .menu > summary .caret { margin-left:auto; color:#8a93a2; transition:transform .15s; }
  .menu[open] > summary { border-color:#2f5fd0; }
  .menu[open] > summary .caret { transform:rotate(180deg); }
  .menu .items { margin-top:6px; border:1px solid #2c333f; border-radius:8px;
          overflow:hidden; }
  .menu .items a { display:block; padding:11px 12px; color:#cbd3df;
          text-decoration:none; border-bottom:1px solid #20252e; font-size:14px; }
  .menu .items a:last-child { border-bottom:none; }
  .menu .items a.on { background:#2f5fd0; color:#fff; }
  .card { display:flex; gap:12px; margin:10px 16px; padding:10px; position:relative;
          background:#161a20; border:1px solid #262c36; border-radius:12px;
          -webkit-touch-callout:none; }
  .hide-btn { position:absolute; top:6px; right:8px; width:26px; height:26px;
          border-radius:50%; background:#20252e; color:#8a93a2; z-index:2;
          display:flex; align-items:center; justify-content:center; font-size:14px;
          border:1px solid #2c333f; }
  .hide-btn:active { background:#4a1f1f; color:#f0b6b6; }
  .card.deal { border-color:#2f7d4f; background:#132018; }
  a.card { text-decoration:none; color:inherit; }
  a.card:active { background:#1c2230; }
  .thumb { width:84px; height:84px; border-radius:8px; object-fit:cover;
           background:#20252e; flex:none; }
  .body { min-width:0; flex:1; }
  .title { font-size:14px; line-height:1.25; margin:0 0 4px;
           display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
           overflow:hidden; }
  .title a { color:#e6e6e6; text-decoration:none; }
  .price { font-size:17px; font-weight:700; }
  .orig { color:#8a93a2; font-size:12px; font-weight:400; }
  .meta { color:#8a93a2; font-size:12px; margin-top:3px; }
  .badge { display:inline-block; font-size:11px; padding:2px 7px; border-radius:20px;
           margin-right:6px; }
  .b-deal { background:#1e5f3c; color:#c9f5d9; }
  .b-dealer { background:#3a2f16; color:#f0dca0; }
  .b-drop { background:#3a1f16; color:#f0b6a0; }
  .b-susp { background:#3a2a3a; color:#e0b6f0; }
  .spark { display:block; margin-top:5px; }
  .chart { margin:6px 16px 14px; padding:10px 6px; background:#161a20;
           border:1px solid #262c36; border-radius:12px; }
  .candles { width:100%; height:auto; display:block; }
  .was { color:#8a93a2; font-size:12px; text-decoration:line-through; }
  .drop-pct { color:#f0b6a0; font-size:12px; font-weight:600; }
  .empty { padding:40px 16px; text-align:center; color:#8a93a2; }
  form.mng { margin:12px 16px; padding:14px; background:#161a20;
             border:1px solid #262c36; border-radius:12px; }
  form.mng label { display:block; font-size:12px; color:#8a93a2; margin:8px 0 3px; }
  form.mng input, form.mng select { width:100%; padding:9px; font-size:15px;
        background:#0f1115; color:#e6e6e6; border:1px solid #2c333f; border-radius:8px; }
  .row2 { display:flex; gap:10px; }
  .row2 > div { flex:1; }
  .srow { display:flex; align-items:center; gap:10px; margin:8px 16px; padding:10px;
          background:#161a20; border:1px solid #262c36; border-radius:10px; }
  .srow .info { flex:1; min-width:0; }
  .srow .k { font-weight:600; }
  .srow .d { color:#8a93a2; font-size:12px; }
  .note { color:#8a93a2; font-size:12px; margin:6px 16px; }
  .flash { margin:10px 16px; padding:10px; border-radius:8px; background:#16321f;
           color:#c9f5d9; border:1px solid #1e5f3c; font-size:13px; }
"""

_SHELL = """<!doctype html>
<html lang="ro"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OLX Deals</title>
<style>{css}</style></head><body>
<header>
  <h1>OLX Deals</h1>
  <div class="sub">{sub}</div>
  <nav>
    <a href="/" class="{deals_active}">Deals</a>
    <a href="/drops" class="{drops_active}">Drops</a>
    <a href="/history" class="{trends_active}">Trends</a>
    <a href="/searches" class="{manage_active}">Manage</a>
    <form method="post" action="/sync" style="margin:0">
      <button class="btn btn-go" type="submit">Sync now</button>
    </form>
  </nav>
</header>
{flash}
{content}
<script>
// On Android, rewrite listing links to open the OLX app (ro.mercador),
// falling back to the web page if the app isn't installed.
if (/Android/i.test(navigator.userAgent)) {{
  document.querySelectorAll('a.card[data-olx]').forEach(function(a) {{
    var u = a.dataset.olx;
    a.href = 'intent://' + u.replace(/^https?:\\/\\//, '') +
      '#Intent;scheme=https;package=ro.mercador;S.browser_fallback_url=' +
      encodeURIComponent(u) + ';end';
    a.removeAttribute('target');
  }});
}}

// Exclude a listing from tracking (persisted server-side).
function excludeId(id, el) {{
  fetch('/exclude', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'id=' + encodeURIComponent(id)
  }}).then(function(r) {{ if (r.ok && el) el.remove(); }});
}}
function askHide(card) {{
  if (!card) return;
  if (confirm('Hide this listing from tracking?\\nIt stays hidden on future ' +
              'syncs — restore it from the Manage tab.'))
    excludeId(card.dataset.id, card);
}}
function hideCard(e, btn) {{
  e.preventDefault(); e.stopPropagation();
  askHide(btn.closest('a.card'));
}}
// Long-press gesture as an alternative to the ✕ button.
document.querySelectorAll('a.card[data-id]').forEach(function(card) {{
  var timer = null, fired = false;
  card.addEventListener('touchstart', function() {{
    fired = false;
    timer = setTimeout(function() {{ fired = true; askHide(card); }}, 550);
  }}, {{passive: true}});
  ['touchend', 'touchmove', 'touchcancel'].forEach(function(ev) {{
    card.addEventListener(ev, function() {{ clearTimeout(timer); }});
  }});
  card.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
  card.addEventListener('click', function(e) {{
    if (fired) {{ e.preventDefault(); fired = false; }}
  }});
}});
</script>
</body></html>"""


def _shell(sub: str, content: str, active: str, flash: str = "") -> str:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return _SHELL.format(
        css=_CSS, sub=sub, content=content, flash=flash_html,
        deals_active="active" if active == "deals" else "",
        drops_active="active" if active == "drops" else "",
        trends_active="active" if active == "trends" else "",
        manage_active="active" if active == "searches" else "",
    )


# ---------- deals page ----------

def _ron_series(history: list | None) -> list[float]:
    """Normalise a price-history series to RON floats, dropping blanks."""
    out: list[float] = []
    for h in history or []:
        v = to_ron(h.get("price"), h.get("currency"))
        if v is not None and v > 0:
            out.append(v)
    return out


def _sparkline(series: list[float], w: int = 130, h: int = 28) -> str:
    """Tiny inline SVG line chart of a price series (>=2 points)."""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1.0
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = 2 + (w - 4) * i / (n - 1)
        y = 2 + (h - 4) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    last, first = series[-1], series[0]
    color = "#5fd08a" if last < first else "#f0b6a0" if last > first else "#8a93a2"
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
            f'points="{" ".join(pts)}"/></svg>')


def _card(sl, history: list | None = None) -> str:
    r = sl.raw
    title = html.escape(r.get("title") or "—")
    url = html.escape(r.get("url") or "#")
    cur = r.get("currency") or ""
    price_txt = f"{sl.price_ron:.0f} RON" if sl.price_ron is not None else "—"
    orig = ""
    if cur == "EUR" and r.get("price") is not None:
        orig = f' <span class="orig">({r["price"]:.0f} EUR)</span>'
    thumb = html.escape(r.get("photo") or "")
    img = (f'<img class="thumb" loading="lazy" src="{thumb}">'
           if thumb else '<div class="thumb"></div>')
    badges = ""
    if sl.is_deal:
        badges += f'<span class="badge b-deal">−{sl.deal_score*100:.0f}% deal</span>'
    elif sl.suspicious:
        badges += (f'<span class="badge b-susp">too cheap? −'
                   f'{sl.deal_score*100:.0f}%</span>')
    if r.get("is_business"):
        badges += '<span class="badge b-dealer">dealer</span>'
    pp = r.get("previous_price")
    if pp is not None and r.get("price") is not None and pp > r["price"]:
        badges += '<span class="badge b-drop">price drop</span>'
    city = html.escape(r.get("city") or "")

    # Price history: sparkline + "was X (−Y%)" when the price has fallen.
    series = _ron_series(history)
    trend = ""
    if len(series) >= 2:
        first, last = series[0], series[-1]
        if last < first:
            pct = (first - last) / first * 100
            trend = (f'<div><span class="was">{first:.0f} RON</span> '
                     f'<span class="drop-pct">−{pct:.0f}%</span></div>')
        trend += _sparkline(series)

    return f"""<a class="card {'deal' if sl.is_deal else ''}" href="{url}"
   target="_blank" rel="noopener" data-olx="{url}" data-id="{r.get('id')}">
  <span class="hide-btn" title="Hide from tracking"
        onclick="hideCard(event, this)">✕</span>
  {img}
  <div class="body">
    <p class="title">{title}</p>
    <div class="price">{price_txt}{orig}</div>
    <div>{badges}</div>
    <div class="meta">{city}</div>
    {trend}
  </div>
</a>"""


def _search_keys(config_path: str, db_path: str) -> list[str]:
    """Configured searches (in file order), plus any leftover DB-only keys."""
    keys = [s.get("key") for s in config.load_raw(config_path).get("searches", [])
            if s.get("key")]
    store = Store(db_path)
    try:
        db_keys = [r["search_key"] for r in store.conn.execute(
            "SELECT DISTINCT search_key FROM listings WHERE active=1")]
    finally:
        store.close()
    for k in db_keys:
        if k not in keys:
            keys.append(k)
    return keys


def _menu(keys: list[str], base: str, selected: str | None) -> str:
    """Hamburger dropdown: current selection + a list of all searches."""
    if not keys:
        return ""
    current = selected if selected in keys else None
    label = html.escape(current) if current else "All searches"
    items = [f'<a href="{base}" class="{"" if current else "on"}">All searches</a>']
    for k in keys:
        on = "on" if k == current else ""
        items.append(
            f'<a href="{base}?search={urllib.parse.quote(k)}" class="{on}">'
            f'{html.escape(k)}</a>')
    return (f'<details class="menu"><summary>'
            f'<span class="burger">&#9776;</span>{label}'
            f'<span class="caret">&#9662;</span></summary>'
            f'<div class="items">{"".join(items)}</div></details>')


def render_deals(db_path: str, config_path: str, selected: str | None = None,
                 flash: str = "") -> str:
    all_keys = _search_keys(config_path, db_path)
    show = [selected] if selected in all_keys else all_keys
    store = Store(db_path)
    try:
        blocks: list[str] = []
        total_deals = 0
        for key in show:
            active = store.active_for_search(key)
            sd = score_search(key, active)
            hist = store.histories([l.raw["id"] for l in sd.listings])
            deals = sd.deals
            total_deals += len(deals)
            med = f"{sd.median:.0f} RON" if sd.median else "—"
            susp = len(sd.suspicious)
            susp_txt = f" · {susp} too-cheap" if susp else ""
            blocks.append(
                f'<div class="search"><b>{html.escape(key)}</b> · '
                f'{len(active)} active · median {med} · '
                f'{len(deals)} deal(s){susp_txt}</div>')
            if not active:
                blocks.append('<div class="note" style="margin:0 16px 8px">'
                              'No active listings — try Sync now.</div>')
                continue
            shown = deals + [l for l in sd.listings if not l.is_deal][:20]
            blocks.append("".join(
                _card(l, hist.get(l.raw["id"])) for l in shown))
        body = "".join(blocks) or \
            '<div class="empty">No searches yet. Add one on Manage, then Sync now.</div>'
        content = _menu(all_keys, "/", selected) + body
        scope = f"'{selected}'" if selected else f"{len(all_keys)} search(es)"
        sub = f"{scope} · {total_deals} deal(s) · EUR→RON {EUR_TO_RON}"
    finally:
        store.close()
    return _shell(sub, content, "deals", flash)


def render_drops(db_path: str, config_path: str, selected: str | None = None,
                 flash: str = "") -> str:
    """Listings whose price has fallen since we first saw them, biggest first.
    Drops that land in deal range are highlighted."""
    all_keys = _search_keys(config_path, db_path)
    show = [selected] if selected in all_keys else all_keys
    store = Store(db_path)
    try:
        cards: list[tuple[float, str]] = []
        for key in show:
            active = store.active_for_search(key)
            sd = score_search(key, active)
            hist = store.histories([l.raw["id"] for l in sd.listings])
            for l in sd.listings:
                series = _ron_series(hist.get(l.raw["id"]))
                if len(series) >= 2 and series[-1] < series[0]:
                    pct = (series[0] - series[-1]) / series[0]
                    cards.append((pct, _card(l, hist.get(l.raw["id"]))))
        cards.sort(key=lambda c: c[0], reverse=True)
        if cards:
            body = "".join(c for _, c in cards)
        else:
            body = ('<div class="empty">No price drops recorded yet.<br>'
                    'Drops appear here once a tracked listing gets cheaper '
                    'between syncs — check back after a day or two.</div>')
        content = _menu(all_keys, "/drops", selected) + body
        scope = f"'{selected}'" if selected else "all searches"
        sub = f"{len(cards)} price drop(s) · {scope}"
    finally:
        store.close()
    return _shell(sub, content, "drops", flash)


# ---------- trends (candlestick) page ----------

def _candlestick(candles: list[dict], w: int = 340, h: int = 210) -> str:
    """SVG candlestick: wick = day min–max, box = Q1–Q3, line = median.
    Colour: green if median fell vs the previous day, red if it rose."""
    if not candles:
        return ('<div class="empty">No trend data yet.<br>One candle is added '
                'per day as syncs run — check back tomorrow.</div>')
    padL, padR, padT, padB = 46, 10, 12, 22
    plot_w, plot_h = w - padL - padR, h - padT - padB
    lows = [c["low"] for c in candles if c["low"] is not None]
    highs = [c["high"] for c in candles if c["high"] is not None]
    if not lows or not highs:
        return '<div class="empty">No priced listings recorded yet.</div>'
    lo, hi = min(lows), max(highs)
    span = (hi - lo) or 1.0

    def y(v: float) -> float:
        return padT + plot_h * (1 - (v - lo) / span)

    n = len(candles)
    slot = plot_w / n
    cw = min(slot * 0.6, 22)
    p: list[str] = []
    for val in (lo, (lo + hi) / 2, hi):
        yy = y(val)
        p.append(f'<line x1="{padL}" y1="{yy:.1f}" x2="{w-padR}" y2="{yy:.1f}" '
                 f'stroke="#262c36"/>')
        p.append(f'<text x="{padL-6}" y="{yy+3:.1f}" text-anchor="end" '
                 f'fill="#8a93a2" font-size="10">{val:.0f}</text>')

    prev = None
    for i, c in enumerate(candles):
        cx = padL + slot * i + slot / 2
        med = c.get("median")
        color = "#8a93a2"
        if prev is not None and med is not None:
            color = ("#5fd08a" if med < prev else
                     "#f0b6a0" if med > prev else "#8a93a2")
        if med is not None:
            prev = med
        if c["low"] is not None and c["high"] is not None:
            p.append(f'<line x1="{cx:.1f}" y1="{y(c["high"]):.1f}" '
                     f'x2="{cx:.1f}" y2="{y(c["low"]):.1f}" stroke="{color}" '
                     f'stroke-width="1.4"/>')
        q1, q3 = c.get("q1"), c.get("q3")
        if q1 is not None and q3 is not None:
            top, bot = y(max(q1, q3)), y(min(q1, q3))
            p.append(f'<rect x="{cx-cw/2:.1f}" y="{top:.1f}" width="{cw:.1f}" '
                     f'height="{max(bot-top,2):.1f}" fill="{color}" '
                     f'fill-opacity="0.25" stroke="{color}"/>')
        if med is not None:
            p.append(f'<line x1="{cx-cw/2:.1f}" y1="{y(med):.1f}" '
                     f'x2="{cx+cw/2:.1f}" y2="{y(med):.1f}" stroke="{color}" '
                     f'stroke-width="2"/>')

    p.append(f'<text x="{padL}" y="{h-6}" fill="#8a93a2" font-size="10">'
             f'{candles[0]["day"][5:]}</text>')
    if n > 1:
        p.append(f'<text x="{w-padR}" y="{h-6}" text-anchor="end" '
                 f'fill="#8a93a2" font-size="10">{candles[-1]["day"][5:]}</text>')
    return (f'<svg class="candles" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(p)}</svg>')


def render_history(db_path: str, config_path: str, selected: str | None = None,
                   flash: str = "") -> str:
    all_keys = _search_keys(config_path, db_path)
    show = [selected] if selected in all_keys else all_keys
    store = Store(db_path)
    try:
        blocks = [
            '<div class="note" style="margin:8px 16px 0">Daily candles · '
            'wick = min–max · box = Q1–Q3 · line = median · '
            'green = cheaper than previous day</div>']
        for key in show:
            candles = store.daily_candles(key)
            blocks.append(
                f'<div class="search"><b>{html.escape(key)}</b> · '
                f'{len(candles)} day(s) tracked</div>')
            if candles:
                last = candles[-1]
                blocks.append(
                    f'<div class="note" style="margin:0 16px 4px">latest: '
                    f'min {last["low"]:.0f} · median {last["median"]:.0f} · '
                    f'max {last["high"]:.0f} RON · {last["n"]} listings</div>')
            blocks.append(f'<div class="chart">{_candlestick(candles)}</div>')
        body = "".join(blocks)
        content = _menu(all_keys, "/history", selected) + body
        sub = "daily min / median / max per search"
    finally:
        store.close()
    return _shell(sub, content, "trends", flash)


# ---------- management page ----------

def _search_summary(s: dict) -> str:
    parts = []
    filters = s.get("filters") or {}
    if filters.get("model"):
        parts.append("model=" + ",".join(filters["model"]))
    if filters.get("state"):
        parts.append("state=" + ",".join(filters["state"]))
    if s.get("query"):
        parts.append(f'query="{s["query"]}"')
    if s.get("price_from") is not None or s.get("price_to") is not None:
        parts.append(f'{s.get("price_from","")}–{s.get("price_to","")} RON')
    parts.append(f'cat {s.get("category_id", 948)}')
    if s.get("region_id"):
        parts.append(f'region {s["region_id"]}')
    return " · ".join(parts)


def render_searches(config_path: str, db_path: str, edit_key: str | None = None,
                    flash: str = "") -> str:
    data = config.load_raw(config_path)
    searches = data.get("searches", [])
    editing = next((s for s in searches if s.get("key") == edit_key), None) if edit_key else None

    def val(key, default=""):
        if not editing:
            return default
        v = editing.get(key)
        return "" if v is None else html.escape(str(v))

    ed_model = ed_state = ""
    if editing:
        f = editing.get("filters") or {}
        ed_model = html.escape((f.get("model") or [""])[0])
        ed_state = (f.get("state") or [""])[0]

    def sel(v):
        return "selected" if ed_state == v else ""

    discover_panel = """<div class="mng">
  <label>Find model key &amp; category id (searches OLX live)</label>
  <div class="row2">
    <input id="dq" placeholder="iphone 15 pro"
           onkeydown="if(event.key==='Enter'){event.preventDefault();doDiscover();}">
    <button type="button" class="btn btn-go" style="flex:none" onclick="doDiscover()">Search</button>
  </div>
  <div id="dres" class="note">Type a phone name, then tap a result to fill the form below.</div>
</div>
<script>
async function doDiscover(){
  const q=document.getElementById('dq').value.trim();
  const box=document.getElementById('dres');
  if(!q){box.textContent='Type something first.';return;}
  box.textContent='Searching OLX…';
  try{
    const r=await fetch('/api/discover?q='+encodeURIComponent(q));
    const d=await r.json();
    let h='';
    if(d.categories.length){h+='<div style="margin:6px 0">Category id (tap): ';
      d.categories.forEach(c=>{h+='<span class="badge b-dealer" style="cursor:pointer" '+
        'onclick="setCat('+c.id+')">'+c.id+' '+c.type+' ×'+c.n+'</span> ';});
      h+='</div>';}
    if(d.models.length){h+='<div>Model (tap): ';
      d.models.forEach(m=>{const lbl=(m.label||m.key).replace(/</g,'');
        h+='<span class="badge b-deal" style="cursor:pointer" '+
        'onclick="setModel(\\''+m.key+'\\')">'+lbl+' ('+m.key+') ×'+m.n+'</span> ';});
      h+='</div>';}
    else{h+='No model filter here — use the free-text query field instead.';}
    box.innerHTML=h||'No results.';
  }catch(e){box.textContent='Error: '+e;}
}
function setCat(id){document.querySelector('[name=category_id]').value=id;}
function setModel(k){
  document.querySelector('[name=model]').value=k;
  const key=document.querySelector('[name=key]');
  if(!key.value && !key.hasAttribute('readonly')) key.value=k+'_used';
}
</script>
"""

    form = discover_panel + f"""<form class="mng" method="post" action="/searches/add">
  <label>Key (unique id)</label>
  <input name="key" value="{val('key')}" placeholder="iphone_15_used" required
         {'readonly' if editing else ''}>
  <label>OLX model key (optional)</label>
  <input name="model" value="{ed_model}" placeholder="iphone_15">
  <div class="row2">
    <div><label>Condition</label>
      <select name="state">
        <option value="" {sel('')}>any</option>
        <option value="used" {sel('used')}>used</option>
        <option value="new" {sel('new')}>new</option>
      </select></div>
    <div><label>Category id (0 = all)</label>
      <input name="category_id" value="{val('category_id','0')}"></div>
  </div>
  <label>Free-text query (optional; use instead of model)</label>
  <input name="query" value="{val('query')}" placeholder="iphone 15 pro max">
  <div class="row2">
    <div><label>Price from (RON)</label>
      <input name="price_from" value="{val('price_from')}" inputmode="numeric"></div>
    <div><label>Price to (RON)</label>
      <input name="price_to" value="{val('price_to')}" inputmode="numeric"></div>
  </div>
  <label>Region id (optional)</label>
  <input name="region_id" value="{val('region_id')}" inputmode="numeric">
  <div style="margin-top:12px; display:flex; gap:10px;">
    <button class="btn btn-go" type="submit">
      {'Save changes' if editing else 'Add search'}</button>
    {'<a class="btn" href="/searches">Cancel</a>' if editing else ''}
  </div>
  <div class="note">Tip: with a model key, leave <b>Category id = 0</b> (all
    categories) — OLX phone categories are per-brand (Apple 948, Samsung 956…),
    so a fixed category can hide results. Use the finder above to get the model key.</div>
</form>"""

    rows = []
    for s in searches:
        key = html.escape(s.get("key", ""))
        rows.append(f"""<div class="srow">
  <div class="info"><div class="k">{key}</div>
    <div class="d">{html.escape(_search_summary(s))}</div></div>
  <a class="btn" href="/searches?edit={urllib.parse.quote(s.get('key',''))}">Edit</a>
  <form method="post" action="/searches/delete" style="margin:0"
        onsubmit="return confirm('Delete {key}?')">
    <input type="hidden" name="key" value="{key}">
    <button class="btn btn-del" type="submit">Delete</button>
  </form>
</div>""")
    listing = "".join(rows) or '<div class="empty">No searches yet.</div>'
    content = form + '<div class="search"><b>Current searches</b></div>' + listing

    # Hidden listings — excluded via ✕ / long-press, restorable here.
    store = Store(db_path)
    try:
        hidden = store.excluded_listings()
    finally:
        store.close()
    if hidden:
        hrows = []
        for h in hidden:
            title = html.escape((h.get("title") or "—")[:60])
            price = f"{h.get('price'):.0f} {h.get('currency') or ''}".strip() \
                if h.get("price") is not None else "—"
            hrows.append(f"""<div class="srow">
  <div class="info"><div class="k">{price}</div>
    <div class="d">{html.escape(h.get('search_key',''))} · {title}</div></div>
  <form method="post" action="/exclude" style="margin:0">
    <input type="hidden" name="id" value="{h.get('id')}">
    <input type="hidden" name="undo" value="1">
    <button class="btn btn-go" type="submit">Restore</button>
  </form>
</div>""")
        content += (f'<div class="search"><b>Hidden from tracking</b> · '
                    f'{len(hidden)} listing(s)</div>' + "".join(hrows))

    sub = f"{len(searches)} search(es) configured"
    return _shell(sub, content, "searches", flash)


# ---------- request handling ----------

_KEY_RE = re.compile(r"[^a-z0-9_]+")


def _slug(s: str) -> str:
    return _KEY_RE.sub("_", s.strip().lower()).strip("_")


def _int_or_none(v: str | None):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def build_search(form: dict[str, str]) -> dict:
    key = _slug(form.get("key", ""))
    if not key:
        raise ValueError("key is required")
    # 0 = all categories; safe default because a model filter is brand-specific
    # on its own (OLX phone categories are per-brand: 948=Apple, 956=Samsung…).
    cat = _int_or_none(form.get("category_id"))
    s: dict = {"key": key, "category_id": cat if cat is not None else 0}
    filters: dict = {}
    model = form.get("model", "").strip()
    if model:
        filters["model"] = [model]
    state = form.get("state", "").strip()
    if state in ("used", "new"):
        filters["state"] = [state]
    if filters:
        s["filters"] = filters
    query = form.get("query", "").strip()
    if query:
        s["query"] = query
    pf, pt = _int_or_none(form.get("price_from")), _int_or_none(form.get("price_to"))
    if pf is not None:
        s["price_from"] = pf
    if pt is not None:
        s["price_to"] = pt
    region = _int_or_none(form.get("region_id"))
    if region is not None:
        s["region_id"] = region
    return s


class Handler(BaseHTTPRequestHandler):
    db_path = "olxdeals.db"
    config_path = "searches.yaml"

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        flash = qs.get("msg", [""])[0]
        selected = qs.get("search", [None])[0]
        if parsed.path in ("/", "/index.html"):
            self._html(render_deals(self.db_path, self.config_path, selected, flash))
        elif parsed.path == "/drops":
            self._html(render_drops(self.db_path, self.config_path, selected, flash))
        elif parsed.path == "/history":
            self._html(render_history(
                self.db_path, self.config_path, selected, flash))
        elif parsed.path == "/searches":
            edit_key = qs.get("edit", [None])[0]
            self._html(render_searches(
                self.config_path, self.db_path, edit_key, flash))
        elif parsed.path == "/api/discover":
            q = qs.get("q", [""])[0].strip()
            cats: list = []
            models: list = []
            if q:
                try:
                    c, m = discover(q, pages=2)
                    cats = [{"id": cid, "type": ctype or "", "n": n}
                            for (cid, ctype), n in c.most_common(6) if cid]
                    models = [{"key": k, "label": lbl or "", "n": n}
                              for (k, lbl), n in m.most_common(12)]
                except Exception:
                    pass
            self._json({"categories": cats, "models": models})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/searches/add":
                search = build_search(self._form())
                config.upsert_search(self.config_path, search)
                self._redirect("/searches?msg=" + urllib.parse.quote(
                    f"Saved '{search['key']}'. Sync now to fetch it."))
            elif parsed.path == "/searches/delete":
                key = self._form().get("key", "")
                config.delete_search(self.config_path, key)
                self._deactivate(key)
                self._redirect("/searches?msg=" + urllib.parse.quote(
                    f"Deleted '{key}'."))
            elif parsed.path == "/exclude":
                form = self._form()
                lid = _int_or_none(form.get("id"))
                undo = bool(form.get("undo"))
                if lid is not None:
                    store = Store(self.db_path)
                    try:
                        store.set_excluded(lid, not undo)
                    finally:
                        store.close()
                if undo:
                    self._redirect("/searches?msg=" + urllib.parse.quote(
                        "Listing restored to tracking."))
                else:
                    self.send_response(204)  # async ✕ / long-press call
                    self.end_headers()
            elif parsed.path == "/sync":
                self._trigger_sync()
                self._redirect("/?msg=" + urllib.parse.quote(
                    "Sync started — refresh in ~30s to see results."))
            else:
                self.send_error(404)
        except Exception as exc:  # surface errors back to the page
            self._redirect("/searches?msg=" + urllib.parse.quote(f"Error: {exc}"))

    def _deactivate(self, key: str) -> None:
        store = Store(self.db_path)
        try:
            store.conn.execute(
                "UPDATE listings SET active=0 WHERE search_key=?", (key,))
            store.conn.commit()
        finally:
            store.close()

    def _trigger_sync(self) -> None:
        # --no-block so the HTTP request returns immediately.
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "olx-sync.service"],
            timeout=10, check=False,
        )

    def log_message(self, *a):
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the OLX deals dashboard")
    ap.add_argument("--db", default="olxdeals.db")
    ap.add_argument("--config", default="searches.yaml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    Handler.db_path = args.db
    Handler.config_path = args.config
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OLX dashboard on http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
