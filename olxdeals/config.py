"""Load search definitions from a YAML config into SearchSpec objects."""

from __future__ import annotations

from pathlib import Path

import yaml

from .fetcher import SearchSpec


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
    searches = [s for s in data["searches"] if s.get("key") != search["key"]]
    searches.append(search)
    data["searches"] = searches
    save_raw(path, data)


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
                price_from=raw.get("price_from"),
                price_to=raw.get("price_to"),
                region_id=raw.get("region_id"),
            )
        )
    if not specs:
        raise ValueError(f"No searches defined in {path}")
    return specs
