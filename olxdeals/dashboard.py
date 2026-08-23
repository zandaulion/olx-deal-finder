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
import hashlib
import hmac
import html
import http.cookies
import json
import mimetypes
import os
import re
import queue
import secrets
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class SyncTracker:
    """Thread-safe tracker for live sync progress and SSE streaming."""
    _lock = threading.Lock()
    _state: dict[str, Any] = {
        "running": False,
        "step": 0,
        "total": 0,
        "current_key": "",
        "new_count": 0,
        "deal_count": 0,
        "message": "Idle",
        "started_at": None,
        "finished_at": None,
        "version": 0,
    }
    _listeners: list[queue.Queue] = []

    @classmethod
    def get_state(cls) -> dict[str, Any]:
        with cls._lock:
            return dict(cls._state)

    @classmethod
    def update(cls, **kwargs) -> None:
        with cls._lock:
            cls._state.update(kwargs)
            cls._state["version"] += 1
            st = dict(cls._state)
            for q in cls._listeners[:]:
                try:
                    q.put_nowait(st)
                except Exception:
                    pass

    @classmethod
    def register_listener(cls) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with cls._lock:
            cls._listeners.append(q)
        return q

    @classmethod
    def unregister_listener(cls, q: queue.Queue) -> None:
        with cls._lock:
            if q in cls._listeners:
                cls._listeners.remove(q)

STATIC_DIR = Path(__file__).parent / "static"

# --- Progressive Web App: manifest + service worker (installable full-screen) ---
MANIFEST = {
    "name": "OLX Deals",
    "short_name": "OLX Deals",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#090c10",
    "theme_color": "#090c10",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any maskable"},
        {"src": "/static/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
}

# Network-first so live data stays fresh; falls back to cache (offline shell).
SW_JS = """
const CACHE = 'olx-deals-v4';
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

from . import accounts, analytics, config, fx, scorer
from .discover import discover
from .push import Push
from .scorer import score_search, to_ron
from .store import Store

_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap');

  :root, [data-theme="dark"] {
    color-scheme: dark;
    --bg-canvas: #090c10;
    --bg-header: rgba(13, 17, 23, 0.88);
    --bg-surface: #121824;
    --bg-surface-elevated: #1a2233;
    --bg-surface-hover: #222d42;
    --border-subtle: rgba(255, 255, 255, 0.08);
    --border-medium: rgba(255, 255, 255, 0.14);
    --border-focus: rgba(99, 102, 241, 0.5);

    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
    --text-tertiary: #64748b;

    --accent-brand: #3b82f6;
    --accent-brand-bg: rgba(59, 130, 246, 0.14);
    --accent-deal: #10b981;
    --accent-deal-bg: rgba(16, 185, 129, 0.14);
    --accent-deal-border: rgba(16, 185, 129, 0.38);
    --accent-drop: #f59e0b;
    --accent-drop-bg: rgba(245, 158, 11, 0.14);
    --accent-new: #06b6d4;
    --accent-new-bg: rgba(6, 182, 212, 0.14);
    --accent-fav: #fbbf24;
    --accent-susp: #c084fc;
    --accent-susp-bg: rgba(192, 132, 252, 0.14);
    --accent-danger: #f43f5e;
    --accent-danger-bg: rgba(244, 63, 94, 0.15);

    --bg-card-unread: #161d2b;
    --bg-card-read: #0b0e15;
    --border-card-unread: rgba(255, 255, 255, 0.12);
    --border-card-read: rgba(255, 255, 255, 0.04);
    /* Read state needs its own text colour: --text-secondary is tuned for
       labels on an unread card, not for signalling "already handled". */
    --text-read: #6b7a90;
    --accent-deal-bg-read: rgba(16, 185, 129, 0.05);

    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --radius-pill: 9999px;

    --shadow-card: 0 4px 18px -2px rgba(0, 0, 0, 0.45), 0 2px 6px -1px rgba(0, 0, 0, 0.3);
    --shadow-float: 0 12px 36px -4px rgba(0, 0, 0, 0.7);
    --font-sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-heading: 'Outfit', sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
  }

  [data-theme="light"] {
    color-scheme: light;
    --bg-canvas: #edf2f7;
    --bg-header: rgba(255, 255, 255, 0.96);
    --bg-surface: #ffffff;
    --bg-surface-elevated: #f8fafc;
    --bg-surface-hover: #f1f5f9;
    --border-subtle: #cbd5e1;
    --border-medium: #94a3b8;
    --border-focus: #2563eb;

    --bg-card-unread: #ffffff;
    --bg-card-read: #ccd8e6;
    --border-card-unread: #cbd5e1;
    --border-card-read: #aab8c9;
    --text-read: #64748b;
    --accent-deal-bg-read: rgba(4, 120, 87, 0.05);

    --text-primary: #020617;
    --text-secondary: #1e293b;
    --text-tertiary: #475569;

    --accent-brand: #2563eb;
    --accent-brand-bg: #eff6ff;
    --accent-deal: #047857;
    --accent-deal-bg: #ecfdf5;
    --accent-deal-border: #34d399;
    --accent-drop: #b45309;
    --accent-drop-bg: #fffbeb;
    --accent-new: #0369a1;
    --accent-new-bg: #f0f9ff;
    --accent-fav: #b45309;
    --accent-susp: #7e22ce;
    --accent-susp-bg: #faf5ff;
    --accent-danger: #be123c;
    --accent-danger-bg: #fff1f2;

    --shadow-card: 0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04);
    --shadow-float: 0 10px 25px -3px rgba(0, 0, 0, 0.12), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font-sans);
    background: var(--bg-canvas);
    color: var(--text-primary);
    -webkit-font-smoothing: antialiased;
    padding-bottom: calc(72px + env(safe-area-inset-bottom));
    min-height: 100vh;
  }

  .wrap { max-width: 1240px; margin: 0 auto; width: 100%; padding: 0 16px; }

  /* --- Frosted Sticky Header --- */
  header {
    background: var(--bg-header);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border-subtle);
    position: sticky;
    top: 0;
    z-index: 20;
    padding: 10px 0;
    padding-top: calc(10px + env(safe-area-inset-top));
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 9px;
    text-decoration: none;
    color: var(--text-primary);
  }
  .brand-logo {
    width: 32px;
    height: 32px;
    border-radius: 9px;
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 10px rgba(59, 130, 246, 0.35);
  }
  .brand-logo svg { width: 18px; height: 18px; color: #fff; }
  .brand-text {
    font-family: var(--font-heading);
    font-size: 19px;
    font-weight: 700;
    letter-spacing: -0.3px;
  }
  .brand-badge {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: var(--radius-pill);
    background: var(--accent-deal-bg);
    color: var(--accent-deal);
    border: 1px solid var(--accent-deal-border);
    margin-left: 2px;
  }
  .actions { display: flex; align-items: center; gap: 8px; }
  .iconbtn {
    background: var(--bg-surface-elevated);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    width: 38px;
    height: 38px;
    border-radius: var(--radius-md);
    font-size: 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s ease;
    padding: 0;
  }
  .iconbtn:hover {
    background: var(--bg-surface-hover);
    color: var(--text-primary);
    border-color: var(--border-medium);
  }
  .iconbtn:active { transform: scale(0.95); }
  .iconbtn.spin svg { animation: spin 0.9s linear infinite; }
  @keyframes spin { 100% { transform: rotate(360deg); } }

  /* --- Live Sync Progress Bar --- */
  #sync-live-bar {
    display: none;
    position: relative;
    background: var(--bg-surface-elevated);
    border-bottom: 1px solid var(--border-subtle);
    padding: 8px 16px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    z-index: 19;
  }
  #sync-live-bar.active {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    animation: fadeIn 0.2s ease;
  }
  .sync-progress-track {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: rgba(59, 130, 246, 0.18);
  }
  .sync-progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, #3b82f6, #10b981);
    transition: width 0.35s ease;
  }
  .sync-live-text {
    display: flex;
    align-items: center;
    gap: 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .sync-live-text svg {
    animation: spin 0.9s linear infinite;
    color: var(--accent-brand);
    flex-shrink: 0;
  }
  .sync-live-stats {
    font-family: var(--font-mono);
    font-size: 11.5px;
    color: var(--text-secondary);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .sub-chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-top: 8px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    font-weight: 500;
    padding: 3px 8px;
    border-radius: var(--radius-pill);
    background: var(--bg-surface-elevated);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
  }
  .chip-deal {
    background: var(--accent-deal-bg);
    border-color: var(--accent-deal-border);
    color: var(--accent-deal);
    font-weight: 600;
  }
  .pulse-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-deal);
    box-shadow: 0 0 8px var(--accent-deal);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.85); }
  }

  /* --- Buttons & Inputs --- */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 7px 14px;
    border-radius: var(--radius-md);
    font-size: 13px;
    font-weight: 600;
    text-decoration: none;
    background: var(--bg-surface-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .btn:hover { background: var(--bg-surface-hover); border-color: var(--border-medium); }
  .btn:active { transform: scale(0.97); }
  .btn-primary {
    background: linear-gradient(135deg, #3b82f6, #4f46e5);
    color: #fff;
    border: none;
    box-shadow: 0 2px 10px rgba(59, 130, 246, 0.3);
  }
  .btn-primary:hover { background: linear-gradient(135deg, #2563eb, #4338ca); }
  .btn-go {
    background: var(--accent-deal-bg);
    color: var(--accent-deal);
    border: 1px solid var(--accent-deal-border);
  }
  .btn-go:hover { background: rgba(16, 185, 129, 0.22); }
  .btn-del {
    background: var(--accent-danger-bg);
    color: var(--accent-danger);
    border: 1px solid rgba(244, 63, 94, 0.3);
  }
  .btn-del:hover { background: rgba(244, 63, 94, 0.25); }

  /* --- Horizontal Pill Navigation Bar --- */
  .pills-nav {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
    padding: 10px 0 4px;
  }
  .pills-nav::-webkit-scrollbar { display: none; }
  .pill-link {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    border-radius: var(--radius-pill);
    font-size: 12px;
    font-weight: 600;
    text-decoration: none;
    color: var(--text-secondary);
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    white-space: nowrap;
    transition: all 0.15s ease;
  }
  .pill-link:hover {
    color: var(--text-primary);
    background: var(--bg-surface-elevated);
    border-color: var(--border-medium);
  }
  .pill-link.active {
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    color: #fff;
    border-color: transparent;
    box-shadow: 0 2px 10px rgba(59, 130, 246, 0.35);
  }
  .pill-link .badge-num {
    font-size: 10px;
    padding: 1px 5px;
    border-radius: var(--radius-pill);
    background: rgba(0, 0, 0, 0.25);
  }
  .pill-link.active .badge-num { background: rgba(255, 255, 255, 0.25); }

  /* --- Accordion Menu for Full Search Taxonomy --- */
  .menu {
    margin: 8px 0;
    border-radius: var(--radius-md);
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    overflow: hidden;
  }
  .menu > summary {
    list-style: none;
    cursor: pointer;
    padding: 10px 14px;
    color: var(--text-primary);
    font-size: 13px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    user-select: none;
  }
  .menu > summary::-webkit-details-marker { display: none; }
  .menu > summary .caret {
    margin-left: auto;
    color: var(--text-tertiary);
    transition: transform 0.2s ease;
  }
  .menu[open] > summary .caret { transform: rotate(180deg); }
  .menu .items {
    border-top: 1px solid var(--border-subtle);
    max-height: 380px;
    overflow-y: auto;
  }
  .menu .items a {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 14px;
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 13px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    transition: background 0.12s;
  }
  .menu .items a:hover { background: var(--bg-surface-elevated); color: var(--text-primary); }
  .menu .items a.on {
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
    font-weight: 600;
  }
  .menu .grp { border-bottom: 1px solid var(--border-subtle); }
  .menu .grp > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 14px;
    font-size: 13px;
    font-weight: 600;
  }
  .menu .grp > summary::-webkit-details-marker { display: none; }
  .menu .grp-name { color: var(--text-primary); text-decoration: none; flex: 1; }
  .menu .grp-name.on { color: #60a5fa; }
  .menu .grp-caret { font-size: 10px; color: var(--text-tertiary); transition: transform 0.15s; }
  .menu .grp[open] > summary .grp-caret { transform: rotate(90deg); }
  .menu .grp-items a { padding-left: 28px; background: rgba(0, 0, 0, 0.2); }

  /* --- Quick Client Search & Filter Bar --- */
  .toolbar {
    margin: 8px 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .quick-search-box {
    position: relative;
    display: flex;
    align-items: center;
  }
  .quick-search-box svg {
    position: absolute;
    left: 12px;
    width: 16px;
    height: 16px;
    color: var(--text-tertiary);
    pointer-events: none;
  }
  .quick-search-input {
    width: 100%;
    padding: 9px 12px 9px 36px;
    font-size: 13px;
    font-family: inherit;
    background: var(--bg-surface);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: all 0.15s ease;
  }
  .quick-search-input:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
    background: var(--bg-surface-elevated);
  }

  .filter-panel {
    border-radius: var(--radius-md);
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    overflow: hidden;
  }
  .filter-panel > summary {
    list-style: none;
    cursor: pointer;
    padding: 9px 14px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 6px;
    user-select: none;
  }
  .filter-panel > summary::-webkit-details-marker { display: none; }
  .filter-panel > summary .badge-filter {
    font-size: 10px;
    background: var(--accent-brand-bg);
    color: #60a5fa;
    padding: 1px 6px;
    border-radius: var(--radius-pill);
  }
  .filter-form {
    padding: 12px 14px;
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }
  .filter-group { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
  .filter-form select, .filter-form input.px {
    background: var(--bg-surface-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 6px 9px;
    font-size: 12px;
    font-family: inherit;
  }
  .filter-form select:focus, .filter-form input.px:focus {
    outline: none;
    border-color: #3b82f6;
  }
  .filter-form input.px { width: 78px; }
  .toggle-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
  }

  /* --- Card Grid Layout --- */
  .cards {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 10px;
  }
  @media (min-width: 760px) {
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
      gap: 14px;
    }
  }

  /* --- Refined Listing Card --- */
  .card {
    position: relative;
    display: flex;
    gap: 12px;
    padding: 12px;
    background: var(--bg-card-unread);
    border: 1px solid var(--border-card-unread);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-card);
    transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease, background 0.18s ease;
    user-select: none;
    -webkit-touch-callout: none;
    touch-action: pan-y;
  }
  .card:hover {
    border-color: var(--border-medium);
    transform: translateY(-2px);
    box-shadow: 0 8px 24px -4px rgba(0, 0, 0, 0.18);
  }
  .card.deal {
    border-color: var(--accent-deal-border);
    background: linear-gradient(180deg, var(--accent-deal-bg) 0%, var(--bg-card-unread) 100%);
  }
  /* Unread marker.
     Dimming alone cannot carry this state: a read card can only recede so far
     before it stops being readable, and on a .deal card the green gradient
     masks what little difference is left. So unread gets a positive mark and
     read is simply its absence -- the same reason mail clients use a dot
     rather than a slightly paler row. One colour for one meaning: the rail
     says "unread" regardless of whether the card is also a deal. */
  .card:not(.seen)::before {
    content: '';
    position: absolute;
    left: 0;
    top: 10px;
    bottom: 10px;
    width: 3px;
    border-radius: 0 3px 3px 0;
    background: var(--accent-brand);
  }
  .card.deal.seen {
    border-color: var(--border-card-read);
    background: linear-gradient(180deg, var(--accent-deal-bg-read) 0%, var(--bg-card-read) 100%);
  }
  .card.seen {
    background: var(--bg-card-read);
    border-color: var(--border-card-read);
    box-shadow: none;
  }
  .card.seen .title a {
    color: var(--text-read);
    font-weight: 400;
  }
  .card.seen .thumb-wrap {
    border-color: var(--border-card-read);
  }
  /* The photo is the largest object in the card, so it carries the state
     further down the page than any text treatment can. */
  .card.seen .thumb {
    opacity: 0.5;
    filter: grayscale(0.4);
  }
  .card.sw-fav { box-shadow: inset 8px 0 0 0 var(--accent-fav), var(--shadow-card); }
  .card.sw-seen { box-shadow: inset -8px 0 0 0 var(--accent-brand), var(--shadow-card); }
  /* While a swipe is in progress its feedback owns the card edges: the gold
     favourite bar lands on the same left edge as the unread rail. */
  .card.sw-fav::before,
  .card.sw-seen::before { opacity: 0; }

  /* Absolute card action buttons */
  .fav-btn, .hide-btn {
    position: absolute;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--bg-surface-elevated);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 3;
    transition: all 0.15s ease;
  }
  .fav-btn { top: 8px; left: 8px; font-size: 15px; }
  .fav-btn.on { color: var(--accent-fav); border-color: rgba(251, 191, 36, 0.5); background: rgba(251, 191, 36, 0.15); }
  .hide-btn { top: 8px; right: 8px; font-size: 13px; }
  .hide-btn:hover { background: var(--accent-danger-bg); color: var(--accent-danger); border-color: var(--accent-danger); }

  /* Card Image / Thumbnail */
  .imglink { flex: none; display: block; }
  .thumb-wrap {
    position: relative;
    width: 92px;
    height: 92px;
    border-radius: var(--radius-md);
    overflow: hidden;
    background: var(--bg-surface-elevated);
    border: 1px solid var(--border-subtle);
  }
  .thumb {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 0.25s ease;
  }
  .card:hover .thumb { transform: scale(1.04); }
  .thumb-placeholder {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-tertiary);
  }

  /* Card Body */
  .body { min-width: 0; flex: 1; display: flex; flex-direction: column; }
  .title {
    font-size: 13.5px;
    font-weight: 600;
    line-height: 1.35;
    margin-bottom: 4px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .title a {
    color: var(--text-primary);
    text-decoration: none;
    transition: color 0.12s;
  }
  .title a:hover { color: #60a5fa; }

  .price-row {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-bottom: 5px;
  }
  .price {
    font-family: var(--font-mono);
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: var(--text-primary);
  }
  .orig {
    font-size: 11px;
    color: var(--text-tertiary);
    font-family: var(--font-sans);
  }

  /* Badges */
  .badges-row {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 5px;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 10.5px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: var(--radius-pill);
    letter-spacing: 0.1px;
  }
  .b-deal {
    background: var(--accent-deal-bg);
    color: var(--accent-deal);
    border: 1px solid var(--accent-deal-border);
  }
  .b-new {
    background: var(--accent-new-bg);
    color: var(--accent-new);
    border: 1px solid rgba(6, 182, 212, 0.45);
  }
  .b-drop {
    background: var(--accent-drop-bg);
    color: var(--accent-drop);
    border: 1px solid rgba(245, 158, 11, 0.45);
  }
  .b-dealer {
    background: var(--bg-surface-elevated);
    color: var(--text-secondary);
    border: 1px solid var(--border-subtle);
  }
  .b-susp {
    background: var(--accent-susp-bg);
    color: var(--accent-susp);
    border: 1px solid rgba(192, 132, 252, 0.45);
  }
  .ai-badge { cursor: pointer; }
  .ai-good { background: var(--accent-deal-bg); color: var(--accent-deal); border: 1px solid var(--accent-deal-border); }
  .ai-mid { background: var(--accent-drop-bg); color: var(--accent-drop); border: 1px solid rgba(245, 158, 11, 0.45); }
  .ai-bad { background: var(--accent-danger-bg); color: var(--accent-danger); border: 1px solid rgba(244, 63, 94, 0.45); }

  /* Metadata & Location */
  .meta-row {
    font-size: 11.5px;
    color: var(--text-tertiary);
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .meta-row svg { width: 12px; height: 12px; vertical-align: -1px; }

  /* Price Sparkline */
  .spark-wrap { margin-top: 4px; }
  .was-row {
    font-size: 11px;
    display: flex;
    align-items: center;
    gap: 5px;
    margin-bottom: 2px;
  }
  .was { text-decoration: line-through; color: var(--text-tertiary); font-family: var(--font-mono); }
  .drop-pct { color: var(--accent-drop); font-weight: 700; font-family: var(--font-mono); }
  .spark { display: block; overflow: visible; }

  /* Action Buttons on Card Bottom */
  .card-footer {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }
  .seen-btn {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-surface-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 3px 8px;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .seen-btn:hover { color: var(--text-primary); border-color: var(--border-medium); }
  .seen-btn.on { color: var(--accent-deal); background: var(--accent-deal-bg); border-color: var(--accent-deal-border); }
  .ai-action-btn {
    font-size: 11px;
    font-weight: 600;
    color: #c084fc;
    background: var(--accent-susp-bg);
    border: 1px solid rgba(192, 132, 252, 0.3);
    border-radius: var(--radius-sm);
    padding: 3px 8px;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .ai-action-btn:hover { background: rgba(192, 132, 252, 0.22); }

  /* AI Panel */
  .ai-panel {
    margin-top: 8px;
    padding: 10px 12px;
    background: rgba(10, 14, 22, 0.9);
    border: 1px solid var(--border-medium);
    border-radius: var(--radius-md);
    font-size: 12px;
    line-height: 1.5;
    color: #cbd5e1;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  }
  .ai-panel ul { margin: 6px 0 6px 18px; color: #fca5a5; }
  .ai-meta { color: var(--text-tertiary); font-size: 11px; margin-top: 6px; }

  /* --- Candlestick & Trends View --- */
  .chart-card {
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: 14px;
    margin: 10px 0 16px;
    box-shadow: var(--shadow-card);
  }
  .chart-title {
    font-family: var(--font-heading);
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .candles { width: 100%; height: auto; display: block; }

  /* --- Management View & Forms --- */
  .mng-box {
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: 16px;
    margin: 12px 0;
    box-shadow: var(--shadow-card);
  }
  .mng-box h2 {
    font-family: var(--font-heading);
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 10px;
    color: var(--text-primary);
  }
  .form-label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 10px 0 4px;
  }
  .form-input, .form-select {
    width: 100%;
    padding: 9px 12px;
    font-size: 14px;
    font-family: inherit;
    background: var(--bg-surface-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: border-color 0.15s;
  }
  .form-input:focus, .form-select:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
  }
  .row2 { display: flex; gap: 10px; }
  .row2 > div { flex: 1; }

  .srow {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px;
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    margin-bottom: 8px;
    transition: border-color 0.15s;
  }
  .srow:hover { border-color: var(--border-medium); }
  .srow .info { flex: 1; min-width: 0; }
  .srow .k { font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 6px; }
  .srow .d { color: var(--text-tertiary); font-size: 12px; margin-top: 2px; }
  .srow.paused { opacity: 0.6; }

  /* Discovery Chips */
  .chip-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: var(--radius-pill);
    cursor: pointer;
    margin: 3px 4px 3px 0;
    transition: transform 0.12s, box-shadow 0.12s;
  }
  .chip-pill:hover { transform: scale(1.04); }
  .chip-pill:active { transform: scale(0.96); }

  /* --- Floating Glass Tabbar --- */
  .tabbar {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    background: var(--bg-header);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-top: 1px solid var(--border-subtle);
    padding-bottom: env(safe-area-inset-bottom);
  }
  .tabbar .wrap { display: flex; padding: 0 8px; }
  .tabbar a {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 3px;
    padding: 8px 4px 7px;
    color: var(--text-tertiary);
    text-decoration: none;
    font-size: 10.5px;
    font-weight: 600;
    transition: all 0.15s ease;
  }
  .tabbar a svg { width: 20px; height: 20px; stroke-width: 2.1; transition: transform 0.15s; }
  .tabbar a:hover { color: var(--text-secondary); }
  .tabbar a.active { color: #60a5fa; }
  .tabbar a.active svg { transform: translateY(-1px); color: #3b82f6; }

  /* --- Flash Messages & Toast System --- */
  .flash {
    margin: 10px 0;
    padding: 10px 14px;
    border-radius: var(--radius-md);
    background: rgba(16, 185, 129, 0.12);
    color: #6ee7b7;
    border: 1px solid rgba(16, 185, 129, 0.3);
    font-size: 13px;
  }
  .flash.warn {
    background: rgba(244, 63, 94, 0.12);
    color: #fda4af;
    border-color: rgba(244, 63, 94, 0.3);
  }

  #toast-box {
    position: fixed;
    bottom: calc(76px + env(safe-area-inset-bottom));
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 100;
    pointer-events: none;
    width: 90%;
    max-width: 380px;
  }
  .toast {
    padding: 10px 16px;
    border-radius: var(--radius-pill);
    background: rgba(18, 24, 38, 0.95);
    backdrop-filter: blur(12px);
    border: 1px solid var(--border-medium);
    box-shadow: var(--shadow-float);
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    animation: toastIn 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
    transition: opacity 0.2s, transform 0.2s;
  }
  .toast.toast-success { border-color: var(--accent-deal); color: #6ee7b7; }
  .toast.toast-warn { border-color: var(--accent-drop); color: #fde68a; }
  @keyframes toastIn {
    from { opacity: 0; transform: translateY(16px) scale(0.95); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* Empty State */
  .empty {
    padding: 48px 16px;
    text-align: center;
    color: var(--text-tertiary);
    font-size: 14px;
    line-height: 1.6;
  }

  /* Gate (Invite required) */
  .gate-wrap {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .gate-card {
    width: 100%;
    max-width: 420px;
    background: var(--bg-surface);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: 24px;
    box-shadow: var(--shadow-float);
  }
  .gate-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }
  .gate-header h1 {
    font-family: var(--font-heading);
    font-size: 20px;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.02em;
  }
  .gate-lead {
    color: var(--text-secondary);
    font-size: 13.5px;
    line-height: 1.5;
    margin-bottom: 16px;
  }
  .code-input {
    font-family: var(--font-mono);
    font-size: 16px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    text-align: center;
    font-weight: 600;
  }
  .gate-warn {
    margin-top: 16px;
    padding: 12px 14px;
    background: rgba(224, 86, 36, 0.1);
    border: 1px solid rgba(224, 86, 36, 0.3);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: 13px;
    line-height: 1.45;
  }
  .gate-hint {
    margin-top: 18px;
    padding: 12px;
    background: var(--bg-subtle);
    border-radius: var(--radius-md);
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.5;
  }
  .gate-hint strong {
    color: var(--text-primary);
  }
"""

_GATE_PAGE = """<!doctype html>
<html lang="ro"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>OLX Deals — Necesită invitație</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#090c10">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OLX Deals">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<script>
(function() {{
  try {{
    var t = localStorage.getItem('olx_theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme', t);
  }} catch(e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>{css}</style></head><body>
<div class="gate-wrap">
  <div class="gate-card">
    <div class="gate-header">
      <div class="brand-logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/></svg></div>
      <h1 id="gate-title">{title}</h1>
    </div>
    <p class="gate-lead" id="gate-lead">{lead}</p>
    {error_html}
    <form id="form-code" method="post" action="/activate">
      <input type="hidden" name="next" value="{next}">
      <input class="form-input code-input" id="invite-code" name="code" value="{code_value}"
             autocomplete="off" autocapitalize="characters" spellcheck="false"
             placeholder="ABCD-EFGH-JKLM" aria-label="Cod de invitație" required autofocus>
      <button class="btn btn-primary" type="submit" id="btn-activate" style="width:100%;margin-top:12px;padding:12px;font-size:15px">{btn_label}</button>
    </form>
    <p class="err" id="gate-err" style="display:none;margin-top:12px;color:var(--bad);font-size:13px"></p>

    <div class="gate-warn" id="gate-inapp" style="display:none">
      <strong>Ai deschis pagina în browserul din WhatsApp.</strong>
      <p style="margin:4px 0 8px 0;font-size:12.5px;line-height:1.4">Acesta are cookie-uri proprii, așa că aplicația instalată ulterior nu va fi autentificată. Continuă în browserul propriu-zis — codul te însoțește.</p>
      <div class="warn-actions" style="display:flex;gap:8px;margin-top:8px">
        <a class="btn btn-sm btn-ghost" id="open-chrome" href="#" style="display:none">Deschide în Chrome</a>
        <button class="btn btn-sm btn-ghost" id="copy-code" type="button">Copiază codul</button>
      </div>
      <p id="inapp-note" style="margin-top:8px;font-size:11.5px;color:var(--text-secondary)"></p>
    </div>

    <div class="gate-hint">
      <strong>Pe iPhone:</strong> adaugă întâi pagina pe ecranul principal (Distribuie → Adaugă pe ecranul principal), apoi deschide-o de acolo și introdu codul. Notificările funcționează doar din aplicația instalată.
    </div>
  </div>
</div>
<script>
(function() {{
  function inAppBrowser() {{
    var ua = navigator.userAgent || '';
    return /\\bwv\\b/.test(ua) || /(FBAN|FBAV|Instagram|Line\\/|WhatsApp|Snapchat|Messenger)/i.test(ua);
  }}
  function isAndroid() {{ return /Android/i.test(navigator.userAgent || ''); }}

  var code = document.getElementById('invite-code').value.trim();
  var box = document.getElementById('gate-inapp');
  if (box && inAppBrowser()) {{
    box.style.display = 'block';
    var url = location.origin + '/?code=' + encodeURIComponent(code);
    var chrome = document.getElementById('open-chrome');
    if (isAndroid() && code && chrome) {{
      chrome.href = 'intent://' + url.replace(/^https?:\\/\\//, '') + '#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url=' + encodeURIComponent(url) + ';end';
      chrome.style.display = 'inline-flex';
    }}
    var note = document.getElementById('inapp-note');
    if (note) {{
      note.textContent = isAndroid()
        ? 'În Chrome: instalează aplicația din meniul ⋮, deschide-o de pe ecranul principal, apoi introdu codul. Poți activa de mai multe ori în prima oră.'
        : 'Deschide ' + location.host + ' în Safari, adaugă pagina pe ecranul principal, deschide-o de acolo, apoi introdu codul.';
    }}
    var copyBtn = document.getElementById('copy-code');
    if (copyBtn) {{
      copyBtn.onclick = function() {{
        var c = document.getElementById('invite-code').value;
        if (navigator.clipboard && window.isSecureContext) {{
          navigator.clipboard.writeText(c).then(function() {{ copyBtn.textContent = 'Copiat'; setTimeout(function(){{copyBtn.textContent='Copiază codul';}}, 1500); }});
        }} else {{
          var ta = document.createElement('textarea');
          ta.value = c;
          ta.style.position = 'fixed';
          ta.style.top = '0';
          ta.style.left = '0';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          try {{ document.execCommand('copy'); copyBtn.textContent = 'Copiat'; setTimeout(function(){{copyBtn.textContent='Copiază codul';}}, 1500); }} catch(e){{}}
          document.body.removeChild(ta);
        }}
      }};
    }}
  }}

  var form = document.getElementById('form-code');
  if (form) {{
    form.addEventListener('submit', async function(e) {{
      if (window.fetch) {{
        e.preventDefault();
        var btn = document.getElementById('btn-activate');
        var err = document.getElementById('gate-err');
        var codeVal = document.getElementById('invite-code').value.trim();
        err.style.display = 'none';
        btn.disabled = true;
        btn.textContent = 'Se activează…';
        try {{
          var res = await fetch('/api/invites/redeem', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{code: codeVal}})
          }});
          var data = await res.json().catch(function(){{ return {{}}; }});
          if (!res.ok) throw new Error(data.detail || 'Activarea a eșuat (' + res.status + ')');
          location.href = '{next}' || '/';
        }} catch(ex) {{
          err.textContent = ex.message || 'Activarea a eșuat.';
          err.style.display = 'block';
          btn.disabled = false;
          btn.textContent = '{btn_label}';
        }}
      }}
    }});
  }}
}})();
</script>
</body></html>"""


def render_gate(prefill: str = "", error: str = "", next_path: str = "/") -> str:
    has_code = bool(prefill and prefill.strip())
    title = "Activează acest dispozitiv" if has_code else "Necesită invitație"
    lead = ("Această invitație înregistrează dispozitivul pe care o citești acum."
            if has_code else
            "Aplicația este doar pe bază de invitație. Dacă ai primit un link sau un cod, "
            "introdu-l mai jos ca să înregistrezi acest dispozitiv.")
    btn_label = "Activează acest dispozitiv" if has_code else "Activează"
    error_html = (f'<div class="flash warn" style="margin-bottom:14px">{html.escape(error)}</div>'
                  if error else "")
    return _GATE_PAGE.format(
        css=_CSS,
        title=title,
        lead=lead,
        btn_label=btn_label,
        code_value=html.escape(prefill or ""),
        error_html=error_html,
        next=html.escape(next_path or "/"),
    )


_SHELL = """<!doctype html>
<html lang="ro"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>OLX Deals</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#090c10">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OLX Deals">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<script>
(function() {{
  try {{
    var t = localStorage.getItem('olx_theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme', t);
  }} catch(e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>{css}</style></head><body>
<header>
  <div class="wrap">
    <div class="topbar">
      <a class="brand" href="/">
        <div class="brand-logo">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/></svg>
        </div>
        <span class="brand-text">OLX Deals</span>
      </a>
      <div class="actions">
        <button class="iconbtn" id="theme-btn" type="button" onclick="toggleTheme()" title="Toggle light/dark mode">
          <svg id="theme-ic-sun" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
          <svg id="theme-ic-moon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        </button>
        <form method="post" action="/sync" onsubmit="handleSync(event, this)">
          <button class="iconbtn" id="sync-btn" type="submit" title="Sync now">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.19"/></svg>
          </button>
        </form>
        <button class="iconbtn" type="button" onclick="enableNotifs()" title="Enable deal alerts">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        </button>
      </div>
    </div>
    <div class="sub-chips">{sub_chips}</div>
  </div>
</header>

<div id="sync-live-bar">
  <div class="sync-live-text">
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.19"/></svg>
    <span id="sync-status-msg">Starting sync...</span>
  </div>
  <div class="sync-live-stats" id="sync-stats-msg">0/0</div>
  <div class="sync-progress-track">
    <div class="sync-progress-fill" id="sync-progress-fill"></div>
  </div>
</div>

<div class="wrap">
  {flash}
</div>

<main class="wrap">
{content}
</main>

<div id="toast-box"></div>

<nav class="tabbar"><div class="wrap">
  <a href="{deals_href}" class="{deals_active}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
    Deals
  </a>
  <a href="{drops_href}" class="{drops_active}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/></svg>
    Drops
  </a>
  <a href="/saved" class="{saved_active}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
    Saved
  </a>
  <a href="{trends_href}" class="{trends_active}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
    Trends
  </a>
  <a href="/searches" class="{manage_active}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    Manage
  </a>
</div></nav>

<script>
// --- Non-blocking Toast system ---
function showToast(msg, type) {{
  var box = document.getElementById('toast-box');
  if (!box) return;
  var el = document.createElement('div');
  el.className = 'toast ' + (type === 'success' ? 'toast-success' : type === 'warn' ? 'toast-warn' : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(function() {{
    el.style.opacity = '0';
    el.style.transform = 'translateY(10px) scale(0.95)';
    setTimeout(function() {{ el.remove(); }}, 200);
  }}, 2800);
}}

// --- Live Sync Stream & Auto-Reload ---
var syncEvtSource = null;
var wasSyncRunning = false;

function initSyncStream() {{
  if (!!window.EventSource) {{
    syncEvtSource = new EventSource('/api/sync/events');
    syncEvtSource.onmessage = function(e) {{
      try {{
        var data = JSON.parse(e.data);
        handleSyncUpdate(data);
      }} catch(err) {{}}
    }};
    syncEvtSource.onerror = function() {{
      setTimeout(pollSyncStatus, 2500);
    }};
  }} else {{
    pollSyncStatus();
  }}
}}

function pollSyncStatus() {{
  fetch('/api/sync/status')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      handleSyncUpdate(data);
      if (data.running) setTimeout(pollSyncStatus, 1500);
    }})
    .catch(function() {{}});
}}

function handleSyncUpdate(data) {{
  var bar = document.getElementById('sync-live-bar');
  var msgEl = document.getElementById('sync-status-msg');
  var statsEl = document.getElementById('sync-stats-msg');
  var fillEl = document.getElementById('sync-progress-fill');
  var syncBtn = document.getElementById('sync-btn');

  if (!bar) return;

  if (data.running) {{
    wasSyncRunning = true;
    bar.classList.add('active');
    if (syncBtn) syncBtn.classList.add('spin');
    if (msgEl) msgEl.textContent = data.message || 'Syncing in background...';
    var pct = data.total > 0 ? Math.round((data.step / data.total) * 100) : 10;
    if (fillEl) fillEl.style.width = Math.min(100, Math.max(6, pct)) + '%';
    if (statsEl) {{
      var dealTxt = data.deal_count > 0 ? (' · ' + data.deal_count + ' deal' + (data.deal_count > 1 ? 's' : '')) : '';
      statsEl.textContent = (data.step || 0) + '/' + (data.total || 0) + dealTxt;
    }}
  }} else {{
    if (syncBtn) syncBtn.classList.remove('spin');
    if (wasSyncRunning) {{
      wasSyncRunning = false;
      if (fillEl) fillEl.style.width = '100%';
      if (msgEl) msgEl.textContent = 'Sync finished!';
      setTimeout(function() {{
        bar.classList.remove('active');
      }}, 1400);

      var newCount = data.new_count || 0;
      var dealCount = data.deal_count || 0;
      var toastMsg = 'Sync complete! ' + (newCount > 0 ? (newCount + ' new item' + (newCount > 1 ? 's' : '') + (dealCount > 0 ? ', ' + dealCount + ' deal(s)' : '')) : 'No new listings');
      showToast(toastMsg, 'success');
      refreshPageContent();
    }} else {{
      bar.classList.remove('active');
    }}
  }}
}}

function refreshPageContent() {{
  fetch(window.location.href, {{ headers: {{'X-Requested-With': 'XMLHttpRequest'}} }})
    .then(function(r) {{ return r.text(); }})
    .then(function(html) {{
      var parser = new DOMParser();
      var doc = parser.parseFromString(html, 'text/html');

      var newMain = doc.querySelector('main.wrap');
      var curMain = document.querySelector('main.wrap');
      if (newMain && curMain) {{
        curMain.innerHTML = newMain.innerHTML;
      }}

      var newChips = doc.querySelector('.sub-chips');
      var curChips = document.querySelector('.sub-chips');
      if (newChips && curChips) {{
        curChips.innerHTML = newChips.innerHTML;
      }}

      var searchInput = document.getElementById('client-search-input');
      if (searchInput && searchInput.value) {{
        filterCards(searchInput.value);
      }}
    }})
    .catch(function() {{}});
}}

function handleSync(e, form) {{
  e.preventDefault();
  var btn = document.getElementById('sync-btn');
  if (btn) btn.classList.add('spin');
  var bar = document.getElementById('sync-live-bar');
  if (bar) bar.classList.add('active');
  var msgEl = document.getElementById('sync-status-msg');
  if (msgEl) msgEl.textContent = 'Starting background sync...';

  fetch('/sync', {{
    method: 'POST',
    headers: {{'Accept': 'application/json'}}
  }}).then(function() {{
    pollSyncStatus();
  }}).catch(function() {{}});
}}

initSyncStream();

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
    if (r.ok) {{
      if (el) {{
        el.style.transition = 'all 0.25s ease';
        el.style.opacity = '0';
        el.style.transform = 'scale(0.9)';
        setTimeout(function() {{ el.remove(); }}, 250);
      }}
      showToast('Listing hidden from tracking', 'warn');
    }}
    else {{ showToast('Could not hide (error ' + r.status + ')', 'warn'); }}
  }}).catch(function(err) {{
    showToast('Failed to hide: ' + err, 'warn');
  }});
}}
function askHide(card) {{
  if (!card) return;
  if (confirm('Hide this listing from tracking?\\nIt stays hidden on future syncs — restore it from Manage.'))
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
    function(on) {{
      btn.textContent = on ? '★' : '☆';
      showToast(on ? 'Saved to Favorites' : 'Removed from Favorites', 'success');
    }});
}}
function toggleSeen(e, btn) {{
  toggleFlag(e, btn, '/seen', 'seen',
    function(on) {{
      btn.textContent = on ? 'seen ✓' : 'mark seen';
      showToast(on ? 'Marked as seen' : 'Marked as unread');
    }});
}}

// Fast client-side quick filter
function filterCards(q) {{
  var term = (q || '').toLowerCase().trim();
  var count = 0;
  document.querySelectorAll('.card[data-id]').forEach(function(c) {{
    var text = (c.innerText || '').toLowerCase();
    var match = !term || text.indexOf(term) !== -1;
    c.style.display = match ? '' : 'none';
    if (match) count++;
  }});
  var counter = document.getElementById('visible-count');
  if (counter) counter.textContent = count + ' visible';
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
  btn.textContent = 'analyzing…';
  var card = btn.closest('.card');
  showToast('Running AI inspection...', 'info');
  fetch('/analyze', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'id=' + encodeURIComponent(card.dataset.id)
  }}).then(function(r) {{
    if (r.ok) {{ location.reload(); }}
    else {{
      r.text().then(function(t) {{
        showToast('Analysis failed: ' + (t || r.status), 'warn');
        delete btn.dataset.busy; btn.textContent = '✦ analyze';
      }});
    }}
  }}).catch(function(err) {{
    showToast('Analysis failed: ' + err, 'warn');
    delete btn.dataset.busy; btn.textContent = '✦ analyze';
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
    btn.textContent = on ? 'seen ✓' : 'mark seen'; }}
  showToast(on ? 'Marked seen' : 'Marked unread');
}}
function swipeFav(card) {{  // swipe right -> toggle favorite
  var btn = card.querySelector('.fav-btn');
  var on = !(btn && btn.classList.contains('on'));
  setFlag(card, '/favorite', on);
  if (btn) {{ btn.classList.toggle('on', on);
    btn.textContent = on ? '★' : '☆'; }}
  showToast(on ? 'Saved to Favorites' : 'Removed from Favorites', 'success');
}}

// Per-card gestures: title/image link to OLX; tap body toggles AI; swipe/long-press.
document.querySelectorAll('.card[data-id]').forEach(function(card) {{
  var timer = null, fired = false;
  var startX = 0, startY = 0, dx = 0, swiping = false, swiped = false;

  function settle() {{
    card.style.transition = 'transform .2s ease';
    card.style.transform = '';
    card.classList.remove('sw-fav', 'sw-seen');
  }}

  card.addEventListener('touchstart', function(e) {{
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    dx = 0; swiping = false; swiped = false; fired = false;
    card.style.transition = '';
    timer = setTimeout(function() {{ fired = true; askHide(card); }}, 520);
  }}, {{passive: true}});

  card.addEventListener('touchmove', function(e) {{
    var mx = e.touches[0].clientX - startX, my = e.touches[0].clientY - startY;
    if (!swiping) {{
      if (Math.abs(mx) > 12 && Math.abs(mx) > Math.abs(my) * 1.5) {{
        swiping = true; clearTimeout(timer);
      }} else {{
        if (Math.abs(my) > 10) clearTimeout(timer);
        return;
      }}
    }}
    dx = mx;
    e.preventDefault();
    card.style.transform = 'translateX(' + dx + 'px)';
    card.classList.toggle('sw-fav', dx > 45);
    card.classList.toggle('sw-seen', dx < -45);
  }}, {{passive: false}});

  ['touchend', 'touchcancel'].forEach(function(ev) {{
    card.addEventListener(ev, function() {{
      clearTimeout(timer);
      if (!swiping) return;
      if (dx <= -75) swipeSeen(card);
      else if (dx >= 75) swipeFav(card);
      settle();
      swiped = true; swiping = false;
    }});
  }});

  card.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
  card.addEventListener('click', function(e) {{
    if (fired || swiped) {{
      e.preventDefault(); e.stopPropagation(); fired = false; swiped = false; return;
    }}
    if (e.target.closest('.olx, .fav-btn, .hide-btn, .seen-btn, .ai-badge, .ai-panel, .ai-action-btn'))
      return;
    var panel = card.querySelector('.ai-panel');
    if (panel) panel.hidden = !panel.hidden;
  }});
}});

// Register service worker
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
    showToast('Push notifications not supported in this browser', 'warn'); return;
  }}
  if (!window.isSecureContext) {{
    showToast('Open via HTTPS address to enable alerts', 'warn'); return;
  }}
  try {{
    var perm = await Notification.requestPermission();
    if (perm !== 'granted') {{ showToast('Notification permission denied', 'warn'); return; }}
    var reg = await navigator.serviceWorker.ready;
    var key = (await (await fetch('/push/public-key')).json()).key;
    var sub = await reg.pushManager.subscribe({{
      userVisibleOnly: true, applicationServerKey: urlB64ToUint8(key)
    }});
    await fetch('/push/subscribe', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(sub)
    }});
    await fetch('/push/test', {{method: 'POST'}});
    showToast('Deal alerts enabled! Test notification sent.', 'success');
  }} catch (err) {{
    showToast('Could not enable alerts: ' + err, 'warn');
  }}
}}

function updateThemeIcons(theme) {{
  var sun = document.getElementById('theme-ic-sun');
  var moon = document.getElementById('theme-ic-moon');
  var metaTheme = document.querySelector('meta[name="theme-color"]');
  if (theme === 'light') {{
    if (sun) sun.style.display = 'none';
    if (moon) moon.style.display = 'block';
    if (metaTheme) metaTheme.setAttribute('content', '#f4f6f8');
  }} else {{
    if (sun) sun.style.display = 'block';
    if (moon) moon.style.display = 'none';
    if (metaTheme) metaTheme.setAttribute('content', '#090c10');
  }}
}}

function toggleTheme() {{
  var cur = document.documentElement.getAttribute('data-theme') || 'dark';
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try {{ localStorage.setItem('olx_theme', next); }} catch(e) {{}}
  updateThemeIcons(next);
  showToast(next === 'light' ? '☀️ Light mode enabled' : '🌙 Dark mode enabled');
}}

(function() {{
  var curTh = document.documentElement.getAttribute('data-theme') || 'dark';
  updateThemeIcons(curTh);
}})();
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
    return (f'<div class="flash warn">⚠️ Last sync failed for {names}. '
            f'Data may be stale — check system logs.</div>')


def _last_sync_text(store: Store) -> str:
    runs = store.last_runs()
    if not runs:
        return "never synced"
    latest = max(r["ts"] for r in runs.values())
    return f"synced {_time_ago(latest)}"


def _sub_chips_html(scope_txt: str, shown_deals: int, store: Store,
                    market_chip: str = "") -> str:
    n24, c24 = store.ai_cost(24)
    nT, cT = store.ai_cost()
    ai_chip = f'<span class="chip">✦ AI ${c24:.2f}/24h</span>' if nT else ""
    deal_chip = (f'<span class="chip chip-deal"><span class="pulse-dot"></span> '
                 f'{shown_deals} deal(s)</span>' if shown_deals > 0
                 else '<span class="chip">0 deals</span>')
    return (
        f'<span class="chip">🔍 {html.escape(scope_txt)}</span>'
        f'{deal_chip}'
        f'<span class="chip">🕒 {_last_sync_text(store)}</span>'
        f'<span class="chip">💶 1€ = {scorer.EUR_TO_RON:.2f} RON</span>'
        f'{market_chip}'
        f'{ai_chip}'
    )


def _shell(sub_chips: str, content: str, active: str, flash: str = "") -> str:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return _SHELL.format(
        css=_CSS, sub_chips=sub_chips, content=content, flash=flash_html,
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

DISPLAY_CAP = 100  # max cards rendered per Deals view


def _ron_series(history: list | None) -> list[float]:
    """Normalise a price-history series to RON floats, dropping blanks."""
    out: list[float] = []
    for h in history or []:
        v = to_ron(h.get("price"), h.get("currency"))
        if v is not None and v > 0:
            out.append(v)
    return out


def _sparkline(series: list[float], w: int = 140, h: int = 28) -> str:
    """Polished SVG gradient sparkline with smooth polyline."""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1.0
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = 2 + (w - 4) * i / (n - 1)
        y = 3 + (h - 6) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    last, first = series[-1], series[0]
    color = "#10b981" if last < first else "#f59e0b" if last > first else "#94a3b8"
    grad_id = f"spk_{abs(hash(tuple(series))) % 100000}"
    area_pts = pts[0] + " " + " ".join(pts) + f" {w-2:.1f},{h-1} 2,{h-1}"
    return (
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.28"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0.0"/>'
        f'</linearGradient></defs>'
        f'<polygon fill="url(#{grad_id})" points="{area_pts}"/>'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" points="{" ".join(pts)}"/>'
        f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="2.5" fill="{color}"/>'
        f'</svg>'
    )


def _ai_bits(analysis: dict | None) -> tuple[str, str, str]:
    """(badge_html, panel_html, action_html) for a listing's LLM verdict."""
    if not analysis:
        return "", "", ('<span class="ai-action-btn" '
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
  <div style="font-weight:700;margin-bottom:3px">{html.escape(v.get('summary', ''))}</div>
  <div>Condition: {html.escape(v.get('condition_summary', ''))}</div>
  {'<ul>' + flags + '</ul>' if flags else ''}
  <div style="margin-top:4px">💡 {html.escape(v.get('negotiation_tip', ''))}</div>
  <div class="ai-meta">Scam risk: <b>{html.escape(str(risk).upper())}</b> · Photos match:
    {'✓ yes' if v.get('photos_match_description') else '<b style="color:#f43f5e">NO</b>'}</div>
</div>"""
    badge = (f'<span class="badge ai-badge {cls}" '
             f'onclick="toggleAi(event,this)">✦ AI {score}</span>')
    return badge, panel, ""


def _card(sl, history: list | None = None, search_label: str | None = None,
          analysis: dict | None = None) -> str:
    r = sl.raw
    title = html.escape(r.get("title") or "—")
    url = html.escape(r.get("url") or "#")
    cur = r.get("currency") or ""
    price_val = f"{sl.price_ron:,.0f} RON".replace(",", ".") if sl.price_ron is not None else "—"
    orig = ""
    if cur == "EUR" and r.get("price") is not None:
        orig = f'<span class="orig">({r["price"]:.0f} EUR)</span>'
    thumb = html.escape(r.get("photo") or "")
    if thumb:
        img_html = f'<img class="thumb" loading="lazy" src="{thumb}" alt="{title}">'
    else:
        img_html = ('<div class="thumb-placeholder"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></div>')

    badges = ""
    if _is_recent(r.get("first_seen"), 24):
        badges += '<span class="badge b-new">✦ NEW</span>'
    if sl.is_deal:
        badges += f'<span class="badge b-deal">⚡ −{sl.deal_score*100:.0f}% deal</span>'
    elif sl.suspicious:
        badges += f'<span class="badge b-susp">⚠ too cheap −{sl.deal_score*100:.0f}%</span>'
    if r.get("is_business"):
        badges += '<span class="badge b-dealer">🏢 dealer</span>'
    pp = r.get("previous_price")
    if pp is not None and r.get("price") is not None and pp > r["price"]:
        badges += '<span class="badge b-drop">↓ price drop</span>'

    meta_bits = []
    if r.get("city"):
        meta_bits.append(f'<span>📍 {html.escape(r["city"])}</span>')
    posted = _time_ago(r.get("created_time"))
    if posted and posted != "never":
        meta_bits.append(f'<span>🕒 {posted}</span>')
    if search_label:
        meta_bits.append(f'<span class="chip" style="font-size:10px;padding:1px 6px">{html.escape(search_label)}</span>')
    meta_html = "".join(meta_bits)

    # Price history
    series = _ron_series(history)
    trend = ""
    if len(series) >= 2:
        first, last = series[0], series[-1]
        was_txt = ""
        if last < first:
            pct = (first - last) / first * 100
            was_txt = f'<div class="was-row"><span class="was">{first:.0f} RON</span><span class="drop-pct">−{pct:.0f}%</span></div>'
        trend = f'<div class="spark-wrap">{was_txt}{_sparkline(series)}</div>'

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
  <span class="fav-btn {fav_on}" title="Save to favorites" onclick="toggleFav(event, this)">{fav_glyph}</span>
  <span class="hide-btn" title="Hide from tracking" onclick="hideCard(event, this)">✕</span>
  <a class="olx imglink" {olx}>
    <div class="thumb-wrap">{img_html}</div>
  </a>
  <div class="body">
    <p class="title"><a class="olx" {olx}>{title}</a></p>
    <div class="price-row">
      <span class="price">{price_val}</span>
      {orig}
    </div>
    <div class="badges-row">{badges}{ai_badge}</div>
    <div class="meta-row">{meta_html}</div>
    {trend}
    {ai_panel}
    <div class="card-footer">
      <span class="seen-btn seen-toggle {seen_on}" onclick="toggleSeen(event, this)">{seen_txt}</span>
      {ai_action}
    </div>
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
    deal_span = f'<span style="color:var(--accent-deal);font-weight:700">{deals}⚡</span>' if deals else '0⚡'
    return f'<span class="stat" style="font-size:11px;color:var(--text-tertiary)">{active} · {deal_span}</span>'


def _resolve_scope(config_path: str, db_path: str,
                   selected: str | None, group: str | None):
    """Resolve which searches to show from ?search / ?group."""
    all_keys = _search_keys(config_path, db_path)
    groups, _kg = _search_groups(config_path, db_path)
    if selected in all_keys:
        return all_keys, groups, [selected], "search", selected, None
    if group in groups:
        return all_keys, groups, groups[group], "group", None, group
    return all_keys, groups, all_keys, "all", None, None


def _pills_nav_bar(base: str, groups: dict, sel_search: str | None,
                   sel_group: str | None, counts: dict, totals: tuple) -> str:
    """Horizontal 1-tap pill navigation for quick group and search switching."""
    pills = []
    all_active = not sel_search and not sel_group
    pills.append(f'<a class="pill-link {"active" if all_active else ""}" href="{base}">'
                 f'All <span class="badge-num">{totals[0]}</span></a>')
    for gname, keys in groups.items():
        ga = sum(counts.get(k, (0, 0))[0] for k in keys)
        gd = sum(counts.get(k, (0, 0))[1] for k in keys)
        g_active = sel_group == gname
        d_badge = f' ⚡{gd}' if gd else ''
        pills.append(
            f'<a class="pill-link {"active" if g_active else ""}" '
            f'href="{base}?group={urllib.parse.quote(gname)}">'
            f'{html.escape(gname)} <span class="badge-num">{ga}{d_badge}</span></a>')
    return f'<div class="pills-nav">{"".join(pills)}</div>'


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
            f'{_stat_html(ga, gd)}<span class="grp-caret">▶</span></summary>'
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
            f'<span>☰</span> <span>{label}</span>'
            f'<span style="font-size:12px;margin-left:4px">{stat}</span>'
            f'<span class="caret">▼</span></summary>'
            f'<div class="items">{"".join(rows)}</div></details>')


def _controls_bar(selected: str | None, group: str | None, f: dict) -> str:
    """Instant quick search + filter drawer."""
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
    badge_html = '<span class="badge-filter">active</span>' if active else ""

    return f"""<div class="toolbar">
  <div class="quick-search-box">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input class="quick-search-input" id="client-search-input" placeholder="Type to filter listings on screen in real time..." oninput="filterCards(this.value)">
  </div>
  <details class="filter-panel" {"open" if active else ""}>
    <summary>
      <span>⚙️ Filters &amp; Sorting</span> {badge_html}
      <span id="visible-count" style="margin-left:auto;font-size:11px;color:var(--text-tertiary)"></span>
    </summary>
    <form class="filter-form" method="get" action="/">{hidden}
      <div class="filter-group">
        <span>Sort</span>
        <select name="sort">
          {opt('sort','deal','⚡ Deal score')}
          {opt('sort','price_asc','Price: Low to High')}
          {opt('sort','price_desc','Price: High to Low')}
          {opt('sort','newest','Newest listed')}
        </select>
      </div>
      <div class="filter-group">
        <span>Seller</span>
        <select name="seller">
          {opt('seller','all','All sellers')}
          {opt('seller','private','Private only')}
          {opt('seller','dealer','Dealer only')}
        </select>
      </div>
      <div class="filter-group">
        <span>Price (RON)</span>
        <input class="px" name="pmin" inputmode="numeric" placeholder="Min" value="{f.get('pmin') or ''}">
        <span>–</span>
        <input class="px" name="pmax" inputmode="numeric" placeholder="Max" value="{f.get('pmax') or ''}">
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="hide_seen" value="1" {checked}> Hide seen
      </label>
      <button class="btn btn-primary" type="submit" style="padding:5px 12px;font-size:12px">Apply</button>
      <a class="btn" href="/" style="padding:5px 10px;font-size:12px;color:var(--text-tertiary)">Clear</a>
    </form>
  </details>
</div>"""


def _matching_active_ids(db_path: str, config_path: str, selected: str | None,
                         group: str | None, seller: str, pmin: int | None,
                         pmax: int | None) -> list[int]:
    all_keys = _search_keys(config_path, db_path)
    groups, _kg = _search_groups(config_path, db_path)
    if selected in all_keys:
        show = [selected]
    elif group in groups:
        show = groups[group]
    else:
        show = all_keys
    store = Store(db_path)
    try:
        ids = []
        for key in show:
            for r in store.active_for_search(key):
                if r.get("seen"):
                    continue
                p = to_ron(r.get("price"), r.get("currency"))
                if p is None or p <= 0:
                    continue
                if seller == "private" and r.get("is_business"):
                    continue
                if seller == "dealer" and not r.get("is_business"):
                    continue
                if pmin is not None and p < pmin:
                    continue
                if pmax is not None and p > pmax:
                    continue
                ids.append(r["id"])
        return ids
    finally:
        store.close()


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
        pool: list[tuple[str, Any, list | None]] = []

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
            if scope == "search":
                med = f"{sd.median:,.0f} RON".replace(",", ".") if sd.median else "—"
                susp = len(sd.suspicious)
                susp_txt = f' · <span style="color:var(--accent-susp)">{susp} too-cheap</span>' if susp else ""
                if active:
                    sel_header = (
                        f'<div class="chip" style="margin:6px 0;font-size:12px;padding:5px 10px">'
                        f'<b>{html.escape(key)}</b> · {len(active)} active · median <b>{med}</b> · '
                        f'<b>{ndeals} deal(s)</b>{susp_txt}</div>')
                else:
                    sel_header = ('<div class="empty">No active listings yet — try Sync now.</div>')

        def keep(sl) -> bool:
            r = sl.raw
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

        if sort == "price_asc":
            pool.sort(key=lambda t: (t[1].price_ron is None, t[1].price_ron or 0))
        elif sort == "price_desc":
            pool.sort(key=lambda t: t[1].price_ron or 0, reverse=True)
        elif sort == "newest":
            pool.sort(key=lambda t: t[1].raw.get("created_time") or "", reverse=True)
        else:
            pool.sort(key=lambda t: (t[1].is_deal, t[1].deal_score), reverse=True)
        pool.sort(key=lambda t: bool(t[1].raw.get("seen")))
        has_unseen = bool(pool) and not pool[0][1].raw.get("seen")
        pool = pool[:DISPLAY_CAP]

        analyses = store.get_analyses([l.raw["id"] for _, l, _ in pool])
        cards = "".join(
            _card(l, h, search_label=key if show_label else None,
                  analysis=analyses.get(l.raw["id"]))
            for key, l, h in pool)
        if cards:
            body = sel_header + f'<div class="cards">{cards}</div>'
        else:
            body = ('<div class="empty">No listings found matching the criteria.<br>'
                    'Add a search on Manage or tap Sync now.</div>')

        mark_all = ""
        if has_unseen:
            mark_all = f"""<form style="display:flex;justify-content:flex-end;margin:4px 0" method="post" action="/mark_all_seen">
  <input type="hidden" name="search" value="{html.escape(selected or '')}">
  <input type="hidden" name="group" value="{html.escape(group or '')}">
  <input type="hidden" name="seller" value="{html.escape(seller)}">
  <input type="hidden" name="pmin" value="{pmin if pmin is not None else ''}">
  <input type="hidden" name="pmax" value="{pmax if pmax is not None else ''}">
  <input type="hidden" name="next" value="{html.escape(_tab_href('/'))}">
  <button class="btn" type="submit" style="font-size:12px;padding:4px 10px">Mark visible as read</button>
</form>"""

        pills = _pills_nav_bar("/", groups, selected, group, counts, (total_active, total_deals))
        menu = _grouped_menu(groups, "/", selected, group, counts, (total_active, total_deals))
        controls = _controls_bar(selected, group, f)

        content = _sync_banner(store) + pills + menu + controls + mark_all + body

        market_chip = ""
        if scope == "search" and selected:
            mkt = analytics.compute_market_analytics(store, selected)
            market_chip = analytics.render_mini_market_chip(mkt)

        if scope == "search":
            scope_txt = f"{selected}"
        elif scope == "group":
            scope_txt = f"Group: {group}"
        else:
            scope_txt = f"All {len(all_keys)} searches"

        sub_chips = _sub_chips_html(scope_txt, shown_deals, store, market_chip=market_chip)
    finally:
        store.close()
    return _shell(sub_chips, content, "deals", flash)


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
            body = ('<div class="cards">'
                    + "".join(_card(l, h, search_label=key,
                                    analysis=analyses.get(l.raw["id"]))
                              for _, key, l, h in cards_data) + '</div>')
        else:
            body = ('<div class="empty">No saved listings yet.<br>'
                    'Tap the ☆ star on any card to bookmark it here.</div>')
        content = _sync_banner(store) + body
        sub_chips = (f'<span class="chip">★ {len(cards_data)} saved bookmark(s)</span>'
                     f'<span class="chip">🕒 {_last_sync_text(store)}</span>')
    finally:
        store.close()
    return _shell(sub_chips, content, "saved", flash)


def render_drops(db_path: str, config_path: str, selected: str | None = None,
                 group: str | None = None, flash: str = "") -> str:
    """Listings whose price has fallen since we first saw them."""
    all_keys, groups, show, scope, selected, group = _resolve_scope(
        config_path, db_path, selected, group)
    store = Store(db_path)
    try:
        counts: dict[str, tuple[int, int]] = {}
        cards: list[tuple[float, str, Any, list | None]] = []
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
                    cards.append((pct, key, l, hist.get(l.raw["id"])))
        cards.sort(key=lambda c: c[0], reverse=True)
        if cards:
            analyses = store.get_analyses([l.raw["id"] for _, _, l, _ in cards])
            body = ('<div class="cards">'
                    + "".join(_card(l, h, search_label=key if scope != "search" else None,
                                    analysis=analyses.get(l.raw["id"]))
                              for _, key, l, h in cards)
                    + '</div>')
        else:
            body = ('<div class="empty">No price drops recorded yet.<br>'
                    'Price drops appear automatically when a listing price is lowered between syncs.</div>')
        totals = (sum(a for a, _ in counts.values()),
                  sum(d for _, d in counts.values()))
        pills = _pills_nav_bar("/drops", groups, selected, group, counts, totals)
        menu = _grouped_menu(groups, "/drops", selected, group, counts, totals)
        content = _sync_banner(store) + pills + menu + body
        scope_txt = (f"{selected}" if scope == "search" else
                     f"Group: {group}" if scope == "group" else "All searches")
        sub_chips = (f'<span class="chip">📉 {len(cards)} price drop(s)</span>'
                     f'<span class="chip">{scope_txt}</span>'
                     f'<span class="chip">🕒 {_last_sync_text(store)}</span>')
    finally:
        store.close()
    return _shell(sub_chips, content, "drops", flash)


# ---------- trends (candlestick) page ----------

def _candlestick(candles: list[dict], w: int = 340, h: int = 210) -> str:
    """Polished SVG financial candlestick with grid and values."""
    if not candles:
        return ('<div class="empty">No trend data yet.<br>'
                'Daily candles accumulate as sync runs — check back tomorrow.</div>')
    padL, padR, padT, padB = 52, 12, 14, 26
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
    cw = min(slot * 0.65, 24)
    p: list[str] = []

    for val in (lo, (lo + hi) / 2, hi):
        yy = y(val)
        p.append(f'<line x1="{padL}" y1="{yy:.1f}" x2="{w-padR}" y2="{yy:.1f}" '
                 f'stroke="var(--border-subtle)" stroke-dasharray="2 2"/>')
        p.append(f'<text x="{padL-8}" y="{yy+3.5:.1f}" text-anchor="end" '
                 f'fill="var(--text-tertiary)" font-size="9.5" font-family="JetBrains Mono">{val:,.0f}</text>')

    prev = None
    for i, c in enumerate(candles):
        cx = padL + slot * i + slot / 2
        med = c.get("median")
        color = "var(--text-secondary)"
        if prev is not None and med is not None:
            color = "#10b981" if med < prev else "#f43f5e" if med > prev else "var(--text-secondary)"
        if med is not None:
            prev = med
        if c["low"] is not None and c["high"] is not None:
            p.append(f'<line x1="{cx:.1f}" y1="{y(c["high"]):.1f}" '
                     f'x2="{cx:.1f}" y2="{y(c["low"]):.1f}" stroke="{color}" '
                     f'stroke-width="1.6" stroke-linecap="round"/>')
        q1, q3 = c.get("q1"), c.get("q3")
        if q1 is not None and q3 is not None:
            top, bot = y(max(q1, q3)), y(min(q1, q3))
            p.append(f'<rect x="{cx-cw/2:.1f}" y="{top:.1f}" width="{cw:.1f}" '
                     f'height="{max(bot-top, 3):.1f}" rx="2" fill="{color}" '
                     f'fill-opacity="0.28" stroke="{color}" stroke-width="1.2"/>')
        if med is not None:
            p.append(f'<line x1="{cx-cw/2:.1f}" y1="{y(med):.1f}" '
                     f'x2="{cx+cw/2:.1f}" y2="{y(med):.1f}" stroke="{color}" '
                     f'stroke-width="2.2" stroke-linecap="round"/>')

    p.append(f'<text x="{padL}" y="{h-6}" fill="var(--text-tertiary)" font-size="10" font-family="JetBrains Mono">'
             f'{candles[0]["day"][5:]}</text>')
    if n > 1:
        p.append(f'<text x="{w-padR}" y="{h-6}" text-anchor="end" '
                 f'fill="var(--text-tertiary)" font-size="10" font-family="JetBrains Mono">{candles[-1]["day"][5:]}</text>')
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
            '<div class="chip" style="margin:8px 0 12px;font-size:12px;padding:6px 12px">'
            '📊 <b>Daily Price Candles</b> · Wick = Min–Max · Box = Q1–Q3 · Line = Median · Green = Price drop vs previous day</div>'
        ]
        for key in show:
            mkt = analytics.compute_market_analytics(store, key)
            blocks.append(analytics.render_market_card(mkt))
            candles = store.daily_candles(key)
            blocks.append('<div class="chart-card">')
            blocks.append(f'<div class="chart-title">{html.escape(key)} — Daily Price History</div>')
            if candles:
                last = candles[-1]
                blocks.append(
                    f'<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">'
                    f'Latest: Min <b>{last["low"]:,.0f}</b> · Median <b>{last["median"]:,.0f}</b> · '
                    f'Max <b>{last["high"]:,.0f} RON</b> ({last["n"]} listings)</div>')
            blocks.append(_candlestick(candles))
            blocks.append('</div>')
        body = "".join(blocks)
        totals = (sum(a for a, _ in counts.values()),
                  sum(d for _, d in counts.values()))
        pills = _pills_nav_bar("/history", groups, selected, group, counts, totals)
        menu = _grouped_menu(groups, "/history", selected, group, counts, totals)
        content = _sync_banner(store) + pills + menu + body
        sub_chips = (f'<span class="chip">📊 Price Trends &amp; Velocity</span>'
                     f'<span class="chip">🕒 {_last_sync_text(store)}</span>')
    finally:
        store.close()
    return _shell(sub_chips, content, "trends", flash)


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

    discover_panel = """<div class="mng-box">
  <h2>🔍 Model Key &amp; Category Finder</h2>
  <p style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">
    Search OLX in real-time to find exact category IDs and model keys.
  </p>
  <div class="row2">
    <input class="form-input" id="dq" placeholder="e.g. iphone 15 pro, galaxy z fold 6, golf 7"
           onkeydown="if(event.key==='Enter'){event.preventDefault();doDiscover();}">
    <button type="button" class="btn btn-primary" style="flex:none" onclick="doDiscover()">Search</button>
  </div>
  <div id="dres" style="margin-top:10px;font-size:12px;color:var(--text-tertiary)">
    Type a keyword, then tap a result chip below to fill the form automatically.
  </div>
</div>
<script>
async function doDiscover(){
  const q=document.getElementById('dq').value.trim();
  const box=document.getElementById('dres');
  if(!q){box.textContent='Type a search term first.';return;}
  box.innerHTML='<span style="color:#60a5fa">Searching OLX taxonomy…</span>';
  try{
    const r=await fetch('/api/discover?q='+encodeURIComponent(q));
    const d=await r.json();
    let h='';
    if(d.categories.length){
      h+='<div style="margin:6px 0"><b style="color:var(--text-primary)">Category ID (tap to set):</b><br>';
      d.categories.forEach(c=>{
        h+='<span class="chip-pill b-dealer" onclick="setCat('+c.id+')">📁 '+c.id+' '+c.type+' <small>×'+c.n+'</small></span>';
      });
      h+='</div>';
    }
    if(d.models.length){
      h+='<div style="margin:6px 0"><b style="color:var(--text-primary)">Model Key (tap to set):</b><br>';
      d.models.forEach(m=>{
        const lbl=(m.label||m.key).replace(/</g,'');
        h+='<span class="chip-pill b-deal" onclick="setModel(\\''+m.key+'\\')">⚡ '+lbl+' <small>('+m.key+') ×'+m.n+'</small></span>';
      });
      h+='</div>';
    }
    box.innerHTML=h||'No structured models found — you can use the free-text query field instead.';
  }catch(e){box.textContent='Error: '+e;}
}
function setCat(id){
  document.querySelector('[name=category_id]').value=id;
  showToast('Category ID set to ' + id, 'success');
}
function setModel(k){
  document.querySelector('[name=model]').value=k;
  const key=document.querySelector('[name=key]');
  if(!key.value && !key.hasAttribute('readonly')) key.value=k+'_used';
  showToast('Model set to ' + k, 'success');
}
</script>
"""

    form = discover_panel + f"""<form class="mng-box" method="post" action="/searches/add">
  <h2>{'✏️ Edit Search' if editing else '➕ Add Tracked Search'}</h2>
  <label class="form-label">Key (Unique ID)</label>
  <input class="form-input" name="key" value="{val('key')}" placeholder="e.g. iphone_15_pro_used" required
         {'readonly' if editing else ''}>
  <label class="form-label">Group (Optional)</label>
  <input class="form-input" name="group" value="{ed_group}" list="groups" placeholder="e.g. Phones, Fold, Cars…">
  <datalist id="groups">{datalist}</datalist>
  <label class="form-label">OLX Model Key (Optional)</label>
  <input class="form-input" name="model" value="{ed_model}" placeholder="e.g. iphone_15_pro">
  <div class="row2">
    <div>
      <label class="form-label">Condition</label>
      <select class="form-select" name="state">
        <option value="" {sel('')}>Any condition</option>
        <option value="used" {sel('used')}>Used (Second Hand)</option>
        <option value="new" {sel('new')}>New</option>
      </select>
    </div>
    <div>
      <label class="form-label">Category ID (0 = All)</label>
      <input class="form-input" name="category_id" value="{val('category_id','0')}">
    </div>
  </div>
  <label class="form-label">Free-text query (Optional)</label>
  <input class="form-input" name="query" value="{val('query')}" placeholder="e.g. 256gb natural titanium">
  <div class="row2">
    <div>
      <label class="form-label">Price Min (RON)</label>
      <input class="form-input" name="price_from" value="{val('price_from')}" inputmode="numeric">
    </div>
    <div>
      <label class="form-label">Price Max (RON)</label>
      <input class="form-input" name="price_to" value="{val('price_to')}" inputmode="numeric">
    </div>
  </div>
  <label class="form-label">Region ID (Optional)</label>
  <input class="form-input" name="region_id" value="{val('region_id')}" inputmode="numeric">
  <details style="margin-top:14px" {'open' if (ed_yfrom or ed_yto or ed_mileage or ed_fuel or ed_gear) else ''}>
    <summary style="cursor:pointer;color:var(--text-secondary);font-size:13px;font-weight:600">🚗 Vehicle Filters (Optional)</summary>
    <div class="row2">
      <div>
        <label class="form-label">Year From</label>
        <input class="form-input" name="year_from" value="{ed_yfrom}" inputmode="numeric" placeholder="2018">
      </div>
      <div>
        <label class="form-label">Year To</label>
        <input class="form-input" name="year_to" value="{ed_yto}" inputmode="numeric" placeholder="2022">
      </div>
    </div>
    <label class="form-label">Max Mileage (km)</label>
    <input class="form-input" name="mileage_to" value="{ed_mileage}" inputmode="numeric" placeholder="180000">
    <div class="row2">
      <div>
        <label class="form-label">Fuel</label>
        <select class="form-select" name="fuel">
          <option value="">Any</option>
          <option value="diesel" {selo(ed_fuel,'diesel')}>Diesel</option>
          <option value="petrol" {selo(ed_fuel,'petrol')}>Benzină</option>
          <option value="hybrid" {selo(ed_fuel,'hybrid')}>Hibrid</option>
          <option value="lpg" {selo(ed_fuel,'lpg')}>GPL</option>
          <option value="electric" {selo(ed_fuel,'electric')}>Electric</option>
        </select>
      </div>
      <div>
        <label class="form-label">Gearbox</label>
        <select class="form-select" name="gearbox">
          <option value="">Any</option>
          <option value="automatic" {selo(ed_gear,'automatic')}>Automată</option>
          <option value="manual" {selo(ed_gear,'manual')}>Manuală</option>
        </select>
      </div>
    </div>
  </details>
  <details style="margin-top:12px" {'open' if (ed_wheel or ed_brand) else ''}>
    <summary style="cursor:pointer;color:var(--text-secondary);font-size:13px;font-weight:600">🚲 Bike Filters (Optional)</summary>
    <div class="row2">
      <div>
        <label class="form-label">Wheel Size</label>
        <select class="form-select" name="wheel">
          <option value="">Any</option>
          <option value="29_inch" {selo(ed_wheel,'29_inch')}>29"</option>
          <option value="27_5_inch" {selo(ed_wheel,'27_5_inch')}>27.5"</option>
          <option value="26_inch" {selo(ed_wheel,'26_inch')}>26"</option>
          <option value="28_inch" {selo(ed_wheel,'28_inch')}>28"</option>
        </select>
      </div>
      <div>
        <label class="form-label">Brand</label>
        <select class="form-select" name="brand">
          <option value="">Any</option>
          <option value="cube" {selo(ed_brand,'cube')}>Cube</option>
          <option value="specialized" {selo(ed_brand,'specialized')}>Specialized</option>
          <option value="scott" {selo(ed_brand,'scott')}>Scott</option>
          <option value="trek" {selo(ed_brand,'trek')}>Trek</option>
          <option value="rockrider" {selo(ed_brand,'rockrider')}>Rockrider</option>
        </select>
      </div>
    </div>
  </details>
  <div style="margin-top:16px;display:flex;gap:10px">
    <button class="btn btn-primary" type="submit">{'Save Changes' if editing else 'Save & Track'}</button>
    {'<a class="btn" href="/searches">Cancel</a>' if editing else ''}
  </div>
</form>"""

    def srow(s: dict) -> str:
        key = html.escape(s.get("key", ""))
        paused = bool(s.get("paused"))
        badge = ' <span class="badge b-susp">paused</span>' if paused else '<span class="badge b-deal">active</span>'
        return f"""<div class="srow{' paused' if paused else ''}">
  <div class="info">
    <div class="k">{key} {badge}</div>
    <div class="d">{html.escape(_search_summary(s))}</div>
  </div>
  <form method="post" action="/searches/pause" style="margin:0">
    <input type="hidden" name="key" value="{key}">
    <button class="btn" type="submit" style="font-size:12px;padding:4px 8px">{'Resume' if paused else 'Pause'}</button>
  </form>
  <a class="btn" href="/searches?edit={urllib.parse.quote(s.get('key',''))}" style="font-size:12px;padding:4px 8px">Edit</a>
  <form method="post" action="/searches/delete" style="margin:0"
        onsubmit="return confirm('Delete {key}?')">
    <input type="hidden" name="key" value="{key}">
    <button class="btn btn-del" type="submit" style="font-size:12px;padding:4px 8px">Delete</button>
  </form>
</div>"""

    grouped: dict[str, list[dict]] = {}
    for s in searches:
        grouped.setdefault((s.get("group") or "").strip() or "Other", []).append(s)
    listing = ""
    for gname, gsearches in grouped.items():
        listing += (f'<div style="margin:14px 0 6px;font-weight:700;font-size:14px;color:var(--text-primary)">'
                    f'{html.escape(gname)} ({len(gsearches)})</div>')
        listing += "".join(srow(s) for s in gsearches)
    listing = listing or '<div class="empty">No searches configured yet.</div>'
    content = form + '<div class="mng-box"><h2>📋 Configured Searches</h2>' + listing + '</div>'

    # Hidden listings
    store = Store(db_path)
    try:
        banner = _sync_banner(store)
        hidden = store.excluded_listings()
        last_sync = _last_sync_text(store)
    finally:
        store.close()
    content = banner + content
    if hidden:
        hrows = []
        for h in hidden:
            title = html.escape((h.get("title") or "—")[:60])
            price = f"{h.get('price'):.0f} {h.get('currency') or ''}".strip() if h.get("price") is not None else "—"
            hrows.append(f"""<div class="srow">
  <div class="info">
    <div class="k">{price}</div>
    <div class="d">{html.escape(h.get('search_key',''))} · {title}</div>
  </div>
  <form method="post" action="/exclude" style="margin:0">
    <input type="hidden" name="id" value="{h.get('id')}">
    <input type="hidden" name="undo" value="1">
    <button class="btn btn-go" type="submit" style="font-size:12px;padding:4px 10px">Restore</button>
  </form>
</div>""")
        content += ('<div class="mng-box"><h2>🙈 Hidden Listings (' + str(len(hidden)) + ')</h2>'
                    + "".join(hrows) + '</div>')

    sub_chips = (f'<span class="chip">⚙️ {len(searches)} search(es) active</span>'
                 f'<span class="chip">🕒 {last_sync}</span>')
    return _shell(sub_chips, content, "searches", flash)


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
        filters["petrol"] = [fuel]
    gearbox = form.get("gearbox", "").strip()
    if gearbox:
        filters["gearbox"] = [gearbox]
    wheel = form.get("wheel", "").strip()
    if wheel:
        filters["dimensiune_roata"] = [wheel]
    brand = form.get("brand", "").strip()
    if brand:
        filters["brand"] = [brand]
    if filters:
        s["filters"] = filters
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
    push: "Push" = None

    def _check_admin(self) -> bool:
        """Admin routes live on the tailnet listener, which injects X-Admin: 1."""
        return self.headers.get("X-Admin") == "1"

    def _get_client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "127.0.0.1"

    def _get_cookie(self, name: str) -> str | None:
        jar = http.cookies.SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _get_current_device(self) -> dict | None:
        token = self._get_cookie(accounts.COOKIE_NAME)
        return accounts.device_by_token(token) if token else None

    def _check_auth(self) -> dict | None:
        dev = self._get_current_device()
        if dev:
            return dev
        if self.path.startswith("/api/"):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "detail": "Acest dispozitiv nu este înregistrat. Ai nevoie de o invitație."
            }).encode("utf-8"))
            return None
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [""])[0] or qs.get("invite", [""])[0]
        self._html(render_gate(prefill=code, next_path=self.path if self.command == "GET" else "/"))
        return None

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
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
        name = Path(path).name
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
                "body": "Alerts are active. You will be notified of new deals.",
                "url": "/",
                "tag": "olx-test",
            })
            for ep in dead:
                store.remove_subscription(ep)
        finally:
            store.close()

    def _apply_fx(self) -> None:
        store = Store(self.db_path)
        try:
            scorer.EUR_TO_RON = fx.current(store)
        finally:
            store.close()

    _PUBLIC_PATHS = {"/manifest.webmanifest", "/sw.js", "/activate", "/api/invites/redeem"}
    _PUBLIC_PREFIXES = ("/static/",)

    def _is_public(self, path: str) -> bool:
        return path in self._PUBLIC_PATHS or path.startswith(self._PUBLIC_PREFIXES)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Admin API endpoints (tailnet listener with X-Admin: 1)
        if parsed.path.startswith("/api/admin/"):
            if not self._check_admin():
                self.send_error(404)
                return
            if parsed.path == "/api/admin/devices":
                self._json({"devices": accounts.list_devices()})
                return
            elif parsed.path == "/api/admin/invites":
                base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                invites = accounts.list_invites()
                for i in invites:
                    code = i.pop("code_plain", None)
                    i["code"] = code
                    i["url"] = f"{base}/?code={code}" if code and base else None
                self._json({"invites": invites, "ttl_days": accounts.INVITE_TTL.days})
                return
            else:
                self.send_error(404)
                return

        # Public endpoints
        if parsed.path == "/manifest.webmanifest":
            self._raw(json.dumps(MANIFEST).encode("utf-8"),
                      "application/manifest+json")
            return
        elif parsed.path == "/sw.js":
            self._raw(SW_JS.encode("utf-8"), "application/javascript",
                      {"Service-Worker-Allowed": "/"})
            return
        elif parsed.path.startswith("/static/"):
            self._static(parsed.path)
            return
        elif parsed.path == "/activate":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [""])[0] or qs.get("invite", [""])[0]
            self._html(render_gate(prefill=code, next_path=qs.get("next", ["/"])[0]))
            return

        # Authenticated endpoints
        if not self._check_auth():
            return

        self._apply_fx()
        qs = urllib.parse.parse_qs(parsed.query)
        flash = qs.get("msg", [""])[0]
        selected = qs.get("search", [None])[0]
        group = qs.get("group", [None])[0]
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
        elif parsed.path == "/api/sync/status":
            self._json(SyncTracker.get_state())
        elif parsed.path == "/api/sync/events":
            self._sse_sync_events()
        elif parsed.path == "/push/public-key":
            self._json({"key": self.push.public_key_b64()})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # Admin API endpoints
        if parsed.path.startswith("/api/admin/"):
            if not self._check_admin():
                self.send_error(404)
                return
            if parsed.path == "/api/admin/invites":
                payload = self._json_body()
                label = (payload.get("label") or "").strip()[:60] or None
                code = accounts.create_invite(label)
                base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                self._json({
                    "code": code,
                    "url": f"{base}/?code={code}" if base else None,
                    "expires_in_days": accounts.INVITE_TTL.days,
                    "label": label,
                })
                return
            elif re.match(r"^/api/admin/invites/(\d+)/revoke$", parsed.path):
                m = re.match(r"^/api/admin/invites/(\d+)/revoke$", parsed.path)
                invite_id = int(m.group(1))
                if not accounts.revoke_invite(invite_id):
                    self.send_error(404, "no such unused invite")
                    return
                self._json({"revoked": invite_id})
                return
            elif parsed.path == "/api/admin/invites/prune":
                self._json({"deleted": accounts.prune_invites()})
                return
            elif re.match(r"^/api/admin/devices/(\d+)/revoke$", parsed.path):
                m = re.match(r"^/api/admin/devices/(\d+)/revoke$", parsed.path)
                device_id = int(m.group(1))
                payload = self._json_body()
                revoked = bool(payload.get("revoked", True))
                if not accounts.set_revoked(device_id, revoked):
                    self.send_error(404, "no such device")
                    return
                self._json({"id": device_id})
                return
            elif re.match(r"^/api/admin/devices/(\d+)/label$", parsed.path):
                m = re.match(r"^/api/admin/devices/(\d+)/label$", parsed.path)
                device_id = int(m.group(1))
                label = (self._json_body().get("label") or "").strip()[:60]
                if not accounts.rename_device(device_id, label):
                    self.send_error(404, "no such device")
                    return
                self._json({"id": device_id, "label": label})
                return
            elif parsed.path == "/api/admin/devices/prune":
                self._json({"deleted": accounts.prune_devices()})
                return
            else:
                self.send_error(404)
                return

        # Public invite redemption endpoints
        if parsed.path in ("/activate", "/api/invites/redeem"):
            is_json = parsed.path.startswith("/api/")
            body = self._json_body() if is_json else self._form()
            raw_code = body.get("code", "")
            code = accounts.normalise_code(raw_code)
            next_path = body.get("next") or "/"
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
            client_ip = self._get_client_ip()

            if accounts.throttled(client_ip):
                msg = "Prea multe încercări. Reîncearcă într-un minut."
                if is_json:
                    self._json({"detail": msg}, status=429)
                else:
                    self._html(render_gate(prefill=code, error=msg, next_path=next_path), status=429)
                return

            try:
                device_id, token = accounts.redeem(code)
                cookie_val = (f"{accounts.COOKIE_NAME}={token}; Path=/; "
                              f"Max-Age={accounts.COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax")
                if is_json:
                    data = json.dumps({"ok": True, "device_id": device_id}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Set-Cookie", cookie_val)
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(303)
                    self.send_header("Location", next_path)
                    self.send_header("Set-Cookie", cookie_val)
                    self.end_headers()
            except accounts.InviteError as ex:
                if is_json:
                    self._json({"detail": str(ex)}, status=400)
                else:
                    self._html(render_gate(prefill=raw_code, error=str(ex), next_path=next_path), status=400)
            except Exception as ex:
                if is_json:
                    self._json({"detail": f"Eroare: {ex}"}, status=500)
                else:
                    self._html(render_gate(prefill=raw_code, error=f"Eroare: {ex}", next_path=next_path), status=500)
            return

        # Authenticated POST actions
        dev = self._check_auth()
        if not dev:
            return

        self._apply_fx()
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
            elif parsed.path == "/searches/pause":
                key = self._form().get("key", "")
                paused = config.toggle_paused(self.config_path, key)
                self._redirect("/searches?msg=" + urllib.parse.quote(
                    f"'{key}' {'paused' if paused else 'resumed'}."))
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
                    self.send_response(204)
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
                self.send_response(204)
                self.end_headers()
            elif parsed.path == "/mark_all_seen":
                form = self._form()
                pmin = _int_or_none(form.get("pmin"))
                pmax = _int_or_none(form.get("pmax"))
                ids = _matching_active_ids(
                    self.db_path, self.config_path,
                    form.get("search") or None, form.get("group") or None,
                    form.get("seller", "all"), pmin, pmax)
                store = Store(self.db_path)
                try:
                    store.mark_seen_bulk(ids)
                finally:
                    store.close()
                next_path = form.get("next") or "/"
                if not next_path.startswith("/") or next_path.startswith("//"):
                    next_path = "/"
                self._redirect(next_path)
            elif parsed.path == "/push/subscribe":
                sub = self._json_body()
                if sub.get("endpoint"):
                    store = Store(self.db_path)
                    try:
                        store.add_subscription(sub, device_id=dev.get("id"))
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
                accept = self.headers.get("Accept", "")
                if "application/json" in accept:
                    self._json({"ok": True, "status": "started", "sync": SyncTracker.get_state()})
                else:
                    base = _tab_href("/")
                    sep = "&" if "?" in base else "?"
                    self._redirect(base + sep + "msg=" + urllib.parse.quote(
                        "Sync started... tracking progress live."))
            else:
                self.send_error(404)
        except Exception as exc:
            self._redirect("/searches?msg=" + urllib.parse.quote(f"Error: {exc}"))

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/admin/"):
            if not self._check_admin():
                self.send_error(404)
                return
            m = re.match(r"^/api/admin/devices/(\d+)$", parsed.path)
            if m:
                device_id = int(m.group(1))
                if not accounts.delete_device(device_id):
                    self.send_error(404, "no such device")
                    return
                self._json({"deleted": device_id})
                return
        self.send_error(404)

    def _sse_sync_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = SyncTracker.register_listener()
        try:
            init_data = f"data: {json.dumps(SyncTracker.get_state())}\n\n".encode("utf-8")
            self.wfile.write(init_data)
            self.wfile.flush()

            while True:
                try:
                    state = q.get(timeout=12.0)
                    msg = f"data: {json.dumps(state)}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass
        finally:
            SyncTracker.unregister_listener(q)

    def _deactivate(self, key: str) -> None:
        store = Store(self.db_path)
        try:
            store.conn.execute(
                "UPDATE listings SET active=0 WHERE search_key=?", (key,))
            store.conn.commit()
        finally:
            store.close()

    def _run_analysis(self, listing_id: int | None) -> None:
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

    _sync_lock = threading.Lock()
    _sync_thread: threading.Thread | None = None

    @classmethod
    def sync_running(cls) -> bool:
        t = cls._sync_thread
        return bool(t and t.is_alive())

    def _trigger_sync(self) -> None:
        with Handler._sync_lock:
            if Handler.sync_running():
                return
            Handler._sync_thread = threading.Thread(
                target=self._sync_once, name="olx-sync", daemon=True)
            Handler._sync_thread.start()

    @classmethod
    def _sync_once(cls) -> None:
        import run as sync

        args = SimpleNamespace(config=cls.config_path, db=cls.db_path,
                               delay=1.0, jitter=0.5, quiet=True)
        summary: list[dict] = []
        SyncTracker.update(running=True, step=0, total=0, current_key="",
                           new_count=0, deal_count=0, message="Initializing sync...",
                           started_at=datetime.now(timezone.utc).isoformat(),
                           finished_at=None)
        try:
            fetcher = sync.OlxFetcher(delay=args.delay, jitter=args.jitter)
            push = Push(Path(args.db).resolve().with_name("vapid_key.pem"))
            sync._run_all(args, fetcher, push, summary,
                          progress_cb=lambda info: SyncTracker.update(**info))
            sync.notify_ntfy(summary, args.db)
            new_tot = sum(s.get('new', 0) for s in summary if s.get('ok'))
            SyncTracker.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                               message=f"Sync complete ({new_tot} new item(s))")
        except Exception as exc:
            print(f"sync failed: {exc}", flush=True)
            SyncTracker.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                               message=f"Sync error: {exc}")

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
    Handler.push = Push(Path(args.db).resolve().with_name("vapid_key.pem"))
    accounts.configure(args.db)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OLX dashboard on http://{args.host}:{args.port}/ (device auth enabled)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
