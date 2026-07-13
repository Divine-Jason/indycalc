"""Job installation fee (manufacturing/reaction) and BPC copying cost.

Both need data ESI publishes for free and globally (not per-region, so these
are each a single cheap call, refreshed on demand like everything else):
  - `/markets/prices/`: "adjusted_price" per type -- the EIV (Estimated Item
    Value) input. Different from the Jita/hub sell price already cached
    elsewhere; this is CCP's own slower-moving reference price used for
    taxation, not a live market price.
  - `/industry/systems/`: per-solar-system cost index per activity
    (manufacturing, reaction, copying, ...) -- reflects how busy that
    system's industry slots have been over the last ~28 days.

Formulas (EVE University wiki, this session):
  manufacturing/reaction job cost = EIV x (system_cost_index + facility_tax
    + SCC_surcharge), where EIV = sum(base_material_qty_per_run x
    adjusted_price) using the blueprint's *unresearched* (ME0) material
    quantities -- always, regardless of the ME% you're actually using.
  copying job cost = system_cost_index(copying) x runs x 0.02 x EIV (same
    per-run EIV as above).

Confidence note: the manufacturing/reaction formula is well documented. The
copying formula is less so -- even players debate the exact number publicly
-- so treat copy cost estimates as directional, not exact.

Facility tax is set by whoever owns the station/structure the job runs in;
there's no way to look that up generically, so it's a number the caller
supplies (a UI input), not fetched here.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import requests

from indycalc import db, production_chain
from indycalc.sde_loader import DB_PATH, MANUFACTURING_ACTIVITY_ID, REACTION_ACTIVITY_ID

# True security >= this counts as highsec for game purposes (displays as
# "0.5" after rounding at the boundary) -- used only for the "recommend a
# cheap highsec build system" search below. The manual system search above
# deliberately isn't restricted to this.
HIGHSEC_SECURITY_THRESHOLD = 0.45

ADJUSTED_PRICES_URL = "https://esi.evetech.net/latest/markets/prices/"
INDUSTRY_SYSTEMS_URL = "https://esi.evetech.net/latest/industry/systems/"
INDUSTRY_FACILITIES_URL = "https://esi.evetech.net/latest/industry/facilities/"

SCC_SURCHARGE_PCT = 4.0  # flat, well-documented, applies to every job
COPY_COST_FACTOR = 0.02  # per the copying formula above


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adjusted_prices (
            type_id INTEGER PRIMARY KEY,
            adjusted_price REAL NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_cost_indices (
            system_id INTEGER NOT NULL,
            activity TEXT NOT NULL,
            cost_index REAL NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (system_id, activity)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS industry_facility_systems (
            system_id INTEGER PRIMARY KEY,
            fetched_at REAL NOT NULL
        )
        """
    )
    conn.commit()


def refresh_industry_data(db_path: Path = DB_PATH) -> tuple[int, int, int]:
    """Refresh adjusted prices, system cost indices, and which systems have a
    public industry facility, all from ESI (3 calls total, all
    global/unfiltered). Returns (adjusted_price rows, cost_index rows,
    facility-system rows)."""
    conn = db.connect(db_path)
    try:
        _ensure_tables(conn)
        fetched_at = time.time()

        resp = requests.get(ADJUSTED_PRICES_URL, params={"datasource": "tranquility"}, timeout=30)
        resp.raise_for_status()
        price_rows = [
            (entry["type_id"], entry["adjusted_price"], fetched_at)
            for entry in resp.json()
            if "adjusted_price" in entry
        ]
        conn.executemany(
            """
            INSERT INTO adjusted_prices (type_id, adjusted_price, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(type_id) DO UPDATE SET
                adjusted_price=excluded.adjusted_price, fetched_at=excluded.fetched_at
            """,
            price_rows,
        )
        conn.commit()

        resp = requests.get(INDUSTRY_SYSTEMS_URL, params={"datasource": "tranquility"}, timeout=30)
        resp.raise_for_status()
        index_rows = [
            (entry["solar_system_id"], ci["activity"], ci["cost_index"], fetched_at)
            for entry in resp.json()
            for ci in entry.get("cost_indices", [])
        ]
        conn.executemany(
            """
            INSERT INTO system_cost_indices (system_id, activity, cost_index, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(system_id, activity) DO UPDATE SET
                cost_index=excluded.cost_index, fetched_at=excluded.fetched_at
            """,
            index_rows,
        )
        conn.commit()

        # /industry/facilities/ is the authoritative live list of stations
        # that currently offer industry services -- solves two problems at
        # once: (1) plenty of NPC stations don't offer manufacturing/
        # research/reaction at all (never did, this isn't a recent change),
        # so a system merely *having* a station isn't enough; (2) this
        # endpoint only returns public facilities, so player-owned Upwell
        # structures (which we have no way to know exist, are accessible,
        # or are freeport) are automatically excluded rather than guessed at.
        resp = requests.get(INDUSTRY_FACILITIES_URL, params={"datasource": "tranquility"}, timeout=30)
        resp.raise_for_status()
        facility_system_ids = {entry["solar_system_id"] for entry in resp.json()}
        facility_rows = [(system_id, fetched_at) for system_id in facility_system_ids]
        conn.execute("DELETE FROM industry_facility_systems")
        conn.executemany(
            "INSERT INTO industry_facility_systems (system_id, fetched_at) VALUES (?, ?)",
            facility_rows,
        )
        conn.commit()

        return len(price_rows), len(index_rows), len(facility_rows)
    finally:
        conn.close()


def last_refreshed(db_path: Path = DB_PATH) -> float | None:
    conn = db.connect(db_path)
    try:
        _ensure_tables(conn)
        row = conn.execute("SELECT MAX(fetched_at) FROM adjusted_prices").fetchone()
        return row[0]
    finally:
        conn.close()


def search_systems(query: str, db_path: Path = DB_PATH, limit: int = 25) -> list[tuple[int, str, float]]:
    """Return [(system_id, system_name, security), ...] matching query --
    every system (highsec, lowsec, null, and J-space/wormhole), not just
    highsec. Where you build isn't restricted the way where you buy is; if
    you're searching for a specific station/system by name you already know
    what you're doing."""
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT system_id, system_name, security FROM solar_systems WHERE system_name LIKE ? "
            "ORDER BY system_name LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [(int(r[0]), r[1], float(r[2])) for r in rows]
    finally:
        conn.close()


@dataclass
class SystemRecommendation:
    system_id: int
    system_name: str
    jumps: int
    cost_index: float


def recommend_build_systems(
    activity: str,
    from_system_id: int,
    max_jumps: int | None = None,
    top_n: int = 10,
    db_path: Path = DB_PATH,
) -> list[SystemRecommendation]:
    """The `top_n` highsec systems with the lowest cost index for `activity`
    ("manufacturing" or "reaction"), each tagged with its jump distance from
    `from_system_id` via a highsec-only route ("secure jumps" -- the same
    routing autopilot's "safest" uses). A highsec system with no highsec-only
    path from the start point (possible, if rare) is simply not included --
    there's no meaningful "secure jump count" to show for it.

    Restricted to systems with at least one public NPC station that
    currently offers industry services (per ESI's /industry/facilities/,
    refreshed alongside adjusted prices/cost indices) -- plenty of NPC
    stations never offered manufacturing/research/reaction at all, so
    merely having *a* station isn't enough. This also naturally excludes
    player-owned Upwell structures, since that endpoint only returns public
    facilities -- there's no way to know if one exists in a system, is
    accessible, or is freeport, so it's not worth guessing at.

    Deliberately restricted to highsec, unlike search_systems() -- this is a
    "just recommend something safe and cheap" convenience, not a precise
    tool, so it doesn't try to reason about low/null/J-space risk.
    """
    conn = db.connect(db_path)
    try:
        highsec_ids = {
            row[0]
            for row in conn.execute(
                "SELECT system_id FROM solar_systems WHERE security >= ?",
                (HIGHSEC_SECURITY_THRESHOLD,),
            ).fetchall()
        }
        if from_system_id not in highsec_ids:
            return []

        facility_system_ids = {
            row[0] for row in conn.execute("SELECT system_id FROM industry_facility_systems").fetchall()
        }

        edges = conn.execute("SELECT from_system_id, to_system_id FROM system_jumps").fetchall()
        adjacency: dict[int, list[int]] = {}
        for a, b in edges:
            if a in highsec_ids and b in highsec_ids:
                adjacency.setdefault(a, []).append(b)
                adjacency.setdefault(b, []).append(a)

        jumps_from_start: dict[int, int] = {from_system_id: 0}
        queue: deque[int] = deque([from_system_id])
        while queue:
            current = queue.popleft()
            if max_jumps is not None and jumps_from_start[current] >= max_jumps:
                continue
            for neighbor in adjacency.get(current, []):
                if neighbor not in jumps_from_start:
                    jumps_from_start[neighbor] = jumps_from_start[current] + 1
                    queue.append(neighbor)

        cost_rows = conn.execute(
            "SELECT system_id, cost_index FROM system_cost_indices WHERE activity = ?",
            (activity,),
        ).fetchall()
        names = {
            row[0]: row[1]
            for row in conn.execute("SELECT system_id, system_name FROM solar_systems").fetchall()
        }

        candidates = [
            SystemRecommendation(system_id, names.get(system_id, str(system_id)), jumps_from_start[system_id], cost_index)
            for system_id, cost_index in cost_rows
            if system_id in jumps_from_start and system_id in facility_system_ids
        ]
        # Many systems tie at the cost index floor (barely-used industry
        # slots) -- among ties, prefer the closer one, since "cheapest but
        # 40 jumps away" isn't actually the more useful recommendation.
        candidates.sort(key=lambda c: (c.cost_index, c.jumps))
        return candidates[:top_n]
    finally:
        conn.close()


def _cost_index(system_id: int, activity: str, conn) -> float | None:
    row = conn.execute(
        "SELECT cost_index FROM system_cost_indices WHERE system_id = ? AND activity = ?",
        (system_id, activity),
    ).fetchone()
    return row[0] if row else None


def _eiv_per_run(blueprint_type_id: int, materials_table: str, id_column: str, conn) -> float | None:
    rows = conn.execute(
        f"SELECT material_type_id, quantity FROM {materials_table} WHERE {id_column} = ?",
        (blueprint_type_id,),
    ).fetchall()
    if not rows:
        return None
    eiv = 0.0
    for material_type_id, qty in rows:
        price_row = conn.execute(
            "SELECT adjusted_price FROM adjusted_prices WHERE type_id = ?", (material_type_id,)
        ).fetchone()
        if price_row is None:
            return None
        eiv += qty * price_row[0]
    return eiv


def manufacturing_job_cost(
    blueprint_type_id: int, runs: int, system_id: int, facility_tax_pct: float, db_path: Path = DB_PATH
) -> float | None:
    """Job installation fee to manufacture `runs` runs of this blueprint at
    `system_id`, or None if the needed adjusted-price/cost-index data isn't
    cached yet (call refresh_industry_data() first)."""
    conn = db.connect(db_path)
    try:
        _ensure_tables(conn)
        eiv = _eiv_per_run(blueprint_type_id, "blueprint_materials", "blueprint_type_id", conn)
        cost_index = _cost_index(system_id, "manufacturing", conn)
        if eiv is None or cost_index is None:
            return None
        return eiv * runs * (cost_index + facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT / 100.0)
    finally:
        conn.close()


def reaction_job_cost(
    formula_type_id: int, runs: int, system_id: int, facility_tax_pct: float, db_path: Path = DB_PATH
) -> float | None:
    conn = db.connect(db_path)
    try:
        _ensure_tables(conn)
        eiv = _eiv_per_run(formula_type_id, "reaction_materials", "formula_type_id", conn)
        cost_index = _cost_index(system_id, "reaction", conn)
        if eiv is None or cost_index is None:
            return None
        return eiv * runs * (cost_index + facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT / 100.0)
    finally:
        conn.close()


def copy_job_cost(
    blueprint_type_id: int, runs: int, system_id: int, facility_tax_pct: float, db_path: Path = DB_PATH
) -> float | None:
    """Cost to make a single BPC with `runs` runs of this (Manufacturing)
    blueprint. Lower confidence than manufacturing_job_cost -- see module
    docstring. facility_tax_pct is accepted for a consistent call signature
    but the documented formula for copying doesn't include a tax term."""
    conn = db.connect(db_path)
    try:
        _ensure_tables(conn)
        eiv = _eiv_per_run(blueprint_type_id, "blueprint_materials", "blueprint_type_id", conn)
        cost_index = _cost_index(system_id, "copying", conn)
        if eiv is None or cost_index is None:
            return None
        return cost_index * runs * COPY_COST_FACTOR * eiv
    finally:
        conn.close()


@dataclass
class ScheduledJob:
    label: str
    activity: str  # "manufacturing" or "reaction"
    seconds: float


@dataclass
class BuildTimeEstimate:
    reaction_phase_seconds: float
    manufacturing_phase_seconds: float
    total_seconds: float
    jobs: list[ScheduledJob] = field(default_factory=list)
    missing_time_data: list[str] = field(default_factory=list)


def _job_time_seconds(
    blueprint_type_id: int, activity_id: int, runs: int, time_reduction_pct: float, conn
) -> float | None:
    row = conn.execute(
        "SELECT time_seconds FROM activity_times WHERE type_id = ? AND activity_id = ?",
        (blueprint_type_id, activity_id),
    ).fetchone()
    if row is None:
        return None
    return row[0] * runs * (1 - time_reduction_pct / 100.0)


def _schedule(jobs: list[tuple[str, float]], slots: int) -> float:
    """Greedy longest-processing-time-first bin packing across `slots`
    parallel job slots. Returns the makespan (seconds until the last job
    finishes) -- a well-known good approximation for this NP-hard scheduling
    problem, exact for the small job counts this tool deals with."""
    if not jobs:
        return 0.0
    slots = max(1, slots)
    loads = [0.0] * slots
    for _label, duration in sorted(jobs, key=lambda j: -j[1]):
        idx = min(range(slots), key=lambda i: loads[i])
        loads[idx] += duration
    return max(loads)


def estimate_build_time(
    top_blueprint_type_id: int,
    top_runs: int,
    build_decisions: list,
    manufacturing_slots: int,
    reaction_slots: int,
    time_efficiency_pct: float,
    db_path: Path = DB_PATH,
) -> BuildTimeEstimate:
    """Wall-clock estimate to run every job this purchase plan implies:
    the top-level blueprint, plus one job per item production_chain decided
    to build. Manufacturing and reaction jobs are scheduled independently
    across their own slot pools (they're separate skills/slot types in EVE),
    each via greedy bin-packing. Reactions must finish before a component
    that needs them can start, but this tool doesn't track *which* component
    depends on *which* specific reaction -- so it conservatively assumes the
    whole reaction phase completes before the manufacturing phase starts
    (reaction_phase + manufacturing_phase), which can overestimate total time
    but never underestimates it.

    `time_efficiency_pct` (0-20%, matching in-game TE research) is applied to
    manufacturing jobs only -- reaction formulas have no TE research, only a
    refinery duration rig bonus, which isn't modeled here.
    """
    conn = db.connect(db_path)
    try:
        manufacturing_jobs: list[tuple[str, float]] = []
        reaction_jobs: list[tuple[str, float]] = []
        missing: list[str] = []

        top_time = _job_time_seconds(top_blueprint_type_id, MANUFACTURING_ACTIVITY_ID, top_runs, time_efficiency_pct, conn)
        if top_time is None:
            missing.append("(top-level blueprint)")
        else:
            manufacturing_jobs.append(("(top-level blueprint)", top_time))

        for d in build_decisions:
            if d.decision != "build":
                continue
            producer = production_chain.get_producer(d.type_id, db_path)
            if producer is None:
                missing.append(d.name)
                continue
            is_manufacturing = producer["activity_id"] == MANUFACTURING_ACTIVITY_ID
            reduction = time_efficiency_pct if is_manufacturing else 0.0
            t = _job_time_seconds(producer["blueprint_type_id"], producer["activity_id"], d.runs, reduction, conn)
            if t is None:
                missing.append(d.name)
                continue
            (manufacturing_jobs if is_manufacturing else reaction_jobs).append((d.name, t))

        reaction_makespan = _schedule(reaction_jobs, reaction_slots)
        manufacturing_makespan = _schedule(manufacturing_jobs, manufacturing_slots)

        jobs = [ScheduledJob(label, "reaction", secs) for label, secs in reaction_jobs] + [
            ScheduledJob(label, "manufacturing", secs) for label, secs in manufacturing_jobs
        ]

        return BuildTimeEstimate(
            reaction_phase_seconds=reaction_makespan,
            manufacturing_phase_seconds=manufacturing_makespan,
            total_seconds=reaction_makespan + manufacturing_makespan,
            jobs=jobs,
            missing_time_data=missing,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    n_prices, n_indices, n_facilities = refresh_industry_data()
    print(
        f"Refreshed {n_prices} adjusted prices, {n_indices} system cost index rows, "
        f"{n_facilities} systems with a public industry facility."
    )
