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
import mimetypes
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).parent / "static"

# --- Progressive Web App: manifest + service worker (installable full-screen) ---
MANIFEST = {
    "name": "OLX Deals",
    "short_name": "OLX Deals",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1115",
    "theme_color": "#0f1115",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any maskable"},
        {"src": "/static/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
}

# Network-first so live data stays fresh; falls back to cache (offline shell).
SW_JS = """
const CACHE = 'olx-deals-v3';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/', '/manifest.webmanifest'])));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  e.respondWith(
    fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
      return res;
    }).catch(() => caches.match(req).then((r) => r || caches.match('/')))
  );
});
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || 'OLX Deals', {
    body: d.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.tag || 'olx-deal',
    data: { url: d.url || '/' },
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({ type: 'window' }).then((wins) => {
    for (const w of wins) { if ('focus' in w) { w.navigate(url); return w.focus(); } }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
"""

from . import config
from .discover import discover
from .push import Push
from .scorer import EUR_TO_RON, score_search, to_ron
from .store import Store

_CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0f1115; color:#e6e6e6;
         padding-bottom:calc(60px + env(safe-area-inset-bottom)); }
  /* Centered, width-capped column so it doesn't stretch on desktop. */
  .wrap { max-width:1200px; margin:0 auto; width:100%; }
  header { padding:10px 16px; background:#161a20; position:sticky; top:0;
           border-bottom:1px solid #262c36; z-index:5;
           padding-top:calc(10px + env(safe-area-inset-top)); }
  header h1 { margin:0; font-size:18px; }
  header .sub { color:#8a93a2; font-size:12px; margin-top:3px; }
  .topbar { display:flex; align-items:center; justify-content:space-between; }
  .topbar form { margin:0; }
  .actions { display:flex; gap:8px; }
  .iconbtn { background:#20252e; border:1px solid #2c333f; color:#cbd3df;
        width:40px; height:40px; border-radius:10px; font-size:19px; cursor:pointer;
        display:flex; align-items:center; justify-content:center; padding:0; }
  .iconbtn:active { background:#2c333f; }
  .btn { display:inline-block; padding:6px 12px; border-radius:8px;
        font-size:13px; text-decoration:none; background:#20252e; color:#cbd3df;
        border:1px solid #2c333f; cursor:pointer; }
  .btn-go { background:#1e5f3c; color:#c9f5d9; border-color:#1e5f3c; }
  .btn-del { background:#4a1f1f; color:#f0b6b6; border-color:#5a2a2a; }
  .tabbar { position:fixed; left:0; right:0; bottom:0; z-index:10;
        background:#161a20; border-top:1px solid #262c36;
        padding-bottom:env(safe-area-inset-bottom); }
  .tabbar .wrap { display:flex; }
  .tabbar a { flex:1; display:flex; flex-direction:column; align-items:center;
        gap:2px; padding:7px 2px 8px; color:#8a93a2; text-decoration:none;
        font-size:10px; }
  /* Card list: single column on mobile, responsive grid on wider screens. */
  @media (min-width:760px) {
    .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
             gap:12px; padding:4px 16px; }
    .cards .card { margin:0; }
    .card .thumb { width:96px; height:96px; }
    /* Single-column UI shouldn't stretch across the full grid width. */
    .chart { max-width:620px; }
    .mng, .srow { max-width:660px; }
  }
  .tabbar a .ic { font-size:19px; line-height:1.1; }
  .tabbar a.active { color:#4f8bff; }
  .filterbox { margin:8px 16px 0; }
  .filterbox > summary { list-style:none; cursor:pointer; color:#8a93a2;
        font-size:13px; padding:5px 0; display:flex; align-items:center; gap:6px; }
  .filterbox > summary::-webkit-details-marker { display:none; }
  .filterbox > summary::before { content:'\\25B8'; font-size:11px; }
  .filterbox[open] > summary::before { content:'\\25BE'; }
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
  .menu .items a { display:flex; justify-content:space-between; align-items:center;
          gap:10px; padding:11px 12px; color:#cbd3df; text-decoration:none;
          border-bottom:1px solid #20252e; font-size:14px; }
  .menu .items a:last-child { border-bottom:none; }
  .menu .items a.on { background:#2f5fd0; color:#fff; }
  .menu .items a .stat { color:#8a93a2; font-size:12px; white-space:nowrap; }
  .menu .items a.on .stat { color:#cdd9ff; }
  .menu .sstat { color:#8a93a2; font-size:12px; margin-left:6px; }
  .menu .dl { color:#5fd08a; }
  .menu .grp { border-bottom:1px solid #20252e; }
  .menu .grp > summary { list-style:none; cursor:pointer; display:flex;
        align-items:center; gap:10px; padding:11px 12px; font-size:14px; }
  .menu .grp > summary::-webkit-details-marker { display:none; }
  .menu .grp-name { color:#e6e6e6; text-decoration:none; font-weight:600; flex:1; }
  .menu .grp-name.on { color:#4f8bff; }
  .menu .grp-caret { color:#8a93a2; font-size:11px; transition:transform .15s; }
  .menu .grp[open] > summary .grp-caret { transform:rotate(90deg); }
  .menu .grp-items a { padding-left:28px; background:#0f1115; }
  .card { display:flex; gap:12px; margin:10px 16px; padding:10px; position:relative;
          background:#161a20; border:1px solid #262c36; border-radius:12px;
          -webkit-touch-callout:none; touch-action:pan-y; }
  .card.sw-fav { box-shadow:inset 7px 0 0 -2px #f0c040; }
  .card.sw-seen { box-shadow:inset -7px 0 0 -2px #4f8bff; }
  .hide-btn { position:absolute; top:6px; right:8px; width:26px; height:26px;
          border-radius:50%; background:#20252e; color:#8a93a2; z-index:2;
          display:flex; align-items:center; justify-content:center; font-size:14px;
          border:1px solid #2c333f; }
  .hide-btn:active { background:#4a1f1f; color:#f0b6b6; }
  .card.seen { opacity:0.5; }
  .fav-btn { position:absolute; top:6px; left:8px; width:26px; height:26px;
          border-radius:50%; background:#20252ecc; color:#8a93a2; z-index:2;
          display:flex; align-items:center; justify-content:center; font-size:15px;
          border:1px solid #2c333f; }
  .fav-btn.on { color:#f0c040; border-color:#6b5a1e; }
  .seen-btn { display:inline-block; margin-top:5px; font-size:12px; color:#8a93a2;
          border:1px solid #2c333f; border-radius:6px; padding:2px 9px; }
  .seen-btn.on { color:#8fd0a0; border-color:#2f5f3c; }
  .b-new { background:#123a3a; color:#a6eaea; }
  .ai-badge { cursor:pointer; }
  .ai-good { background:#1e5f3c; color:#c9f5d9; }
  .ai-mid { background:#5a4a1e; color:#f0dca0; }
  .ai-bad { background:#5a1e1e; color:#f0b6b6; }
  .ai-panel { margin-top:6px; padding:8px 10px; background:#0f1115;
          border:1px solid #2c333f; border-radius:8px; font-size:12px;
          color:#cbd3df; line-height:1.5; }
  .ai-panel ul { margin:4px 0; padding-left:18px; color:#f0b6a0; }
  .ai-panel .ai-meta { color:#8a93a2; margin-top:4px; }
  .controls { margin:6px 0 2px; display:flex; flex-wrap:wrap; gap:8px;
          align-items:center; font-size:13px; color:#8a93a2; }
  .controls select, .controls input { background:#0f1115; color:#e6e6e6;
          border:1px solid #2c333f; border-radius:6px; padding:5px 7px; font-size:13px; }
  .controls input.px { width:72px; }
  .controls .apply { background:#2f5fd0; color:#fff; border-color:#2f5fd0;
          cursor:pointer; }
  .card.deal { border-color:#2f7d4f; background:#132018; }
  .olx { color:inherit; text-decoration:none; }
  .imglink { flex:none; display:block; line-height:0; }
  .card.has-ai { cursor:pointer; }
  .card.has-ai:active { background:#1c2230; }
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
  .flash.warn { background:#3a1f1f; color:#f0b6b6; border-color:#5a2a2a; }
  .flash code { background:#00000033; padding:1px 5px; border-radius:4px; }
"""

_SHELL = """<!doctype html>
<html lang="ro"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>OLX Deals</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1115">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OLX Deals">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<style>{css}</style></head><body>
<header>
  <div class="wrap">
    <div class="topbar">
      <h1>OLX Deals</h1>
      <div class="actions">
        <form method="post" action="/sync">
          <button class="iconbtn" type="submit" title="Sync now">&#8635;</button>
        </form>
        <button class="iconbtn" type="button" onclick="enableNotifs()"
                title="Enable deal alerts">&#128276;</button>
      </div>
    </div>
    <div class="sub">{sub}</div>
  </div>
</header>
{flash}
<main class="wrap">
{content}
</main>
<nav class="tabbar"><div class="wrap">
  <a href="{deals_href}" class="{deals_active}"><span class="ic">&#127991;</span>Deals</a>
  <a href="{drops_href}" class="{drops_active}"><span class="ic">&#128201;</span>Drops</a>
  <a href="/saved" class="{saved_active}"><span class="ic">&#9733;</span>Saved</a>
  <a href="{trends_href}" class="{trends_active}"><span class="ic">&#128202;</span>Trends</a>
  <a href="/searches" class="{manage_active}"><span class="ic">&#9881;</span>Manage</a>
</div></nav>
<script>
// On Android, rewrite listing links to open the OLX app (ro.mercador),
// falling back to the web page if the app isn't installed.
if (/Android/i.test(navigator.userAgent)) {{
  document.querySelectorAll('a.olx[data-olx]').forEach(function(a) {{
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
    body: 'id=' + encodeURIComponent(id),
    cache: 'no-store'
  }}).then(function(r) {{
    if (r.ok) {{ if (el) el.remove(); }}
    else {{ alert('Could not hide (server returned ' + r.status + ').'); }}
  }}).catch(function(err) {{
    alert('Could not hide — request failed: ' + err +
          '\\nIf this keeps happening, close and reopen the app to refresh it.');
  }});
}}
function askHide(card) {{
  if (!card) return;
  if (confirm('Hide this listing from tracking?\\nIt stays hidden on future ' +
              'syncs — restore it from the Manage tab.'))
    excludeId(card.dataset.id, card);
}}
function hideCard(e, btn) {{
  e.preventDefault(); e.stopPropagation();
  askHide(btn.closest('.card'));
}}
// Toggle a per-listing flag (favorite / seen) without navigating.
function toggleFlag(e, btn, path, cls, onOk) {{
  e.preventDefault(); e.stopPropagation();
  var card = btn.closest('.card'); if (!card) return;
  var on = !btn.classList.contains('on');
  fetch(path, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'id=' + encodeURIComponent(card.dataset.id) + '&on=' + (on ? '1' : '0')
  }}).then(function(r) {{ if (r.ok) {{
    btn.classList.toggle('on', on);
    if (cls) card.classList.toggle(cls, on);
    if (onOk) onOk(on);
  }} }});
}}
function toggleFav(e, btn) {{
  toggleFlag(e, btn, '/favorite', null,
    function(on) {{ btn.textContent = on ? '\\u2605' : '\\u2606'; }});
}}
function toggleSeen(e, btn) {{
  toggleFlag(e, btn, '/seen', 'seen',
    function(on) {{ btn.textContent = on ? 'seen \\u2713' : 'mark seen'; }});
}}
// LLM verdict: toggle the detail panel / run an on-demand analysis.
function toggleAi(e, el) {{
  e.preventDefault(); e.stopPropagation();
  var p = el.closest('.card').querySelector('.ai-panel');
  if (p) p.hidden = !p.hidden;
}}
function runAnalyze(e, btn) {{
  e.preventDefault(); e.stopPropagation();
  if (btn.dataset.busy) return;
  btn.dataset.busy = '1';
  btn.textContent = 'analyzing\\u2026';
  var card = btn.closest('.card');
  fetch('/analyze', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'id=' + encodeURIComponent(card.dataset.id)
  }}).then(function(r) {{
    if (r.ok) {{ location.reload(); }}
    else {{
      r.text().then(function(t) {{
        alert('Analysis failed: ' + (t || r.status));
        delete btn.dataset.busy; btn.textContent = '\\u2726 analyze';
      }});
    }}
  }}).catch(function(err) {{
    alert('Analysis failed: ' + err);
    delete btn.dataset.busy; btn.textContent = '\\u2726 analyze';
  }});
}}
// Set a per-listing flag on the server and reflect it in the card UI.
function setFlag(card, path, on) {{
  fetch(path, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'id=' + encodeURIComponent(card.dataset.id) + '&on=' + (on ? '1' : '0')
  }});
}}
function swipeSeen(card) {{  // swipe left -> toggle "seen"
  var btn = card.querySelector('.seen-toggle');
  var on = !(btn && btn.classList.contains('on'));
  setFlag(card, '/seen', on);
  card.classList.toggle('seen', on);
  if (btn) {{ btn.classList.toggle('on', on);
    btn.textContent = on ? 'seen \\u2713' : 'mark seen'; }}
}}
function swipeFav(card) {{  // swipe right -> toggle favorite
  var btn = card.querySelector('.fav-btn');
  var on = !(btn && btn.classList.contains('on'));
  setFlag(card, '/favorite', on);
  if (btn) {{ btn.classList.toggle('on', on);
    btn.textContent = on ? '\\u2605' : '\\u2606'; }}
}}

// Per-card gestures: title/image link to OLX; tap the body opens the AI
// summary; long-press hides; horizontal swipe = seen (left) / favorite (right).
document.querySelectorAll('.card[data-id]').forEach(function(card) {{
  var timer = null, fired = false;
  var startX = 0, startY = 0, dx = 0, swiping = false, swiped = false;

  function settle() {{
    card.style.transition = 'transform .18s ease';
    card.style.transform = '';
    card.classList.remove('sw-fav', 'sw-seen');
  }}

  card.addEventListener('touchstart', function(e) {{
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    dx = 0; swiping = false; swiped = false; fired = false;
    card.style.transition = '';
    timer = setTimeout(function() {{ fired = true; askHide(card); }}, 550);
  }}, {{passive: true}});

  card.addEventListener('touchmove', function(e) {{
    var mx = e.touches[0].clientX - startX, my = e.touches[0].clientY - startY;
    if (!swiping) {{
      if (Math.abs(mx) > 12 && Math.abs(mx) > Math.abs(my) * 1.5) {{
        swiping = true; clearTimeout(timer);   // horizontal -> swipe
      }} else {{
        if (Math.abs(my) > 10) clearTimeout(timer);  // vertical -> let it scroll
        return;
      }}
    }}
    dx = mx;
    e.preventDefault();                          // hold the horizontal drag
    card.style.transform = 'translateX(' + dx + 'px)';
    card.classList.toggle('sw-fav', dx > 45);
    card.classList.toggle('sw-seen', dx < -45);
  }}, {{passive: false}});

  ['touchend', 'touchcancel'].forEach(function(ev) {{
    card.addEventListener(ev, function() {{
      clearTimeout(timer);
      if (!swiping) return;
      if (dx <= -80) swipeSeen(card);
      else if (dx >= 80) swipeFav(card);
      settle();
      swiped = true; swiping = false;           // suppress the trailing click
    }});
  }});

  card.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
  card.addEventListener('click', function(e) {{
    if (fired || swiped) {{
      e.preventDefault(); e.stopPropagation(); fired = false; swiped = false; return;
    }}
    if (e.target.closest('.olx, .fav-btn, .hide-btn, .seen-btn, .ai-badge, .ai-panel'))
      return;
    var panel = card.querySelector('.ai-panel');
    if (panel) panel.hidden = !panel.hidden;    // tap body -> toggle AI summary
  }});
}});

// Register the service worker (only in a secure context — HTTPS/localhost).
if ('serviceWorker' in navigator && window.isSecureContext) {{
  navigator.serviceWorker.register('/sw.js').catch(function() {{}});
}}

function urlB64ToUint8(b64) {{
  var pad = '='.repeat((4 - b64.length % 4) % 4);
  var s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  var raw = atob(s), arr = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}}
async function enableNotifs() {{
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {{
    alert('This browser does not support push notifications.'); return;
  }}
  if (!window.isSecureContext) {{
    alert('Open the app via its HTTPS address (…ts.net:8443) to enable alerts.');
    return;
  }}
  try {{
    var perm = await Notification.requestPermission();
    if (perm !== 'granted') {{ alert('Notifications were not allowed.'); return; }}
    var reg = await navigator.serviceWorker.ready;
    var key = (await (await fetch('/push/public-key')).json()).key;
    var sub = await reg.pushManager.subscribe({{
      userVisibleOnly: true, applicationServerKey: urlB64ToUint8(key)
    }});
    await fetch('/push/subscribe', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(sub)
    }});
    await fetch('/push/test', {{method: 'POST'}});  // immediate confirmation push
    alert('Deal alerts enabled — you should see a test notification.');
  }} catch (err) {{
    alert('Could not enable alerts: ' + err);
  }}
}}
</script>
</body></html>"""


# Remember the last query (selected search + filters) per view, so the bottom
# tabs restore where you left off. Single-user local app -> a module dict is fine.
_LAST_QUERY: dict[str, str] = {}
_REMEMBER_KEYS = {
    "/": ["search", "group", "sort", "seller", "pmin", "pmax", "hide_seen"],
    "/drops": ["search", "group"],
    "/history": ["search", "group"],
}


def _remember(path: str, qs: dict) -> None:
    keys = _REMEMBER_KEYS.get(path)
    if keys is None:
        return
    parts = [(k, qs[k][0]) for k in keys if qs.get(k, [""])[0] not in ("", None)]
    _LAST_QUERY[path] = urllib.parse.urlencode(parts)


def _tab_href(path: str) -> str:
    q = _LAST_QUERY.get(path, "")
    return f"{path}?{q}" if q else path


def _time_ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _is_recent(iso: str | None, hours: float) -> bool:
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() < hours * 3600


def _sync_banner(store: Store) -> str:
    """Red banner when the most recent sync of any search failed."""
    runs = store.last_runs()
    failed = [k for k, r in runs.items() if not r.get("ok")]
    if not failed:
        return ""
    names = ", ".join(html.escape(k) for k in failed)
    return (f'<div class="flash warn">&#9888; Last sync failed for {names}. '
            f'Data may be stale — see <code>journalctl --user -u olx-sync</code>.'
            f'</div>')


def _last_sync_text(store: Store) -> str:
    runs = store.last_runs()
    if not runs:
        return "never synced"
    latest = max(r["ts"] for r in runs.values())
    return f"synced {_time_ago(latest)}"


def _shell(sub: str, content: str, active: str, flash: str = "") -> str:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return _SHELL.format(
        css=_CSS, sub=sub, content=content, flash=flash_html,
        deals_href=_tab_href("/"),
        drops_href=_tab_href("/drops"),
        trends_href=_tab_href("/history"),
        deals_active="active" if active == "deals" else "",
        drops_active="active" if active == "drops" else "",
        saved_active="active" if active == "saved" else "",
        trends_active="active" if active == "trends" else "",
        manage_active="active" if active == "searches" else "",
    )


# ---------- deals page ----------

DISPLAY_CAP = 80  # max cards rendered per Deals view (after sort/filter)


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


def _ai_bits(analysis: dict | None) -> tuple[str, str, str]:
    """(badge_html, panel_html, action_html) for a listing's LLM verdict."""
    if not analysis:
        return "", "", ('<span class="seen-btn" '
                        'onclick="runAnalyze(event,this)">✦ analyze</span>')
    score = analysis.get("score") or 0
    risk = analysis.get("scam_risk") or "?"
    cls = "ai-good" if score >= 70 else "ai-mid" if score >= 40 else "ai-bad"
    if risk == "high":
        cls = "ai-bad"
    try:
        v = json.loads(analysis.get("verdict_json") or "{}")
    except ValueError:
        v = {}
    flags = "".join(f"<li>{html.escape(f)}</li>"
                    for f in v.get("red_flags") or [])
    panel = f"""<div class="ai-panel" hidden
     onclick="event.preventDefault();event.stopPropagation()">
  <div><b>{html.escape(v.get('summary', ''))}</b></div>
  <div>Condition: {html.escape(v.get('condition_summary', ''))}</div>
  {'<ul>' + flags + '</ul>' if flags else ''}
  <div>&#128161; {html.escape(v.get('negotiation_tip', ''))}</div>
  <div class="ai-meta">scam risk: {html.escape(str(risk))} · photos match:
    {'yes' if v.get('photos_match_description') else '<b>NO</b>'}</div>
</div>"""
    badge = (f'<span class="badge ai-badge {cls}" '
             f'onclick="toggleAi(event,this)">AI {score}</span>')
    return badge, panel, ""


def _card(sl, history: list | None = None, search_label: str | None = None,
          analysis: dict | None = None) -> str:
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
    # Freshly-appeared listings (first tracked <24h ago) are the best signal.
    if _is_recent(r.get("first_seen"), 24):
        badges += '<span class="badge b-new">NEW</span>'
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

    meta_bits = []
    if r.get("city"):
        meta_bits.append(html.escape(r["city"]))
    posted = _time_ago(r.get("created_time"))
    if posted and posted != "never":
        meta_bits.append(f"posted {posted}")
    if search_label:
        meta_bits.append(f'<span style="color:#6b7280">{html.escape(search_label)}</span>')
    city = " · ".join(meta_bits)

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

    fav_on = "on" if r.get("favorite") else ""
    fav_glyph = "★" if r.get("favorite") else "☆"
    seen_on = "on" if r.get("seen") else ""
    seen_txt = "seen ✓" if r.get("seen") else "mark seen"
    seen_cls = "seen" if r.get("seen") else ""
    ai_badge, ai_panel, ai_action = _ai_bits(analysis)
    has_ai = " has-ai" if analysis else ""
    olx = f'href="{url}" target="_blank" rel="noopener" data-olx="{url}"'

    return f"""<div class="card {'deal' if sl.is_deal else ''} {seen_cls}{has_ai}"
   data-olx="{url}" data-id="{r.get('id')}">
  <span class="fav-btn {fav_on}" title="Save to favorites"
        onclick="toggleFav(event, this)">{fav_glyph}</span>
  <span class="hide-btn" title="Hide from tracking"
        onclick="hideCard(event, this)">✕</span>
  <a class="olx imglink" {olx}>{img}</a>
  <div class="body">
    <p class="title"><a class="olx" {olx}>{title}</a></p>
    <div class="price">{price_txt}{orig}</div>
    <div>{badges}{ai_badge}</div>
    <div class="meta">{city}</div>
    {trend}
    {ai_panel}
    <span class="seen-btn seen-toggle {seen_on}" onclick="toggleSeen(event, this)">{seen_txt}</span>
    {ai_action}
  </div>
</div>"""


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


def _search_groups(config_path: str, db_path: str):
    """Ordered {group: [keys]} from config (group field, default 'Other'),
    plus a {key: group} map. DB-only keys land in 'Other'."""
    searches = config.load_raw(config_path).get("searches", [])
    groups: dict[str, list[str]] = {}
    key_group: dict[str, str] = {}
    for s in searches:
        k = s.get("key")
        if not k:
            continue
        g = (s.get("group") or "").strip() or "Other"
        groups.setdefault(g, [])
        if k not in groups[g]:
            groups[g].append(k)
        key_group[k] = g
    for k in _search_keys(config_path, db_path):  # orphan DB-only keys
        if k not in key_group:
            groups.setdefault("Other", [])
            if k not in groups["Other"]:
                groups["Other"].append(k)
            key_group[k] = "Other"
    return groups, key_group


def _menu_counts(store: Store, keys: list[str]) -> dict[str, tuple[int, int]]:
    """{key: (active_count, deal_count)} for the dropdown stats."""
    out: dict[str, tuple[int, int]] = {}
    for k in keys:
        active = store.active_for_search(k)
        out[k] = (len(active), len(score_search(k, active).deals))
    return out


def _stat_html(active: int, deals: int) -> str:
    return f'<span class="stat">{active} · <span class="dl">{deals}&#9670;</span></span>'


def _resolve_scope(config_path: str, db_path: str,
                   selected: str | None, group: str | None):
    """Resolve which searches to show from ?search / ?group.
    Returns (all_keys, groups, show, scope, selected, group)."""
    all_keys = _search_keys(config_path, db_path)
    groups, _kg = _search_groups(config_path, db_path)
    if selected in all_keys:
        return all_keys, groups, [selected], "search", selected, None
    if group in groups:
        return all_keys, groups, groups[group], "group", None, group
    return all_keys, groups, all_keys, "all", None, None


def _grouped_menu(groups: dict, base: str, sel_search: str | None,
                  sel_group: str | None, counts: dict, totals: tuple) -> str:
    """Accordion dropdown: All → groups (expandable) → searches."""
    def link(href, label, on, stat):
        return (f'<a href="{href}" class="{"on" if on else ""}">'
                f'<span>{label}</span>{stat}</a>')

    all_on = not sel_search and not sel_group
    rows = [link(base, "All searches", all_on, _stat_html(*totals))]
    for gname, keys in groups.items():
        ga = sum(counts.get(k, (0, 0))[0] for k in keys)
        gd = sum(counts.get(k, (0, 0))[1] for k in keys)
        expanded = sel_group == gname or sel_search in keys
        gsel = sel_group == gname
        inner = "".join(
            link(f"{base}?search={urllib.parse.quote(k)}", html.escape(k),
                 sel_search == k, _stat_html(*counts.get(k, (0, 0))))
            for k in keys)
        rows.append(
            f'<details class="grp" {"open" if expanded else ""}><summary>'
            f'<a class="grp-name {"on" if gsel else ""}" '
            f'href="{base}?group={urllib.parse.quote(gname)}">{html.escape(gname)}</a>'
            f'{_stat_html(ga, gd)}<span class="grp-caret">&#9656;</span></summary>'
            f'<div class="grp-items">{inner}</div></details>')

    if sel_search:
        label, stat = html.escape(sel_search), _stat_html(*counts.get(sel_search, (0, 0)))
    elif sel_group:
        keys = groups.get(sel_group, [])
        label = html.escape(sel_group)
        stat = _stat_html(sum(counts.get(k, (0, 0))[0] for k in keys),
                          sum(counts.get(k, (0, 0))[1] for k in keys))
    else:
        label, stat = "All searches", _stat_html(*totals)
    return (f'<details class="menu"><summary>'
            f'<span class="burger">&#9776;</span>{label}'
            f'<span class="sstat">· {stat}</span>'
            f'<span class="caret">&#9662;</span></summary>'
            f'<div class="items">{"".join(rows)}</div></details>')


def _controls_bar(selected: str | None, group: str | None, f: dict) -> str:
    """Sort/filter controls, collapsed behind a 'Filters' toggle.
    Opens by default only when a non-default filter is active."""
    def opt(name, value, label):
        sel = "selected" if f.get(name) == value else ""
        return f'<option value="{value}" {sel}>{label}</option>'
    hidden = ""
    if selected:
        hidden = f'<input type="hidden" name="search" value="{html.escape(selected)}">'
    elif group:
        hidden = f'<input type="hidden" name="group" value="{html.escape(group)}">'
    checked = "checked" if f.get("hide_seen") else ""
    active = (f.get("sort", "deal") != "deal" or f.get("seller", "all") != "all"
              or f.get("pmin") or f.get("pmax") or f.get("hide_seen"))
    summary = "Filters" + (" · active" if active else "")
    form = f"""<form class="controls" method="get" action="/">{hidden}
  Sort <select name="sort">
    {opt('sort','deal','deal %')}{opt('sort','price_asc','price ↑')}
    {opt('sort','price_desc','price ↓')}{opt('sort','newest','newest')}
  </select>
  Seller <select name="seller">
    {opt('seller','all','all')}{opt('seller','private','private')}
    {opt('seller','dealer','dealer')}
  </select>
  <input class="px" name="pmin" inputmode="numeric" placeholder="min"
         value="{f.get('pmin') or ''}">–<input class="px" name="pmax"
         inputmode="numeric" placeholder="max" value="{f.get('pmax') or ''}">RON
  <label><input type="checkbox" name="hide_seen" value="1" {checked}> hide seen</label>
  <button class="apply" type="submit">Apply</button>
</form>"""
    return (f'<details class="filterbox" {"open" if active else ""}>'
            f'<summary>{summary}</summary>{form}</details>')


def render_deals(db_path: str, config_path: str, selected: str | None = None,
                 group: str | None = None, flash: str = "",
                 filters: dict | None = None) -> str:
    f = filters or {}
    seller = f.get("seller", "all")
    pmin, pmax = f.get("pmin"), f.get("pmax")
    hide_seen = bool(f.get("hide_seen"))
    sort = f.get("sort", "deal")

    all_keys = _search_keys(config_path, db_path)
    groups, _kg = _search_groups(config_path, db_path)
    if selected in all_keys:
        show, scope, group = [selected], "search", None
    elif group in groups:
        show, scope, selected = groups[group], "group", None
    else:
        show, scope, selected, group = all_keys, "all", None, None
    show_label = scope != "search"
    store = Store(db_path)
    try:
        counts: dict[str, tuple[int, int]] = {}
        total_deals = total_active = shown_deals = 0
        sel_header = ""
        # (search_key, ScoredListing, history) across every shown search.
        pool: list[tuple[str, Any, list | None]] = []
        # Score every search (cheap) so the dropdown can show per-search stats;
        # only pool listings for the search(es) actually being shown.
        for key in all_keys:
            active = store.active_for_search(key)
            sd = score_search(key, active)
            ndeals = len(sd.deals)
            counts[key] = (len(active), ndeals)
            total_deals += ndeals
            total_active += len(active)
            if key not in show:
                continue
            shown_deals += ndeals
            hist = store.histories([l.raw["id"] for l in sd.listings])
            for l in sd.listings:
                pool.append((key, l, hist.get(l.raw["id"])))
            if scope == "search":  # single search: one compact header line
                med = f"{sd.median:.0f} RON" if sd.median else "—"
                susp = len(sd.suspicious)
                susp_txt = f" · {susp} too-cheap" if susp else ""
                if active:
                    sel_header = (
                        f'<div class="search"><b>{html.escape(key)}</b> · '
                        f'{len(active)} active · median {med} · '
                        f'{ndeals} deal(s){susp_txt}</div>')
                else:
                    sel_header = ('<div class="note" style="margin:8px 16px 0">'
                                  'No active listings — try Sync now.</div>')

        # --- filters ---
        def keep(sl) -> bool:
            r = sl.raw
            # Drop 0-price / price-less junk (common on car listings).
            if sl.price_ron is None or sl.price_ron <= 0:
                return False
            if seller == "private" and r.get("is_business"):
                return False
            if seller == "dealer" and not r.get("is_business"):
                return False
            p = sl.price_ron
            if pmin is not None and (p is None or p < pmin):
                return False
            if pmax is not None and (p is None or p > pmax):
                return False
            if hide_seen and r.get("seen"):
                return False
            return True
        pool = [t for t in pool if keep(t[1])]

        # --- sort (then always sink 'seen' to the bottom, stably) ---
        if sort == "price_asc":
            pool.sort(key=lambda t: (t[1].price_ron is None, t[1].price_ron or 0))
        elif sort == "price_desc":
            pool.sort(key=lambda t: t[1].price_ron or 0, reverse=True)
        elif sort == "newest":
            pool.sort(key=lambda t: t[1].raw.get("created_time") or "", reverse=True)
        else:  # deal %
            pool.sort(key=lambda t: (t[1].is_deal, t[1].deal_score), reverse=True)
        pool.sort(key=lambda t: bool(t[1].raw.get("seen")))  # unseen first, stable
        pool = pool[:DISPLAY_CAP]  # bound page weight after sort/filter

        analyses = store.get_analyses([l.raw["id"] for _, l, _ in pool])
        cards = "".join(
            _card(l, h, search_label=key if show_label else None,
                  analysis=analyses.get(l.raw["id"]))
            for key, l, h in pool)
        if cards:
            body = sel_header + f'<div class="cards">{cards}</div>'
        else:
            body = ('<div class="empty">No searches yet. '
                    'Add one on Manage, then Sync now.</div>')
        menu = _grouped_menu(groups, "/", selected, group, counts,
                             (total_active, total_deals))
        content = (_sync_banner(store) + menu
                   + _controls_bar(selected, group, f) + body)
        if scope == "search":
            scope_txt = f"'{selected}'"
        elif scope == "group":
            scope_txt = f"group '{group}'"
        else:
            scope_txt = f"{len(all_keys)} search(es)"
        n24, c24 = store.ai_cost(24)
        nT, cT = store.ai_cost()
        ai_txt = (f" · AI ${c24:.2f}/24h · ${cT:.2f} total ({nT})"
                  if nT else "")
        sub = (f"{scope_txt} · {shown_deals} deal(s) · {_last_sync_text(store)} · "
               f"EUR→RON {EUR_TO_RON}{ai_txt}")
    finally:
        store.close()
    return _shell(sub, content, "deals", flash)


def render_saved(db_path: str, config_path: str, flash: str = "") -> str:
    """Favorited listings across all searches, best deal first."""
    store = Store(db_path)
    try:
        favs = store.favorite_listings()
        fav_ids = {r["id"] for r in favs}
        by_key: dict[str, list] = {}
        for r in favs:
            by_key.setdefault(r["search_key"], [])
        cards_data: list[tuple[float, str, Any, list | None]] = []
        for key in by_key:
            active = store.active_for_search(key)
            sd = score_search(key, active)
            hist = store.histories([l.raw["id"] for l in sd.listings])
            for l in sd.listings:
                if l.raw["id"] in fav_ids:
                    cards_data.append((l.deal_score, key, l, hist.get(l.raw["id"])))
        cards_data.sort(key=lambda t: t[0], reverse=True)
        if cards_data:
            analyses = store.get_analyses(
                [l.raw["id"] for _, _, l, _ in cards_data])
            body = (f'<div class="cards">'
                    + "".join(_card(l, h, search_label=key,
                                    analysis=analyses.get(l.raw["id"]))
                              for _, key, l, h in cards_data) + '</div>')
        else:
            body = ('<div class="empty">No saved listings yet.<br>'
                    'Tap the ☆ on any card to save it here.</div>')
        content = _sync_banner(store) + body
        sub = f"{len(cards_data)} saved listing(s)"
    finally:
        store.close()
    return _shell(sub, content, "saved", flash)


def render_drops(db_path: str, config_path: str, selected: str | None = None,
                 group: str | None = None, flash: str = "") -> str:
    """Listings whose price has fallen since we first saw them, biggest first.
    Drops that land in deal range are highlighted."""
    all_keys, groups, show, scope, selected, group = _resolve_scope(
        config_path, db_path, selected, group)
    store = Store(db_path)
    try:
        counts: dict[str, tuple[int, int]] = {}
        cards: list[tuple[float, str]] = []
        for key in all_keys:
            active = store.active_for_search(key)
            sd = score_search(key, active)
            counts[key] = (len(active), len(sd.deals))
            if key not in show:
                continue
            hist = store.histories([l.raw["id"] for l in sd.listings])
            for l in sd.listings:
                series = _ron_series(hist.get(l.raw["id"]))
                if len(series) >= 2 and series[-1] < series[0]:
                    pct = (series[0] - series[-1]) / series[0]
                    cards.append((pct, l, hist.get(l.raw["id"])))
        cards.sort(key=lambda c: c[0], reverse=True)
        if cards:
            analyses = store.get_analyses([l.raw["id"] for _, l, _ in cards])
            body = ('<div class="cards">'
                    + "".join(_card(l, h, analysis=analyses.get(l.raw["id"]))
                              for _, l, h in cards)
                    + '</div>')
        else:
            body = ('<div class="empty">No price drops recorded yet.<br>'
                    'Drops appear here once a tracked listing gets cheaper '
                    'between syncs — check back after a day or two.</div>')
        totals = (sum(a for a, _ in counts.values()),
                  sum(d for _, d in counts.values()))
        menu = _grouped_menu(groups, "/drops", selected, group, counts, totals)
        content = _sync_banner(store) + menu + body
        scope_txt = (f"'{selected}'" if scope == "search" else
                     f"group '{group}'" if scope == "group" else "all searches")
        sub = f"{len(cards)} price drop(s) · {scope_txt}"
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
                   group: str | None = None, flash: str = "") -> str:
    all_keys, groups, show, scope, selected, group = _resolve_scope(
        config_path, db_path, selected, group)
    store = Store(db_path)
    try:
        counts = _menu_counts(store, all_keys)
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
        totals = (sum(a for a, _ in counts.values()),
                  sum(d for _, d in counts.values()))
        menu = _grouped_menu(groups, "/history", selected, group, counts, totals)
        content = _sync_banner(store) + menu + body
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
    existing_groups = sorted({(s.get("group") or "").strip()
                              for s in searches if (s.get("group") or "").strip()})
    ed_group = html.escape(editing.get("group") or "") if editing else ""
    datalist = "".join(f'<option value="{html.escape(g)}">' for g in existing_groups)

    def val(key, default=""):
        if not editing:
            return default
        v = editing.get(key)
        return "" if v is None else html.escape(str(v))

    ed_model = ed_state = ed_fuel = ed_gear = ed_wheel = ed_brand = ""
    ed_yfrom = ed_yto = ed_mileage = ""
    if editing:
        f = editing.get("filters") or {}
        ed_model = html.escape((f.get("model") or [""])[0])
        ed_state = (f.get("state") or [""])[0]
        ed_fuel = (f.get("petrol") or [""])[0]
        ed_gear = (f.get("gearbox") or [""])[0]
        ed_wheel = (f.get("dimensiune_roata") or [""])[0]
        ed_brand = (f.get("brand") or [""])[0]
        rng = editing.get("ranges") or {}
        yr = rng.get("year") or {}
        ed_yfrom = yr.get("from") or ""
        ed_yto = yr.get("to") or ""
        ed_mileage = (rng.get("rulaj_pana") or {}).get("to") or ""

    def sel(v):
        return "selected" if ed_state == v else ""

    def selo(current, v):
        return "selected" if current == v else ""

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
  <label>Group (optional)</label>
  <input name="group" value="{ed_group}" list="groups" placeholder="Fold, Phones, Cars…">
  <datalist id="groups">{datalist}</datalist>
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
  <details style="margin-top:10px" {'open' if (ed_yfrom or ed_yto or ed_mileage or ed_fuel or ed_gear) else ''}>
    <summary style="cursor:pointer;color:#8a93a2;font-size:13px">Vehicle filters (optional)</summary>
    <div class="row2">
      <div><label>Year from</label>
        <input name="year_from" value="{ed_yfrom}" inputmode="numeric" placeholder="2017"></div>
      <div><label>Year to</label>
        <input name="year_to" value="{ed_yto}" inputmode="numeric" placeholder="2020"></div>
    </div>
    <label>Max mileage (km)</label>
    <input name="mileage_to" value="{ed_mileage}" inputmode="numeric" placeholder="200000">
    <div class="row2">
      <div><label>Fuel</label>
        <select name="fuel">
          <option value="">any</option>
          <option value="diesel" {selo(ed_fuel,'diesel')}>Diesel</option>
          <option value="petrol" {selo(ed_fuel,'petrol')}>Benzină</option>
          <option value="hybrid" {selo(ed_fuel,'hybrid')}>Hibrid</option>
          <option value="lpg" {selo(ed_fuel,'lpg')}>GPL</option>
          <option value="electric" {selo(ed_fuel,'electric')}>Electric</option>
        </select></div>
      <div><label>Gearbox</label>
        <select name="gearbox">
          <option value="">any</option>
          <option value="automatic" {selo(ed_gear,'automatic')}>Automată</option>
          <option value="manual" {selo(ed_gear,'manual')}>Manuală</option>
        </select></div>
    </div>
    <div class="note" style="margin:6px 0 0">For cars: narrow year + mileage so the
      median compares like-for-like. Leave blank for phones.</div>
  </details>
  <details style="margin-top:10px" {'open' if (ed_wheel or ed_brand) else ''}>
    <summary style="cursor:pointer;color:#8a93a2;font-size:13px">Bike filters (optional)</summary>
    <div class="row2">
      <div><label>Wheel size</label>
        <select name="wheel">
          <option value="">any</option>
          <option value="29_inch" {selo(ed_wheel,'29_inch')}>29"</option>
          <option value="27_5_inch" {selo(ed_wheel,'27_5_inch')}>27.5"</option>
          <option value="26_inch" {selo(ed_wheel,'26_inch')}>26"</option>
          <option value="28_inch" {selo(ed_wheel,'28_inch')}>28"</option>
          <option value="20_inch" {selo(ed_wheel,'20_inch')}>20"</option>
          <option value="16_inch" {selo(ed_wheel,'16_inch')}>16"</option>
        </select></div>
      <div><label>Brand</label>
        <select name="brand">
          <option value="">any</option>
          <option value="cube" {selo(ed_brand,'cube')}>Cube</option>
          <option value="specialized" {selo(ed_brand,'specialized')}>Specialized</option>
          <option value="scott" {selo(ed_brand,'scott')}>Scott</option>
          <option value="focus" {selo(ed_brand,'focus')}>Focus</option>
          <option value="rockrider" {selo(ed_brand,'rockrider')}>Rockrider</option>
          <option value="btwin" {selo(ed_brand,'btwin')}>B'Twin</option>
          <option value="pegas" {selo(ed_brand,'pegas')}>Pegas</option>
          <option value="alt_brand" {selo(ed_brand,'alt_brand')}>Other brand</option>
        </select></div>
    </div>
    <div class="note" style="margin:6px 0 0">Bikes have no type filter — put
      "mountain bike" in the free-text query above. Category 987 = bikes.
      Pick a single wheel size (OLX ignores multiple).</div>
  </details>
  <div style="margin-top:12px; display:flex; gap:10px;">
    <button class="btn btn-go" type="submit">
      {'Save changes' if editing else 'Add search'}</button>
    {'<a class="btn" href="/searches">Cancel</a>' if editing else ''}
  </div>
  <div class="note">Tip: with a model key, leave <b>Category id = 0</b> (all
    categories) — OLX phone categories are per-brand (Apple 948, Samsung 956…),
    so a fixed category can hide results. Use the finder above to get the model key.</div>
</form>"""

    def srow(s: dict) -> str:
        key = html.escape(s.get("key", ""))
        return f"""<div class="srow">
  <div class="info"><div class="k">{key}</div>
    <div class="d">{html.escape(_search_summary(s))}</div></div>
  <a class="btn" href="/searches?edit={urllib.parse.quote(s.get('key',''))}">Edit</a>
  <form method="post" action="/searches/delete" style="margin:0"
        onsubmit="return confirm('Delete {key}?')">
    <input type="hidden" name="key" value="{key}">
    <button class="btn btn-del" type="submit">Delete</button>
  </form>
</div>"""

    # Group the search list under group headers (config order of first appearance).
    grouped: dict[str, list[dict]] = {}
    for s in searches:
        grouped.setdefault((s.get("group") or "").strip() or "Other", []).append(s)
    listing = ""
    for gname, gsearches in grouped.items():
        listing += (f'<div class="search"><b>{html.escape(gname)}</b> · '
                    f'{len(gsearches)} search(es)</div>')
        listing += "".join(srow(s) for s in gsearches)
    listing = listing or '<div class="empty">No searches yet.</div>'
    content = form + '<div class="search"><b>Current searches</b></div>' + listing

    # Hidden listings — excluded via ✕ / long-press, restorable here.
    store = Store(db_path)
    try:
        banner = _sync_banner(store)
        hidden = store.excluded_listings()
    finally:
        store.close()
    content = banner + content
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
    group = form.get("group", "").strip()
    if group:
        s["group"] = group
    filters: dict = {}
    model = form.get("model", "").strip()
    if model:
        filters["model"] = [model]
    state = form.get("state", "").strip()
    if state in ("used", "new"):
        filters["state"] = [state]
    fuel = form.get("fuel", "").strip()
    if fuel:
        filters["petrol"] = [fuel]  # OLX enum name for fuel type
    gearbox = form.get("gearbox", "").strip()
    if gearbox:
        filters["gearbox"] = [gearbox]
    wheel = form.get("wheel", "").strip()
    if wheel:
        filters["dimensiune_roata"] = [wheel]  # OLX bike wheel-size enum
    brand = form.get("brand", "").strip()
    if brand:
        filters["brand"] = [brand]
    if filters:
        s["filters"] = filters
    # Numeric range filters (vehicles): year, mileage.
    ranges: dict = {}
    yf, yt = _int_or_none(form.get("year_from")), _int_or_none(form.get("year_to"))
    if yf is not None or yt is not None:
        ranges["year"] = {k: v for k, v in (("from", yf), ("to", yt))
                          if v is not None}
    mt = _int_or_none(form.get("mileage_to"))
    if mt is not None:
        ranges["rulaj_pana"] = {"to": mt}
    if ranges:
        s["ranges"] = ranges
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
    push: "Push" = None  # set in main()

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

    def _raw(self, data: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str) -> None:
        name = Path(path).name  # basename only — no directory traversal
        fp = STATIC_DIR / name
        if not fp.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._raw(fp.read_bytes(), ctype,
                  {"Cache-Control": "public, max-age=86400"})

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_test_push(self) -> None:
        store = Store(self.db_path)
        try:
            subs = store.all_subscriptions()
            dead = self.push.notify_all(subs, {
                "title": "OLX Deals",
                "body": "Alerts are on. You'll be notified of new deals.",
                "url": "/",
                "tag": "olx-test",
            })
            for ep in dead:
                store.remove_subscription(ep)
        finally:
            store.close()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        flash = qs.get("msg", [""])[0]
        selected = qs.get("search", [None])[0]
        group = qs.get("group", [None])[0]
        # Remember selected search/group + filters per view so tabs restore them.
        _remember("/" if parsed.path == "/index.html" else parsed.path, qs)
        if parsed.path in ("/", "/index.html"):
            filters = {
                "sort": qs.get("sort", ["deal"])[0],
                "seller": qs.get("seller", ["all"])[0],
                "pmin": _int_or_none(qs.get("pmin", [None])[0]),
                "pmax": _int_or_none(qs.get("pmax", [None])[0]),
                "hide_seen": qs.get("hide_seen", [None])[0] == "1",
            }
            self._html(render_deals(
                self.db_path, self.config_path, selected, group, flash, filters))
        elif parsed.path == "/saved":
            self._html(render_saved(self.db_path, self.config_path, flash))
        elif parsed.path == "/drops":
            self._html(render_drops(
                self.db_path, self.config_path, selected, group, flash))
        elif parsed.path == "/history":
            self._html(render_history(
                self.db_path, self.config_path, selected, group, flash))
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
        elif parsed.path == "/push/public-key":
            self._json({"key": self.push.public_key_b64()})
        elif parsed.path == "/manifest.webmanifest":
            self._raw(json.dumps(MANIFEST).encode("utf-8"),
                      "application/manifest+json")
        elif parsed.path == "/sw.js":
            self._raw(SW_JS.encode("utf-8"), "application/javascript",
                      {"Service-Worker-Allowed": "/"})
        elif parsed.path.startswith("/static/"):
            self._static(parsed.path)
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
            elif parsed.path in ("/favorite", "/seen"):
                form = self._form()
                lid = _int_or_none(form.get("id"))
                on = form.get("on", "1") == "1"
                if lid is not None:
                    store = Store(self.db_path)
                    try:
                        if parsed.path == "/favorite":
                            store.set_favorite(lid, on)
                        else:
                            store.set_seen(lid, on)
                    finally:
                        store.close()
                self.send_response(204)  # async star / seen toggle
                self.end_headers()
            elif parsed.path == "/push/subscribe":
                sub = self._json_body()
                if sub.get("endpoint"):
                    store = Store(self.db_path)
                    try:
                        store.add_subscription(sub)
                    finally:
                        store.close()
                self.send_response(204)
                self.end_headers()
            elif parsed.path == "/push/unsubscribe":
                sub = self._json_body()
                if sub.get("endpoint"):
                    store = Store(self.db_path)
                    try:
                        store.remove_subscription(sub["endpoint"])
                    finally:
                        store.close()
                self.send_response(204)
                self.end_headers()
            elif parsed.path == "/push/test":
                self._send_test_push()
                self.send_response(204)
                self.end_headers()
            elif parsed.path == "/analyze":
                self._run_analysis(_int_or_none(self._form().get("id")))
            elif parsed.path == "/sync":
                self._trigger_sync()
                # Return to the Deals view with its remembered search + filters.
                base = _tab_href("/")
                sep = "&" if "?" in base else "?"
                self._redirect(base + sep + "msg=" + urllib.parse.quote(
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

    def _run_analysis(self, listing_id: int | None) -> None:
        """On-demand LLM analysis for one listing (synchronous, ~20-60s)."""
        import os
        if listing_id is None:
            self.send_error(400)
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"ANTHROPIC_API_KEY not configured")
            return
        store = Store(self.db_path)
        try:
            listing = store.get(listing_id)
            if not listing:
                self.send_error(404)
                return
            from .analyzer import analyze
            analyze(store, listing)
            self.send_response(204)
            self.end_headers()
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(str(exc)[:300].encode("utf-8"))
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
    # VAPID key lives next to the DB so the sync process finds the same one.
    Handler.push = Push(Path(args.db).resolve().with_name("vapid_key.pem"))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OLX dashboard on http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
