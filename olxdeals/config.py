"""Load search definitions from a YAML config into SearchSpec objects."""

from __future__ import annotations

from pathlib import Path

import yaml

from .fetcher import SearchSpec


YAML_ONLY_KEYS = ("title_match", "require", "exclude", "notify")


def load_raw(path: str | Path) -> dict:
    """Return the parsed YAML as a plain dict (for editing/round-tripping)."""
    p = Path(path)
    if not p.exists():
        return {"searches": []}
    data = yaml.safe_load(p.read_text()) or {}
    data.setdefault("searches", [])
    return data


def save_raw(path: str | Path, data: dict) -> None:
    Path(path).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )


def upsert_search(path: str | Path, search: dict) -> None:
    """Add a search, or replace an existing one with the same key."""
    data = load_raw(path)
    old = next((s for s in data["searches"]
                if s.get("key") == search["key"]), None)
    if old and old.get("paused"):  # editing shouldn't silently un-pause
        search["paused"] = True
    # Text filters and notify mode are YAML-only; the Manage form doesn't
    # know them, so carry them over rather than dropping them on save.
    for k in YAML_ONLY_KEYS:
        if old and k in old and k not in search:
            search[k] = old[k]
    searches = [s for s in data["searches"] if s.get("key") != search["key"]]
    searches.append(search)
    data["searches"] = searches
    save_raw(path, data)


def toggle_paused(path: str | Path, key: str) -> bool:
    """Flip a search's paused flag; returns the new state."""
    data = load_raw(path)
    new_state = False
    for s in data["searches"]:
        if s.get("key") == key:
            new_state = not bool(s.get("paused"))
            if new_state:
                s["paused"] = True
            else:
                s.pop("paused", None)
    save_raw(path, data)
    return new_state


def delete_search(path: str | Path, key: str) -> None:
    data = load_raw(path)
    data["searches"] = [s for s in data["searches"] if s.get("key") != key]
    save_raw(path, data)


def load_searches(path: str | Path) -> list[SearchSpec]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    specs: list[SearchSpec] = []
    for raw in data.get("searches", []):
        specs.append(
            SearchSpec(
                key=raw["key"],
                category_id=raw.get("category_id", 0),
                query=raw.get("query"),
                filters=raw.get("filters", {}) or {},
                ranges=raw.get("ranges", {}) or {},
                price_from=raw.get("price_from"),
                price_to=raw.get("price_to"),
                region_id=raw.get("region_id"),
                paused=bool(raw.get("paused")),
                title_match=raw.get("title_match"),
                require=list(raw.get("require") or []),
                exclude=raw.get("exclude"),
                notify=raw.get("notify", "deals"),
            )
        )
    if not specs:
        raise ValueError(f"No searches defined in {path}")
    return specs
