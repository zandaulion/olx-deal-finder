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
"""

_FIELDS = [
    "id", "search_key", "url", "title", "price", "currency", "negotiable",
    "previous_price", "model", "state", "city", "region", "is_business",
    "photo", "created_time", "last_refresh_time",
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
        """Aggregate snapshots into one candle per day: min/max span the whole
        day; Q1/median/Q3/n come from that day's latest snapshot."""
        rows = self.conn.execute(
            "SELECT day, n, min, q1, median, q3, max FROM price_stats "
            "WHERE search_key = ? ORDER BY ts",
            (search_key,),
        ).fetchall()
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = by_day.get(r["day"])
            if d is None:
                d = {"day": r["day"], "low": r["min"], "high": r["max"]}
                by_day[r["day"]] = d
            if r["min"] is not None:
                d["low"] = r["min"] if d["low"] is None else min(d["low"], r["min"])
            if r["max"] is not None:
                d["high"] = r["max"] if d["high"] is None else max(d["high"], r["max"])
            # Latest snapshot of the day wins for the distribution body.
            d["q1"], d["median"], d["q3"], d["n"] = (
                r["q1"], r["median"], r["q3"], r["n"])
        return list(by_day.values())

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
