"""Real per-region sell order depth, not a single blended price.

price_cache.py's aggregate functions (Fuzzwork's `sell_percentile` etc.) give
one summary price per (region/station, type) from a single batched call --
cheap, but useless for answering "what does it cost to buy exactly N units,"
since a bulk purchase can blow straight through the cheap end of the order
book into much pricier orders. Confirmed this live: Scordite III-Grade at
Hek's actual station has exactly one sell order (26,442 units @ 23.00 ISK) --
Fuzzwork's aggregate price for a bulk buy there was misleading by construction,
not just imprecise.

This module fetches the real order book from ESI (which price_cache.py
deliberately does *not* use for its bulk refresh, since ESI's orders endpoint
is per-type, not batchable across types the way Fuzzwork's aggregates are --
price_cache.py's cheap aggregate call is still used as a first-pass liquidity
filter before paying the cost of a real per-type ESI fetch). Returned as
price tiers (price, remaining quantity, location) sorted ascending, so a
purchase plan can spend the cheapest tier first and spill into the next
exactly the way a real buy order would.

In practice pagination is rarely a concern -- confirmed even Tritanium in
Jita (about as liquid as this game's economy gets) is a single ESI page.

Fetches for missing/stale (region, type) pairs run concurrently
(ThreadPoolExecutor) -- this is plain network I/O (requests.get releases the
GIL while waiting on a socket), not a call into native/C solver code, so it
doesn't carry the scipy/HiGHS concurrent-call danger documented in
optimizer.py's optimize_many(). "Cheapest overall" mode can mean 15-20
regions x dozens of surviving ore candidates -- sequential fetching there
was measured taking several minutes; concurrency is what keeps that in the
neighborhood of the latency the user accepted for depth-aware pricing.
All sqlite3 access stays on the calling thread (connections aren't
thread-safe to share): worker threads only do HTTP, and results are written
back in one batch after every fetch completes.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from indycalc import db
from indycalc.sde_loader import DB_PATH

ORDERS_URL_TEMPLATE = "https://esi.evetech.net/latest/markets/{region_id}/orders/"

# Shared session with automatic retry/backoff for transient failures (ESI
# returns occasional 502/503/504s, especially under the concurrent load
# get_tiers() generates for "cheapest overall" mode). A Session's connection
# pool is thread-safe for concurrent GETs from a ThreadPoolExecutor -- this
# is standard requests usage, unrelated to the scipy/HiGHS native-threading
# danger documented in optimizer.py.
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])),
)

# Cheapest N sell orders kept per (region, type) -- far more than any single
# blueprint calculation realistically needs, keeps MILP variable count bounded.
MAX_TIERS_PER_REGION_TYPE = 20

# Order books move faster than the SDE but don't need refetching on every
# call within the same session -- an hour is a reasonable balance.
STALE_AFTER_SECONDS = 3600

# Concurrent ESI requests for missing/stale (region, type) pairs. ESI's
# published per-IP error-budget window is far higher than this -- kept
# modest to stay a good citizen, not because higher would be unsafe here.
MAX_WORKERS = 20


@dataclass
class PriceTier:
    price: float
    max_qty: int
    location_id: int


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_book_cache (
            region_id INTEGER NOT NULL,
            type_id INTEGER NOT NULL,
            price REAL NOT NULL,
            volume_remain INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_order_book_region_type ON order_book_cache(region_id, type_id)"
    )
    conn.commit()


def _fetch_live(region_id: int, type_id: int) -> list[tuple[float, int, int]]:
    """Live ESI fetch: cheapest sell orders for `type_id` in `region_id`,
    sorted ascending by price, capped to MAX_TIERS_PER_REGION_TYPE."""
    url = ORDERS_URL_TEMPLATE.format(region_id=region_id)
    orders: list[dict] = []
    page = 1
    try:
        while True:
            resp = _session.get(
                url,
                params={"order_type": "sell", "type_id": type_id, "page": page, "datasource": "tranquility"},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            orders.extend(batch)
            total_pages = int(resp.headers.get("X-Pages", "1"))
            if page >= total_pages or not batch:
                break
            page += 1
    except requests.exceptions.RequestException:
        # Already retried (via the session's Retry adapter) and still
        # failing -- one persistently-unreachable (region, type) pair
        # shouldn't abort an otherwise-successful multi-region calculation.
        # Treated the same as "no real orders found here": that candidate
        # just doesn't show up, same as any other illiquid one.
        return []
    orders.sort(key=lambda o: o["price"])
    return [
        (float(o["price"]), int(o["volume_remain"]), int(o["location_id"]))
        for o in orders[:MAX_TIERS_PER_REGION_TYPE]
    ]


def _fresh_pairs(conn, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """Which of `pairs` (region_id, type_id) have a cache entry newer than
    STALE_AFTER_SECONDS -- one batched query instead of one per pair."""
    if not pairs:
        return set()
    region_ids = sorted({p[0] for p in pairs})
    type_ids = sorted({p[1] for p in pairs})
    region_placeholders = ",".join("?" for _ in region_ids)
    type_placeholders = ",".join("?" for _ in type_ids)
    rows = conn.execute(
        f"""
        SELECT region_id, type_id, MAX(fetched_at)
        FROM order_book_cache
        WHERE region_id IN ({region_placeholders}) AND type_id IN ({type_placeholders})
        GROUP BY region_id, type_id
        """,
        region_ids + type_ids,
    ).fetchall()
    now = time.time()
    fresh_lookup = {(r, t): fetched_at for r, t, fetched_at in rows}
    return {p for p in pairs if p in fresh_lookup and (now - fresh_lookup[p]) < STALE_AFTER_SECONDS}


def _cached_rows(conn, pairs: set[tuple[int, int]]) -> dict[tuple[int, int], list[tuple[float, int, int]]]:
    if not pairs:
        return {}
    region_ids = sorted({p[0] for p in pairs})
    type_ids = sorted({p[1] for p in pairs})
    region_placeholders = ",".join("?" for _ in region_ids)
    type_placeholders = ",".join("?" for _ in type_ids)
    rows = conn.execute(
        f"""
        SELECT region_id, type_id, price, volume_remain, location_id
        FROM order_book_cache
        WHERE region_id IN ({region_placeholders}) AND type_id IN ({type_placeholders})
        ORDER BY price
        """,
        region_ids + type_ids,
    ).fetchall()
    out: dict[tuple[int, int], list[tuple[float, int, int]]] = {}
    for region_id, type_id, price, volume_remain, location_id in rows:
        key = (region_id, type_id)
        if key in pairs:
            out.setdefault(key, []).append((price, volume_remain, location_id))
    return out


def get_tiers(
    type_ids: list[int],
    region_ids: list[int],
    location_filter: set[int] | None = None,
    db_path: Path = DB_PATH,
) -> dict[int, list[PriceTier]]:
    """{type_id: [PriceTier, ...]} pooled across `region_ids`, sorted
    ascending by price, capped to MAX_TIERS_PER_REGION_TYPE total per type.

    Pass `location_filter` (a set of station/structure ids) to keep only
    tiers at those specific locations -- used for single-station and
    multi-station-combo pricing, where `region_ids` would be each station's
    own region (order fetching is per-region; filtering to a station happens
    client-side afterward since ESI doesn't support filtering by station).
    """
    if not type_ids or not region_ids:
        return {}
    unique_regions = sorted(set(region_ids))
    all_pairs = [(region_id, type_id) for type_id in type_ids for region_id in unique_regions]

    conn = db.connect(db_path)
    try:
        _ensure_table(conn)
        fresh = _fresh_pairs(conn, all_pairs)
        missing = [p for p in all_pairs if p not in fresh]

        fetched: dict[tuple[int, int], list[tuple[float, int, int]]] = {}
        if missing:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(_fetch_live, region_id, type_id): (region_id, type_id)
                    for region_id, type_id in missing
                }
                for future in futures:
                    key = futures[future]
                    fetched[key] = future.result()

            fetched_at = time.time()
            # Exact per-pair delete -- an IN-list on region_ids and type_ids
            # independently would be a cross product and could wipe a still-
            # fresh (region, type) row that merely shares a region or type
            # with one of the stale pairs being replaced.
            conn.executemany(
                "DELETE FROM order_book_cache WHERE region_id = ? AND type_id = ?", missing
            )
            conn.executemany(
                "INSERT INTO order_book_cache (region_id, type_id, price, volume_remain, location_id, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (region_id, type_id, p, v, loc, fetched_at)
                    for (region_id, type_id), tiers in fetched.items()
                    for p, v, loc in tiers
                ],
            )
            conn.commit()

        cached = _cached_rows(conn, fresh)

        result: dict[int, list[PriceTier]] = {}
        for type_id in type_ids:
            pooled: list[PriceTier] = []
            for region_id in unique_regions:
                key = (region_id, type_id)
                rows = fetched.get(key) if key in fetched else cached.get(key, [])
                for price, volume_remain, location_id in rows or []:
                    if location_filter is not None and location_id not in location_filter:
                        continue
                    pooled.append(PriceTier(price, volume_remain, location_id))
            pooled.sort(key=lambda t: t.price)
            result[type_id] = pooled[:MAX_TIERS_PER_REGION_TYPE]
        return result
    finally:
        conn.close()
