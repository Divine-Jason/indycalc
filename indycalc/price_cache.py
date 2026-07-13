"""Multi-region ore price cache, refreshed on demand (never automatically) to
avoid hammering Fuzzworks. One Fuzzworks aggregates call per trade hub region
(all ore type IDs batched into that single call via the comma-separated
`types` parameter) -- not one call per ore per region.

Note: the correct host is market.fuzzwork.co.uk (singular). The
"market.fuzzworks.co.uk" (with an s) host given in the original project
notes does not resolve.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import requests

from indycalc import db
from indycalc.sde_loader import DB_PATH

AGGREGATES_URL = "https://market.fuzzwork.co.uk/aggregates/"

# Standard mineral type IDs, fetched alongside ore prices so leftover
# ("waste") minerals can be valued in ISK, not just quantity.
MINERAL_TYPE_IDS = {
    34: "Tritanium",
    35: "Pyerite",
    36: "Mexallon",
    37: "Isogen",
    38: "Nocxium",
    39: "Zydrine",
    40: "Megacyte",
    11399: "Morphite",
}

# The five major player trade hubs, mapped to their dominant region for the
# "scattered across the region" comparison, and to their actual trade station
# for the "everything from one station" comparison (Fuzzwork's aggregates
# endpoint takes a `station=` param, confirmed to return genuinely different,
# station-specific numbers from the region-wide `region=` aggregate). Station
# IDs looked up from the SDE's staStations table, not guessed:
#   Jita 4 - Moon 4 - Caldari Navy Assembly Plant, Amarr VIII (Oris) - Emperor
#   Family Academy, Dodixie 9 - Moon 20 - Federation Navy Assembly Plant,
#   Rens 6 - Moon 8 - Brutor Tribe Treasury, Hek 8 - Moon 12 - Boundless
#   Creation Factory.
TRADE_HUBS: dict[str, str] = {
    "Jita": "The Forge",
    "Amarr": "Domain",
    "Dodixie": "Sinq Laison",
    "Rens": "Heimatar",
    "Hek": "Metropolis",
}

HUB_STATION_IDS: dict[str, int] = {
    "Jita": 60003760,
    "Amarr": 60008494,
    "Dodixie": 60011866,
    "Rens": 60004588,
    "Hek": 60005686,
}

# The regions that contain a major trade hub -- this is the scope for
# "cheapest overall" (region-wide, not restricted to the hub's own station,
# so e.g. Perimeter's market still counts as part of The Forge/Jita's
# region) and for the aggregate liquidity pre-filter. Deliberately not "all
# ~19 highsec regions" (an earlier version of this tool searched all of
# them) -- EVE's bulk-purchase liquidity is concentrated enough in these 5
# that the other highsec regions added real scan latency (real per-order
# ESI fetches, not a cheap batched call) for essentially no better prices in
# practice.
HUB_REGION_NAMES = list(dict.fromkeys(TRADE_HUBS.values()))

_CHUNK_SIZE = 200  # generous headroom well under any practical URL length limit

# A single troll/leftover sell order (e.g. 14 units listed at a fraction of a
# fair price) can otherwise look like the "cheapest" source and blow up the
# optimizer's plan. Require a minimum listed quantity and order count before
# trusting a (region, type) price as a real bulk source.
MIN_LIQUIDITY_VOLUME = 1000
MIN_LIQUIDITY_ORDER_COUNT = 2


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            region_id INTEGER NOT NULL,
            type_id INTEGER NOT NULL,
            sell_min REAL,
            sell_percentile REAL,
            sell_volume REAL,
            sell_order_count INTEGER,
            buy_max REAL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (region_id, type_id)
        )
        """
    )
    # Older DBs created before liquidity columns existed: add them if missing.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(market_prices)")}
    for col, decl in [("sell_volume", "REAL"), ("sell_order_count", "INTEGER")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE market_prices ADD COLUMN {col} {decl}")
    conn.commit()


def _hub_region_ids(conn: sqlite3.Connection) -> dict[int, str]:
    placeholders = ",".join("?" for _ in HUB_REGION_NAMES)
    rows = conn.execute(
        f"SELECT region_id, region_name FROM regions WHERE region_name IN ({placeholders})",
        HUB_REGION_NAMES,
    ).fetchall()
    return {region_id: name for region_id, name in rows}


def _all_ore_type_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT type_id FROM ore_tiers").fetchall()
    return [r[0] for r in rows]


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_aggregates_rows(location_id: int, type_ids: list[int], data: dict, fetched_at: float) -> list[tuple]:
    rows = []
    for type_id_str, entry in data.items():
        sell = entry.get("sell", {})
        buy = entry.get("buy", {})
        rows.append(
            (
                location_id,
                int(type_id_str),
                float(sell["min"]) if sell.get("min") else None,
                float(sell["percentile"]) if sell.get("percentile") else None,
                float(sell["volume"]) if sell.get("volume") else None,
                int(float(sell["orderCount"])) if sell.get("orderCount") else None,
                float(buy["max"]) if buy.get("max") else None,
                fetched_at,
            )
        )
    return rows


def _fetch_and_store(
    conn: sqlite3.Connection, type_ids: list[int], region_ids: dict[int, str]
) -> int:
    """Fetch aggregates for `type_ids` across `region_ids` and upsert into
    market_prices. One Fuzzworks call per (region, chunk-of-200-types)."""
    fetched_at = time.time()
    written = 0
    for region_id in region_ids:
        for chunk in _chunked(type_ids, _CHUNK_SIZE):
            resp = requests.get(
                AGGREGATES_URL,
                params={"region": region_id, "types": ",".join(map(str, chunk))},
                timeout=30,
            )
            resp.raise_for_status()
            rows = _parse_aggregates_rows(region_id, chunk, resp.json(), fetched_at)
            conn.executemany(
                """
                INSERT INTO market_prices
                    (region_id, type_id, sell_min, sell_percentile, sell_volume,
                     sell_order_count, buy_max, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(region_id, type_id) DO UPDATE SET
                    sell_min=excluded.sell_min,
                    sell_percentile=excluded.sell_percentile,
                    sell_volume=excluded.sell_volume,
                    sell_order_count=excluded.sell_order_count,
                    buy_max=excluded.buy_max,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
            written += len(rows)
            # Commit after each region/chunk rather than once at the end -- this
            # loop makes a live network call per iteration, and holding a write
            # transaction open across many seconds of network I/O is exactly
            # what causes "database is locked" against a concurrent reader/writer
            # (e.g. another Streamlit session rerun).
            conn.commit()
    return written


def _ensure_station_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS station_prices (
            station_id INTEGER NOT NULL,
            type_id INTEGER NOT NULL,
            sell_min REAL,
            sell_percentile REAL,
            sell_volume REAL,
            sell_order_count INTEGER,
            buy_max REAL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (station_id, type_id)
        )
        """
    )
    conn.commit()


def refresh_station_prices(db_path: Path = DB_PATH) -> int:
    """Refresh cached sell prices for all ore + mineral types at each of the
    5 major hub stations. Returns the number of (station, type) rows written.
    """
    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        _ensure_station_table(conn)
        type_ids = _all_ore_type_ids(conn) + list(MINERAL_TYPE_IDS)
        fetched_at = time.time()
        written = 0
        for station_id in HUB_STATION_IDS.values():
            for chunk in _chunked(type_ids, _CHUNK_SIZE):
                resp = requests.get(
                    AGGREGATES_URL,
                    params={"station": station_id, "types": ",".join(map(str, chunk))},
                    timeout=30,
                )
                resp.raise_for_status()
                rows = _parse_aggregates_rows(station_id, chunk, resp.json(), fetched_at)
                conn.executemany(
                    """
                    INSERT INTO station_prices
                        (station_id, type_id, sell_min, sell_percentile, sell_volume,
                         sell_order_count, buy_max, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_id, type_id) DO UPDATE SET
                        sell_min=excluded.sell_min,
                        sell_percentile=excluded.sell_percentile,
                        sell_volume=excluded.sell_volume,
                        sell_order_count=excluded.sell_order_count,
                        buy_max=excluded.buy_max,
                        fetched_at=excluded.fetched_at
                    """,
                    rows,
                )
                written += len(rows)
                conn.commit()
        return written
    finally:
        conn.close()


def best_station_prices(type_ids: list[int], station_id: int, db_path: Path = DB_PATH) -> dict[int, dict]:
    """Cheapest cached sell price for each type_id at a single station:
    {type_id: {"price": float}}. Same liquidity filter as best_prices()."""
    if not type_ids:
        return {}
    conn = db.connect(db_path)
    try:
        _ensure_station_table(conn)
        placeholders = ",".join("?" for _ in type_ids)
        rows = conn.execute(
            f"""
            SELECT type_id, sell_min, sell_percentile
            FROM station_prices
            WHERE station_id = ? AND type_id IN ({placeholders})
              AND sell_volume >= {MIN_LIQUIDITY_VOLUME}
              AND sell_order_count >= {MIN_LIQUIDITY_ORDER_COUNT}
            """,
            [station_id] + list(type_ids),
        ).fetchall()
        best: dict[int, dict] = {}
        for type_id, sell_min, sell_percentile in rows:
            price = sell_percentile if sell_percentile is not None else sell_min
            if price is not None:
                best[type_id] = {"price": price}
        return best
    finally:
        conn.close()


def get_or_fetch_station_prices(type_ids: list[int], station_id: int, db_path: Path = DB_PATH) -> dict[int, dict]:
    """Like get_or_fetch_prices() but for a single station -- covers
    components/reaction materials that aren't part of the bulk ore/mineral
    station refresh, fetching live (once) for whatever isn't cached yet."""
    if not type_ids:
        return {}
    conn = db.connect(db_path)
    try:
        _ensure_station_table(conn)
        placeholders = ",".join("?" for _ in type_ids)
        cached = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT type_id FROM station_prices WHERE station_id = ? AND type_id IN ({placeholders})",
                [station_id] + list(type_ids),
            ).fetchall()
        }
        missing = [t for t in type_ids if t not in cached]
        if missing:
            fetched_at = time.time()
            for chunk in _chunked(missing, _CHUNK_SIZE):
                resp = requests.get(
                    AGGREGATES_URL,
                    params={"station": station_id, "types": ",".join(map(str, chunk))},
                    timeout=30,
                )
                resp.raise_for_status()
                rows = _parse_aggregates_rows(station_id, chunk, resp.json(), fetched_at)
                conn.executemany(
                    """
                    INSERT INTO station_prices
                        (station_id, type_id, sell_min, sell_percentile, sell_volume,
                         sell_order_count, buy_max, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_id, type_id) DO UPDATE SET
                        sell_min=excluded.sell_min,
                        sell_percentile=excluded.sell_percentile,
                        sell_volume=excluded.sell_volume,
                        sell_order_count=excluded.sell_order_count,
                        buy_max=excluded.buy_max,
                        fetched_at=excluded.fetched_at
                    """,
                    rows,
                )
                conn.commit()
    finally:
        conn.close()
    return best_station_prices(type_ids, station_id, db_path)


def best_multi_station_prices(
    type_ids: list[int], station_ids: list[int], db_path: Path = DB_PATH
) -> dict[int, dict]:
    """Cheapest cached sell price for each type_id, pooled across several
    stations: {type_id: {"price": float, "location_name": str}} where
    location_name is whichever *specific* station in the list was cheapest
    for that item (from HUB_STATION_IDS's reverse mapping)."""
    if not type_ids or not station_ids:
        return {}
    station_names = {sid: name for name, sid in HUB_STATION_IDS.items() if sid in station_ids}
    conn = db.connect(db_path)
    try:
        _ensure_station_table(conn)
        type_placeholders = ",".join("?" for _ in type_ids)
        station_placeholders = ",".join("?" for _ in station_ids)
        rows = conn.execute(
            f"""
            SELECT type_id, station_id, sell_min, sell_percentile
            FROM station_prices
            WHERE station_id IN ({station_placeholders}) AND type_id IN ({type_placeholders})
              AND sell_volume >= {MIN_LIQUIDITY_VOLUME}
              AND sell_order_count >= {MIN_LIQUIDITY_ORDER_COUNT}
            """,
            list(station_ids) + list(type_ids),
        ).fetchall()
        best: dict[int, dict] = {}
        for type_id, station_id, sell_min, sell_percentile in rows:
            price = sell_percentile if sell_percentile is not None else sell_min
            if price is None:
                continue
            current = best.get(type_id)
            if current is None or price < current["price"]:
                best[type_id] = {"price": price, "location_name": station_names.get(station_id, str(station_id))}
        return best
    finally:
        conn.close()


def get_or_fetch_multi_station_prices(
    type_ids: list[int], station_ids: list[int], db_path: Path = DB_PATH
) -> dict[int, dict]:
    """Like get_or_fetch_station_prices() but pooled across several stations
    -- covers components/reaction materials not part of the bulk refresh."""
    if not type_ids or not station_ids:
        return {}
    for station_id in station_ids:
        get_or_fetch_station_prices(type_ids, station_id, db_path)  # ensures each is cached
    return best_multi_station_prices(type_ids, station_ids, db_path)


def refresh_prices(db_path: Path = DB_PATH) -> int:
    """Refresh cached sell prices for all ore + mineral types across the 5
    trade hub regions. Returns the number of (region, type) price rows
    written.
    """
    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        region_ids = _hub_region_ids(conn)
        type_ids = _all_ore_type_ids(conn) + list(MINERAL_TYPE_IDS)
        if not region_ids:
            raise RuntimeError("No trade hub regions found in `regions` table -- run sde_loader first.")
        if not type_ids:
            raise RuntimeError("No ore types found in `ore_tiers` table -- run sde_loader first.")
        return _fetch_and_store(conn, type_ids, region_ids)
    finally:
        conn.close()


def get_or_fetch_prices(
    type_ids: list[int], db_path: Path = DB_PATH, region_names: list[str] | None = None
) -> dict[int, dict]:
    """Like best_prices(), but for blueprint components/PI/reaction materials
    that aren't part of the bulk ore+mineral refresh (there are far too many
    distinct components across all blueprints to pre-fetch speculatively).
    Any requested type_id with no cached row at all is fetched live -- one
    call per relevant region -- and cached for next time, so re-checking the
    same blueprint doesn't re-hit the network.
    """
    if not type_ids:
        return {}
    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        all_regions = _hub_region_ids(conn)
        region_ids = (
            {rid: name for rid, name in all_regions.items() if name in region_names}
            if region_names
            else all_regions
        )
        # Scope the "already cached" check to the regions we're about to search --
        # a component cached only for Jita shouldn't count as cached when the
        # caller is now asking about Amarr.
        type_placeholders = ",".join("?" for _ in type_ids)
        region_placeholders = ",".join("?" for _ in region_ids)
        cached = {
            row[0]
            for row in conn.execute(
                f"""SELECT DISTINCT type_id FROM market_prices
                    WHERE type_id IN ({type_placeholders})
                      AND region_id IN ({region_placeholders})""",
                list(type_ids) + list(region_ids),
            ).fetchall()
        }
        missing = [t for t in type_ids if t not in cached]
        if missing:
            _fetch_and_store(conn, missing, region_ids)
    finally:
        conn.close()
    return best_prices(type_ids, db_path, region_names=region_names)


def region_ids_for_names(region_names: list[str] | None, db_path: Path = DB_PATH) -> list[int]:
    """region_id for each hub region matching `region_names` (or all 5 hub
    regions if None -- this is what makes "cheapest overall" search only
    the trade hub regions, not every highsec region). Used by order_book.py,
    which fetches real per-order data from ESI -- scoped by region, not by
    the aggregate endpoint's arbitrary region/station param -- so callers
    need actual IDs, not just names."""
    conn = db.connect(db_path)
    try:
        all_regions = _hub_region_ids(conn)
        if region_names:
            return [rid for rid, name in all_regions.items() if name in region_names]
        return list(all_regions)
    finally:
        conn.close()


def station_region_id(station_id: int, db_path: Path = DB_PATH) -> int | None:
    """Which region a known hub station belongs to, via TRADE_HUBS -- needed
    because ESI's real order-book fetch (order_book.py) is scoped by region,
    then filtered down to a specific station's location_id client-side."""
    hub_name = next((name for name, sid in HUB_STATION_IDS.items() if sid == station_id), None)
    if hub_name is None:
        return None
    region_name = TRADE_HUBS.get(hub_name)
    if region_name is None:
        return None
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT region_id FROM regions WHERE region_name = ?", (region_name,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def last_refreshed(db_path: Path = DB_PATH) -> float | None:
    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT MAX(fetched_at) FROM market_prices").fetchone()
        return row[0]
    finally:
        conn.close()


def best_prices(
    type_ids: list[int], db_path: Path = DB_PATH, region_names: list[str] | None = None
) -> dict[int, dict]:
    """For each type_id, return the cheapest sell price across cached regions:
    {type_id: {"price": float, "region_name": str}}.
    Uses sell_percentile (robust against a single troll low-ball order) and
    falls back to sell_min if percentile is missing. Rows with too little
    listed volume/order count (MIN_LIQUIDITY_VOLUME / _ORDER_COUNT) are
    skipped -- a single leftover order for a handful of units otherwise looks
    like a great "price" and derails the whole plan.

    Pass `region_names` (e.g. a single trade hub's region) to restrict the
    search to those regions only, instead of scattering across all 5 hub
    regions.
    """
    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        placeholders = ",".join("?" for _ in type_ids)
        params: list = list(type_ids)
        region_filter = ""
        if region_names:
            region_placeholders = ",".join("?" for _ in region_names)
            region_filter = f"AND r.region_name IN ({region_placeholders})"
            params += list(region_names)
        rows = conn.execute(
            f"""
            SELECT mp.type_id, mp.sell_min, mp.sell_percentile, r.region_name
            FROM market_prices mp
            JOIN regions r ON r.region_id = mp.region_id
            WHERE mp.type_id IN ({placeholders})
              AND mp.sell_volume >= {MIN_LIQUIDITY_VOLUME}
              AND mp.sell_order_count >= {MIN_LIQUIDITY_ORDER_COUNT}
            {region_filter}
            """,
            params,
        ).fetchall()

        best: dict[int, dict] = {}
        for type_id, sell_min, sell_percentile, region_name in rows:
            price = sell_percentile if sell_percentile is not None else sell_min
            if price is None:
                continue
            current = best.get(type_id)
            if current is None or price < current["price"]:
                best[type_id] = {"price": price, "region_name": region_name}
        return best
    finally:
        conn.close()


if __name__ == "__main__":
    n = refresh_prices()
    print(f"Refreshed {n} region/ore price rows.")
