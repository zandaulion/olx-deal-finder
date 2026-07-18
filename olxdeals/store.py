"""SQLite persistence + change detection for OLX listings.

Two tables:
  * ``listings``      — current state of every listing we've ever seen
  * ``price_history`` — one row per observed price for a listing

``sync()`` upserts a freshly-fetched batch and returns what changed
(new listings, price changes, and listings that disappeared).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                INTEGER PRIMARY KEY,
    search_key        TEXT NOT NULL,
    url               TEXT,
    title             TEXT,
    price             REAL,
    currency          TEXT,
    negotiable        INTEGER,
    previous_price    REAL,
    model             TEXT,
    state             TEXT,
    city              TEXT,
    region            TEXT,
    is_business       INTEGER,
    photo             TEXT,
    photos            TEXT,
    description       TEXT,
    seller_id         INTEGER,
    seller_name       TEXT,
    seller_since      TEXT,
    created_time      TEXT,
    last_refresh_time TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    excluded          INTEGER NOT NULL DEFAULT 0,
    favorite          INTEGER NOT NULL DEFAULT 0,
    seen              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(search_key, active);
CREATE INDEX IF NOT EXISTS idx_listings_model  ON listings(model, state);

CREATE TABLE IF NOT EXISTS price_history (
    id       INTEGER NOT NULL,
    price    REAL,
    currency TEXT,
    ts       TEXT NOT NULL,
    PRIMARY KEY (id, ts)
);

CREATE TABLE IF NOT EXISTS price_stats (
    search_key TEXT NOT NULL,
    ts         TEXT NOT NULL,
    day        TEXT NOT NULL,
    n          INTEGER,
    min        REAL,
    q1         REAL,
    median     REAL,
    q3         REAL,
    max        REAL,
    PRIMARY KEY (search_key, ts)
);
CREATE INDEX IF NOT EXISTS idx_stats_search ON price_stats(search_key, day);

CREATE TABLE IF NOT EXISTS sync_runs (
    search_key    TEXT NOT NULL,
    ts            TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    duration_ms   INTEGER,
    new           INTEGER,
    price_changes INTEGER,
    removed       INTEGER,
    seen          INTEGER,
    error         TEXT,
    PRIMARY KEY (search_key, ts)
);
CREATE INDEX IF NOT EXISTS idx_runs_search ON sync_runs(search_key, ts);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh   TEXT NOT NULL,
    auth     TEXT NOT NULL,
    created  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_analysis (
    listing_id   INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    model        TEXT,
    score        INTEGER,
    scam_risk    TEXT,
    summary      TEXT,
    verdict_json TEXT
);
"""

_FIELDS = [
    "id", "search_key", "url", "title", "price", "currency", "negotiable",
    "previous_price", "model", "state", "city", "region", "is_business",
    "photo", "photos", "description", "seller_id", "seller_name",
    "seller_since", "created_time", "last_refresh_time",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PriceChange:
    listing: dict[str, Any]
    old_price: float | None
    new_price: float | None


@dataclass
class SyncResult:
    search_key: str
    new: list[dict[str, Any]] = field(default_factory=list)
    price_changes: list[PriceChange] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total_seen(self) -> int:
        return len(self.new) + len(self.price_changes) + self.unchanged


class Store:
    def __init__(self, path: str | Path = "olxdeals.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(listings)")}
        for col in ("excluded", "favorite", "seen"):
            if col not in cols:
                self.conn.execute(
                    f"ALTER TABLE listings ADD COLUMN {col} "
                    "INTEGER NOT NULL DEFAULT 0")
        for col, typ in (("photos", "TEXT"), ("description", "TEXT"),
                         ("seller_id", "INTEGER"), ("seller_name", "TEXT"),
                         ("seller_since", "TEXT")):
            if col not in cols:
                self.conn.execute(
                    f"ALTER TABLE listings ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get(self, listing_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    def _record_price(self, listing_id: int, price: float | None,
                      currency: str | None, ts: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO price_history (id, price, currency, ts) "
            "VALUES (?, ?, ?, ?)",
            (listing_id, price, currency, ts),
        )

    def sync(self, listings: list[dict[str, Any]], search_key: str) -> SyncResult:
        """Upsert a fetched batch for one search and report the diff."""
        now = _now()
        result = SyncResult(search_key=search_key)
        fetched_ids: set[int] = set()

        for item in listings:
            fetched_ids.add(item["id"])
            existing = self.get(item["id"])
            values = [item.get(f) for f in _FIELDS]

            if existing is None:
                self.conn.execute(
                    f"INSERT INTO listings ({', '.join(_FIELDS)}, "
                    "first_seen, last_seen, active) VALUES "
                    f"({', '.join('?' for _ in _FIELDS)}, ?, ?, 1)",
                    values + [now, now],
                )
                self._record_price(item["id"], item.get("price"),
                                    item.get("currency"), now)
                result.new.append(item)
            else:
                old_price = existing["price"]
                new_price = item.get("price")
                if old_price != new_price:
                    self._record_price(item["id"], new_price,
                                       item.get("currency"), now)
                    result.price_changes.append(
                        PriceChange(item, old_price, new_price)
                    )
                else:
                    result.unchanged += 1
                set_clause = ", ".join(f"{f} = ?" for f in _FIELDS)
                self.conn.execute(
                    f"UPDATE listings SET {set_clause}, last_seen = ?, active = 1 "
                    "WHERE id = ?",
                    values + [now, item["id"]],
                )

        # Anything previously active for this search that we didn't see is gone.
        stale = self.conn.execute(
            "SELECT * FROM listings WHERE search_key = ? AND active = 1",
            (search_key,),
        ).fetchall()
        for row in stale:
            if row["id"] not in fetched_ids:
                self.conn.execute(
                    "UPDATE listings SET active = 0, last_seen = ? WHERE id = ?",
                    (now, row["id"]),
                )
                result.removed.append(dict(row))

        self.conn.commit()
        return result

    def active_for_search(self, search_key: str) -> list[dict[str, Any]]:
        """Active, non-excluded listings — what the scorer and views consume."""
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE search_key = ? AND active = 1 "
            "AND excluded = 0 ORDER BY price",
            (search_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _set_flag(self, column: str, listing_id: int, value: bool) -> None:
        assert column in ("excluded", "favorite", "seen")
        self.conn.execute(
            f"UPDATE listings SET {column} = ? WHERE id = ?",
            (1 if value else 0, listing_id),
        )
        self.conn.commit()

    def set_excluded(self, listing_id: int, value: bool) -> None:
        self._set_flag("excluded", listing_id, value)

    def set_favorite(self, listing_id: int, value: bool) -> None:
        self._set_flag("favorite", listing_id, value)

    def set_seen(self, listing_id: int, value: bool) -> None:
        self._set_flag("seen", listing_id, value)

    def favorite_listings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE active = 1 AND excluded = 0 "
            "AND favorite = 1 ORDER BY search_key, price",
        ).fetchall()
        return [dict(r) for r in rows]

    def record_stats(self, search_key: str, stats: dict[str, Any]) -> None:
        """Store one distribution snapshot (called once per sync per search)."""
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO price_stats "
            "(search_key, ts, day, n, min, q1, median, q3, max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (search_key, now, now[:10], stats.get("n"), stats.get("min"),
             stats.get("q1"), stats.get("median"), stats.get("q3"),
             stats.get("max")),
        )
        self.conn.commit()

    def daily_candles(self, search_key: str) -> list[dict[str, Any]]:
        """One candle per day, taken entirely from that day's LAST snapshot, so
        every part (wick min/max, box Q1–Q3, median) describes one consistent
        population. (Spanning min/max across the day mixed populations whenever a
        search was edited or a transient junk listing appeared mid-day.)"""
        rows = self.conn.execute(
            "SELECT day, n, min, q1, median, q3, max FROM price_stats "
            "WHERE search_key = ? ORDER BY ts",
            (search_key,),
        ).fetchall()
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:  # ordered by ts -> last write per day wins, position kept
            by_day[r["day"]] = {
                "day": r["day"], "low": r["min"], "high": r["max"],
                "q1": r["q1"], "median": r["median"], "q3": r["q3"], "n": r["n"],
            }
        return list(by_day.values())

    def save_analysis(self, listing_id: int, model: str,
                      verdict: dict[str, Any]) -> None:
        import json as _json
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_analysis "
            "(listing_id, ts, model, score, scam_risk, summary, verdict_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (listing_id, _now(), model, verdict.get("verdict_score"),
             verdict.get("scam_risk"), verdict.get("summary"),
             _json.dumps(verdict, ensure_ascii=False)),
        )
        self.conn.commit()

    def get_analyses(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch-fetch stored verdicts: {listing_id: row-with-verdict_json}."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM llm_analysis WHERE listing_id IN ({marks})",
            tuple(ids),
        ).fetchall()
        return {r["listing_id"]: dict(r) for r in rows}

    def add_subscription(self, sub: dict[str, Any]) -> None:
        keys = sub.get("keys") or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions "
            "(endpoint, p256dh, auth, created) VALUES (?, ?, ?, ?)",
            (sub["endpoint"], keys.get("p256dh"), keys.get("auth"), _now()),
        )
        self.conn.commit()

    def remove_subscription(self, endpoint: str) -> None:
        self.conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self.conn.commit()

    def all_subscriptions(self) -> list[dict[str, Any]]:
        """In the shape pywebpush expects: {endpoint, keys:{p256dh, auth}}."""
        rows = self.conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
        return [{"endpoint": r["endpoint"],
                 "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}}
                for r in rows]

    def record_run(self, search_key: str, ok: bool, duration_ms: int,
                   result: "SyncResult | None" = None,
                   error: str | None = None) -> None:
        """Record the outcome of one search's sync (for dashboard visibility)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_runs "
            "(search_key, ts, ok, duration_ms, new, price_changes, removed, "
            "seen, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (search_key, _now(), 1 if ok else 0, duration_ms,
             len(result.new) if result else None,
             len(result.price_changes) if result else None,
             len(result.removed) if result else None,
             result.total_seen if result else None,
             error),
        )
        self.conn.commit()

    def last_runs(self) -> dict[str, dict[str, Any]]:
        """Latest sync run per search_key."""
        rows = self.conn.execute(
            "SELECT r.* FROM sync_runs r JOIN (SELECT search_key, MAX(ts) mt "
            "FROM sync_runs GROUP BY search_key) m "
            "ON r.search_key = m.search_key AND r.ts = m.mt",
        ).fetchall()
        return {r["search_key"]: dict(r) for r in rows}

    def excluded_listings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE active = 1 AND excluded = 1 "
            "ORDER BY search_key, price",
        ).fetchall()
        return [dict(r) for r in rows]

    def price_history(self, listing_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT price, currency, ts FROM price_history WHERE id = ? "
            "ORDER BY ts",
            (listing_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def histories(self, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Batch-fetch price history for many listings at once (id -> series)."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, price, currency, ts FROM price_history "
            f"WHERE id IN ({marks}) ORDER BY ts",
            tuple(ids),
        ).fetchall()
        out: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["id"], []).append(
                {"price": r["price"], "currency": r["currency"], "ts": r["ts"]}
            )
        return out
