"""Devices and invites, the same model the sibling projects use.

There are no usernames or passwords. A device proves who it is with a random
token held in an HttpOnly cookie, issued when an invite is redeemed. One invite
registers exactly one device, which is what makes "two phones = two invites"
fall out naturally.

Tokens are stored hashed: a leaked database should not hand over working
credentials.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

COOKIE_NAME = "olx_device"
# Browsers clamp cookie lifetime to 400 days; asking for more is pointless.
COOKIE_MAX_AGE = 400 * 24 * 3600
INVITE_TTL = timedelta(days=int(os.getenv("INVITE_TTL_DAYS", "7")))
# An invite stays redeemable for this long after its FIRST use, re-binding the
# same device rather than creating another. Chat apps open links in their own
# browser, whose cookie jar the installed PWA cannot read, so the realistic
# flow is: redeem in the viewer, install properly, redeem again. The window is
# absolute -- it does not slide with each use -- so a forwarded link is not a
# week-long open door.
INVITE_REBIND = timedelta(minutes=int(os.getenv("INVITE_REBIND_MINUTES", "60")))

# Unambiguous alphabet: no I/1, no O/0, so a code can be read aloud or typed
# from a phone screen without confusion. 12 chars = ~60 bits.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_GROUPS, _GROUP_LEN = 3, 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
    id         INTEGER PRIMARY KEY,
    code_hash  TEXT NOT NULL UNIQUE,
    -- The code in the clear, kept ONLY while the invite is still usable so it
    -- can be shown again; wiped on redemption.
    code_plain TEXT,
    label      TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    -- Cancelled, rather than deleted, so the console can show that an invite
    -- was withdrawn instead of leaving a gap indistinguishable from one that
    -- was never sent.
    revoked    INTEGER NOT NULL DEFAULT 0,
    device_id  INTEGER REFERENCES devices(id) ON DELETE SET NULL
);
"""

_db_path = "olxdeals.db"


def configure(path: str | Path) -> None:
    """Point the module at the dashboard's database and ensure the tables."""
    global _db_path
    _db_path = str(path)
    with _connect() as con:
        con.executescript(SCHEMA)
        # Cancelling an invite used to delete the row; it is flagged now, so an
        # existing database needs the column. Every surviving row is live by
        # definition -- the cancelled ones were already deleted.
        invite_cols = {r["name"] for r in con.execute(
            "PRAGMA table_info(invites)").fetchall()}
        if "revoked" not in invite_cols:
            con.execute(
                "ALTER TABLE invites ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
            con.commit()
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "push_subscriptions" in tables:
            cols = {r["name"] for r in con.execute(
                "PRAGMA table_info(push_subscriptions)").fetchall()}
            if "device_id" not in cols:
                try:
                    con.execute(
                        "ALTER TABLE push_subscriptions ADD COLUMN device_id "
                        "INTEGER REFERENCES devices(id) ON DELETE SET NULL")
                    con.commit()
                except sqlite3.OperationalError:
                    pass


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def new_invite_code() -> str:
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN))
                    for _ in range(_GROUPS))


def normalise_code(raw: str) -> str:
    """Accept what a human types: lower case, missing or extra dashes."""
    cleaned = "".join(c for c in (raw or "").upper() if c in _ALPHABET)
    if len(cleaned) != _GROUPS * _GROUP_LEN:
        return ""
    return "-".join(cleaned[i:i + _GROUP_LEN]
                    for i in range(0, len(cleaned), _GROUP_LEN))


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------
# Redemption is the one endpoint that must answer before it knows who is
# asking. The per-address cap stops guessing; the global one is a backstop
# against a distributed attempt. Separate, because a single global counter lets
# one noisy client spend everyone's budget and lock out real sign-ups.
_REDEEM_WINDOW = 60.0
_REDEEM_MAX_IP = int(os.getenv("REDEEM_MAX_PER_MIN_IP", "5"))
_REDEEM_MAX_ALL = int(os.getenv("REDEEM_MAX_PER_MIN", "120"))
_attempts: dict[str, list[float]] = {}


def throttled(ip: str) -> bool:
    """True if this attempt should be refused. Records it when it is not."""
    now = time.monotonic()
    for key, times in list(_attempts.items()):
        fresh = [t for t in times if now - t < _REDEEM_WINDOW]
        if fresh:
            _attempts[key] = fresh
        else:
            del _attempts[key]
    if len(_attempts.get(ip, ())) >= _REDEEM_MAX_IP:
        return True
    if sum(len(v) for v in _attempts.values()) >= _REDEEM_MAX_ALL:
        return True
    _attempts.setdefault(ip, []).append(now)
    return False


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------
def device_by_token(token: str) -> dict | None:
    if not token:
        return None
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM devices WHERE token_hash = ? AND revoked = 0",
            (_hash(token),)).fetchone()
        if not row:
            return None
        con.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (_now(), row["id"]))
        con.commit()
        return dict(row)


def list_devices() -> list[dict]:
    with _connect() as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        has_device_col = False
        if "push_subscriptions" in tables:
            cols = {r["name"] for r in con.execute(
                "PRAGMA table_info(push_subscriptions)").fetchall()}
            has_device_col = "device_id" in cols
        push_expr = ("(SELECT COUNT(*) FROM push_subscriptions p WHERE p.device_id = d.id) AS has_push"
                     if has_device_col else "0 AS has_push")
        return [dict(r) for r in con.execute(
            f"SELECT d.id, d.label, d.created_at, d.last_seen, d.revoked, {push_expr} "
            "FROM devices d ORDER BY d.id")]


def set_revoked(device_id: int, revoked: bool) -> bool:
    with _connect() as con:
        cur = con.execute("UPDATE devices SET revoked = ? WHERE id = ?",
                          (1 if revoked else 0, device_id))
        con.commit()
        return cur.rowcount > 0


def rename_device(device_id: int, label: str) -> bool:
    with _connect() as con:
        cur = con.execute("UPDATE devices SET label = ? WHERE id = ?", (label, device_id))
        con.commit()
        return cur.rowcount > 0


def delete_device(device_id: int) -> bool:
    with _connect() as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "push_subscriptions" in tables:
            cols = {r["name"] for r in con.execute(
                "PRAGMA table_info(push_subscriptions)").fetchall()}
            if "device_id" in cols:
                con.execute("DELETE FROM push_subscriptions WHERE device_id = ?", (device_id,))
        cur = con.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        con.commit()
        return cur.rowcount > 0


def prune_devices() -> int:
    with _connect() as con:
        cur = con.execute("DELETE FROM devices WHERE revoked = 1")
        con.commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------
def create_invite(label: str | None = None) -> str:
    code = new_invite_code()
    with _connect() as con:
        con.execute(
            "INSERT INTO invites (code_hash, code_plain, label, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (_hash(code), code, label, _now(),
             (datetime.now(timezone.utc) + INVITE_TTL).isoformat(timespec="seconds")))
        con.commit()
    return code


def list_invites() -> list[dict]:
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, label, created_at, expires_at, used_at, revoked, device_id, "
            "code_plain FROM invites ORDER BY id DESC")]


def revoke_invite(invite_id: int) -> bool:
    with _connect() as con:
        # Flagged, not deleted: a cancelled invite that vanishes leaves no
        # answer to "did I cancel that, or never send it?"
        cur = con.execute(
            "UPDATE invites SET revoked = 1 "
            "WHERE id = ? AND used_at IS NULL AND revoked = 0",
            (invite_id,))
        con.commit()
        return cur.rowcount > 0


def prune_invites() -> int:
    """Drop invites that can no longer register anything: already used, or
    expired unused. Pending ones are never touched."""
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM invites WHERE used_at IS NOT NULL OR expires_at < ?", (_now(),))
        con.commit()
        return cur.rowcount


class InviteError(Exception):
    """Redemption refused. The message is safe to show the user."""


def redeem(code: str) -> tuple[int, str]:
    """-> (device_id, device_token). Single use, enforced under a write lock."""
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM invites WHERE code_hash = ?",
                          (_hash(code),)).fetchone()
        if not row:
            raise InviteError("Codul de invitație nu este valid.")
        if row["revoked"]:
            raise InviteError("Invitația a fost anulată. Cere una nouă.")
        now = datetime.now(timezone.utc)
        if datetime.fromisoformat(row["expires_at"]) < now:
            raise InviteError("Invitația a expirat. Cere una nouă.")

        rebind_to = None
        if row["used_at"]:
            first = datetime.fromisoformat(row["used_at"])
            if now - first <= INVITE_REBIND and row["device_id"]:
                # Same device, new token: whichever browser redeems last wins,
                # and the earlier context is signed out by the rotation.
                rebind_to = row["device_id"]
            else:
                raise InviteError("Invitația a fost deja folosită.")

        token = new_device_token()
        if rebind_to:
            con.execute("UPDATE devices SET token_hash = ?, revoked = 0 WHERE id = ?",
                        (_hash(token), rebind_to))
            device_id = rebind_to
        else:
            cur = con.execute(
                "INSERT INTO devices (token_hash, label, created_at) VALUES (?,?,?)",
                (_hash(token), row["label"], _now()))
            device_id = cur.lastrowid

        # Keep the original used_at so the grace window stays absolute. The
        # plaintext goes at the same time: after this the code can only re-bind
        # an existing device, and not at all once the window closes.
        con.execute(
            "UPDATE invites SET used_at = COALESCE(used_at, ?), device_id = ?, "
            "code_plain = NULL WHERE id = ?", (_now(), device_id, row["id"]))
        con.commit()
        return device_id, token
