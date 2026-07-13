"""Solve for the cheapest way to fill a blueprint's material list.

Prices come from order_book.py's real per-order ESI data, not a single
blended aggregate -- every candidate is priced in depth-capped tiers (see
PriceSource below), so a plan can never assume more of something is
available at a given price than actually is.

Required materials fall into two buckets, handled differently:
  - The 8 standard minerals (Tritanium..Morphite) are ore-derived: solve a
    MILP for the cheapest combination of ore (across cached highsec regions)
    that reprocesses into enough of each, given a refine % per ore tier.
    This has to be integer (whole reprocessing batches, e.g. 100-unit
    portions), not a continuous LP -- rounding a fractional continuous
    solution up to the nearest batch after the fact can massively overshoot
    cost when a candidate's fractional share was small but its batch size
    was large, and it makes a good-looking candidate a trap the solver keeps
    picking blind to that trap. Each ore's price tiers are their own MILP
    candidates too, so the solver can spend a thin order book's cheap end
    and spill into the next tier or a different ore/grade entirely.
  - Everything else (Tech II components, PI materials, reaction intermediates,
    salvage, ...) has its own multi-step build chain that may or may not beat
    buying the finished item outright. Rather than modeling that whole tree,
    these are just priced via a greedy cheapest-tier-first walk and added to
    the total directly -- "buy the component."
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scipy.optimize import Bounds, LinearConstraint, milp

from indycalc import blueprint_calc, db, order_book, price_cache, production_chain
from indycalc.order_book import PriceTier
from indycalc.price_cache import (
    MINERAL_TYPE_IDS,
    best_prices,
    best_station_prices,
    get_or_fetch_prices,
    get_or_fetch_station_prices,
)
from indycalc.production_chain import BuildDecision
from indycalc.sde_loader import DB_PATH

REPROCESS_BATCH_FALLBACK = 100

# "any": let the LP pick whichever is cheapest, raw or compressed.
# "compressed": only consider compressed ore -- ~100x less volume per unit of
#   yield than raw, worth it even at a small ISK premium if you're hauling.
# "uncompressed": only raw ore -- e.g. you're compressing it yourself at a
#   refinery after hauling it in bulk, so the market compression premium buys
#   you nothing.
ORE_MODES = ("any", "compressed", "uncompressed")


@dataclass
class OrePurchase:
    type_id: int
    type_name: str
    tier: str
    quantity: int
    unit_price: float
    location_name: str  # a region name, or a station name when buying from a single station
    cost: float
    volume_m3: float


@dataclass
class ComponentPurchase:
    type_id: int
    type_name: str
    quantity: int
    unit_price: float
    location_name: str
    cost: float
    volume_m3: float


@dataclass
class PriceSource:
    """Abstracts "where do real order-book depth comes from" so
    _optimize_ore/_price_components don't need to know whether they're
    pricing across a region, a single station, or several pooled stations --
    same candidate-generation either way, different lookup.

    Real per-order data (order_book.py) comes from ESI, which is region-
    scoped and expensive enough per (region, type) that it isn't worth
    calling for every ore variant a blueprint could theoretically use --
    most have no real liquidity at all. `prefilter_bulk`/`prefilter_any` are
    price_cache.py's cheap, already-batched Fuzzworks aggregate lookups,
    used only to narrow the candidate set down to items worth a real fetch;
    the aggregate price itself is never used as a cost.

    `location_filter=None` means region-wide (any station/structure in
    `region_ids` counts, shown under the uniform `label`); a non-None set
    restricts to those specific locations (single- or multi-station modes),
    each displayed via `location_names` (falls back to `label` if unknown).
    """
    label: str
    region_ids: list[int]
    location_filter: set[int] | None
    prefilter_bulk: Callable[[list[int]], dict[int, dict]]
    prefilter_any: Callable[[list[int]], dict[int, dict]]
    location_names: dict[int, str] = field(default_factory=dict)

    def location_name(self, location_id: int) -> str:
        return self.location_names.get(location_id, self.label)

    def bulk_tiers(self, type_ids: list[int]) -> dict[int, list[PriceTier]]:
        allowed = self.prefilter_bulk(type_ids)
        candidate_ids = [tid for tid in type_ids if tid in allowed]
        if not candidate_ids:
            return {}
        return order_book.get_tiers(candidate_ids, self.region_ids, self.location_filter)

    def any_tiers(self, type_ids: list[int]) -> dict[int, list[PriceTier]]:
        allowed = self.prefilter_any(type_ids)
        candidate_ids = [tid for tid in type_ids if tid in allowed]
        if not candidate_ids:
            return {}
        return order_book.get_tiers(candidate_ids, self.region_ids, self.location_filter)


def region_price_source(db_path: Path, region_names: list[str] | None) -> PriceSource:
    label = region_names[0] if region_names else "Cheapest overall"
    return PriceSource(
        label=label,
        region_ids=price_cache.region_ids_for_names(region_names, db_path),
        location_filter=None,
        prefilter_bulk=lambda ids: best_prices(ids, db_path, region_names=region_names),
        prefilter_any=lambda ids: get_or_fetch_prices(ids, db_path, region_names=region_names),
    )


def station_price_source(db_path: Path, station_id: int, station_label: str) -> PriceSource:
    region_id = price_cache.station_region_id(station_id, db_path)
    return PriceSource(
        label=station_label,
        region_ids=[region_id] if region_id is not None else [],
        location_filter={station_id},
        prefilter_bulk=lambda ids: best_station_prices(ids, station_id, db_path),
        prefilter_any=lambda ids: get_or_fetch_station_prices(ids, station_id, db_path),
        location_names={station_id: station_label},
    )


def multi_station_price_source(db_path: Path, station_ids: list[int]) -> PriceSource:
    """Pools several stations -- each item is priced at whichever station in
    the list is cheapest for it, tagged with that specific station's name."""
    station_names = {sid: name for name, sid in price_cache.HUB_STATION_IDS.items() if sid in station_ids}
    region_ids = [
        rid
        for rid in (price_cache.station_region_id(sid, db_path) for sid in station_ids)
        if rid is not None
    ]
    return PriceSource(
        label="",  # unused: every tier is tagged with a specific station via location_names
        region_ids=region_ids,
        location_filter=set(station_ids),
        prefilter_bulk=lambda ids: price_cache.best_multi_station_prices(ids, station_ids, db_path),
        prefilter_any=lambda ids: price_cache.get_or_fetch_multi_station_prices(ids, station_ids, db_path),
        location_names=station_names,
    )


@dataclass
class OptimizationResult:
    purchases: list[OrePurchase]
    total_cost: float
    required: dict[int, int]
    produced: dict[int, float]
    waste_qty: dict[int, float]
    waste_isk: dict[int, float]
    total_waste_isk: float
    infeasible_reason: str | None = None
    component_purchases: list[ComponentPurchase] = field(default_factory=list)
    total_volume_m3: float = 0.0
    build_decisions: list[BuildDecision] = field(default_factory=list)


def _empty_result(required: dict[int, int], reason: str) -> OptimizationResult:
    return OptimizationResult([], 0.0, required, {}, {}, {}, 0.0, reason)


def _load_relevant_ores(
    required_mineral_ids: set[int], db_path: Path, ore_mode: str
) -> list[tuple[int, str, str, int, float]]:
    """Ores that yield at least one required mineral. Returns
    [(type_id, type_name, tier, portion_size, volume), ...]."""
    conn = db.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in required_mineral_ids)
        compressed_filter = ""
        if ore_mode == "compressed":
            compressed_filter = "AND ot.is_compressed = 1"
        elif ore_mode == "uncompressed":
            compressed_filter = "AND ot.is_compressed = 0"
        rows = conn.execute(
            f"""
            SELECT DISTINCT ot.type_id, ot.type_name, ot.tier, ot.portion_size, ot.volume
            FROM ore_tiers ot
            JOIN reprocess_materials rm ON rm.ore_type_id = ot.type_id
            WHERE rm.material_type_id IN ({placeholders})
            {compressed_filter}
            """,
            list(required_mineral_ids),
        ).fetchall()
        return [(int(r[0]), r[1], r[2], int(r[3]) or REPROCESS_BATCH_FALLBACK, float(r[4])) for r in rows]
    finally:
        conn.close()


def _load_yields(ore_type_ids: list[int], db_path: Path) -> dict[int, dict[int, float]]:
    """{ore_type_id: {material_type_id: base_quantity_per_portion}}"""
    conn = db.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in ore_type_ids)
        rows = conn.execute(
            f"SELECT ore_type_id, material_type_id, quantity FROM reprocess_materials "
            f"WHERE ore_type_id IN ({placeholders})",
            ore_type_ids,
        ).fetchall()
        yields: dict[int, dict[int, float]] = {}
        for ore_id, mat_id, qty in rows:
            yields.setdefault(ore_id, {})[mat_id] = qty
        return yields
    finally:
        conn.close()


# Real order-book depth-fetching is the expensive step (an ESI call per
# (region, type) not yet cached), and eagerly fetching it for every ore
# variant that could theoretically supply a required mineral wastes most of
# that cost -- the MILP only ever ends up using a handful of the cheapest
# ones. Instead, candidates are ranked per-mineral by the already-cached
# aggregate price (no ESI call) and deep-fetched cheapest-first, in small
# batches, stopping once every mineral has confirmed real depth comfortably
# past what it needs. DEPTH_SAFETY_MARGIN is the buffer above the raw
# requirement (depth toward one mineral doesn't help another, and portion-
# size/batch rounding eats a little more) before a mineral is considered
# "enough" and stops pulling in new candidates.
DEPTH_SAFETY_MARGIN = 1.3
MAX_EXPANSION_ROUNDS = 6
CANDIDATES_PER_ROUND = 6  # new candidates considered per still-short mineral, per round


@dataclass
class _OreCandidate:
    type_id: int
    name: str
    tier: str
    portion_size: int
    unit_volume: float
    yield_row: dict[int, float]  # mineral_id -> yield per single ore/mineral unit, refine-adjusted


def _build_virtual_candidates(
    ores: list[tuple[int, str, str, int, float]],
    base_yields: dict[int, dict[int, float]],
    refine_pct: dict[str, float],
    mineral_ids: list[int],
    allow_direct_minerals: bool,
    db_path: Path,
) -> dict[int, _OreCandidate]:
    """Every ore variant, plus (if allowed) a direct-buy pseudo-candidate per
    mineral, in one pool keyed by type_id -- letting ranking/expansion/the
    final MILP treat "buy the mineral directly" exactly like any other
    candidate instead of special-casing it. A rare mineral (e.g. Morphite)
    may only come from an expensive/thin ore (Mercoxit): reprocessing a
    whole 100-unit portion just to cover a required qty of 1 can cost far
    more than buying that 1 unit directly, so this lets the solver mix
    strategies -- bulk ore for the big minerals, direct buy for a small
    leftover amount of a rare one."""
    candidates: dict[int, _OreCandidate] = {}
    for type_id, name, tier, portion_size, unit_volume in ores:
        pct = refine_pct.get(tier, 0.0) / 100.0
        yield_row = {
            mid: (base_yields.get(type_id, {}).get(mid, 0.0) / portion_size) * pct
            for mid in mineral_ids
        }
        candidates[type_id] = _OreCandidate(type_id, name, tier, portion_size, unit_volume, yield_row)

    if allow_direct_minerals:
        mineral_names = blueprint_calc.material_names(mineral_ids, db_path)
        mineral_volumes = blueprint_calc.material_volumes(mineral_ids, db_path)
        for mid in mineral_ids:
            candidates[mid] = _OreCandidate(
                mid, mineral_names.get(mid, str(mid)), "Direct", 1,
                mineral_volumes.get(mid, 0.0), {mid: 1.0},
            )
    return candidates


def _rank_candidates_per_mineral(
    virtual_candidates: dict[int, _OreCandidate], surface_prices: dict[int, dict], mineral_ids: list[int]
) -> dict[int, list[int]]:
    """Cheapest-estimated-cost-per-unit-of-mineral first, per mineral, using
    only the cheap cached aggregate price (never a real order-book call)."""
    ranked: dict[int, list[int]] = {}
    for mineral_id in mineral_ids:
        scored = []
        for type_id, cand in virtual_candidates.items():
            y = cand.yield_row.get(mineral_id, 0.0)
            if y <= 0:
                continue
            price_info = surface_prices.get(type_id)
            if price_info is None:
                continue
            scored.append((price_info["price"] / y, type_id))
        scored.sort(key=lambda t: t[0])
        ranked[mineral_id] = [tid for _, tid in scored]
    return ranked


# Row: (type_id, name, tier, portion_size, unit_price, yield_row, unit_volume, ub_batches, location_name)
def _build_milp_candidates(
    deep_fetched: set[int],
    virtual_candidates: dict[int, _OreCandidate],
    tiers_by_candidate: dict[int, list[PriceTier]],
    price_source: PriceSource,
) -> tuple[list[tuple], int]:
    """One MILP candidate row per *price tier* of a deep-fetched ore/mineral,
    not one per ore/mineral -- a thin order book (e.g. a single 26k-unit
    sell order) can't fill an arbitrarily large purchase at that one price,
    so each tier gets its own row capped to that tier's actual depth (in
    whole batches). This lets the solver spend the cheap end of one ore's
    book, spill into its next tier or a *different* ore/grade for the
    remainder -- exactly the "Scordite II- vs III-Grade" substitution a
    single blended price per candidate could never discover."""
    rows: list[tuple] = []
    fetched_with_tiers = 0
    for type_id in deep_fetched:
        cand = virtual_candidates[type_id]
        tiers = tiers_by_candidate.get(type_id) or []
        if not tiers:
            continue
        fetched_with_tiers += 1
        for price_tier in tiers:
            ub_batches = price_tier.max_qty // cand.portion_size
            if ub_batches < 1:
                continue
            rows.append(
                (
                    cand.type_id, cand.name, cand.tier, cand.portion_size, price_tier.price,
                    cand.yield_row, cand.unit_volume, ub_batches,
                    price_source.location_name(price_tier.location_id),
                )
            )
    return rows, fetched_with_tiers


def _solve_ore_milp(candidates: list[tuple], mineral_ids: list[int], mineral_required: dict[int, int]):
    # Decision variable per candidate is an integer *batch count* (one batch =
    # one portion_size worth), not a continuous unit quantity -- see module
    # docstring for why. cost/yield are expressed per batch accordingly, and
    # each variable's upper bound is that tier's own depth in whole batches.
    n = len(candidates)
    m = len(mineral_ids)
    cost_per_batch = [cand[4] * cand[3] for cand in candidates]  # unit_price * portion_size
    yield_per_batch = [
        [candidates[j][5].get(mineral_ids[i], 0.0) * candidates[j][3] for j in range(n)]
        for i in range(m)
    ]
    required_vec = [mineral_required[mid] for mid in mineral_ids]
    ub_vec = [cand[7] for cand in candidates]
    constraints = LinearConstraint(yield_per_batch, lb=required_vec, ub=math.inf)
    return milp(c=cost_per_batch, constraints=constraints, integrality=[1] * n, bounds=Bounds(lb=0, ub=ub_vec))


def _optimize_ore(
    mineral_required: dict[int, int],
    refine_pct: dict[str, float],
    db_path: Path,
    price_source: PriceSource,
    ore_mode: str,
    allow_direct_minerals: bool,
):
    """Returns (purchases, total_cost, produced, infeasible_reason). See the
    module-level constants above for the surface-price-guided deep-fetch
    strategy this uses instead of eagerly fetching every candidate."""
    mineral_ids = sorted(mid for mid, qty in mineral_required.items() if qty > 0)
    if not mineral_ids:
        return [], 0.0, {}, None

    ores = _load_relevant_ores(set(mineral_ids), db_path, ore_mode)
    if not ores:
        mode_note = "" if ore_mode == "any" else f" ({ore_mode} only)"
        return [], 0.0, {}, f"No ore{mode_note} yields any required mineral."

    ore_type_ids = [o[0] for o in ores]
    base_yields = _load_yields(ore_type_ids, db_path)
    virtual_candidates = _build_virtual_candidates(
        ores, base_yields, refine_pct, mineral_ids, allow_direct_minerals, db_path
    )

    surface_prices = price_source.prefilter_bulk(list(virtual_candidates))
    ranked_by_mineral = _rank_candidates_per_mineral(virtual_candidates, surface_prices, mineral_ids)

    deep_fetched: set[int] = set()
    tiers_by_candidate: dict[int, list[PriceTier]] = {}
    confirmed_depth: dict[int, float] = {mid: 0.0 for mid in mineral_ids}

    def deep_fetch(type_ids) -> None:
        fetched = price_source.bulk_tiers(list(type_ids))
        for type_id in type_ids:
            deep_fetched.add(type_id)
            tiers = fetched.get(type_id) or []
            tiers_by_candidate[type_id] = tiers
            total_units = sum(t.max_qty for t in tiers)
            for mineral_id, y in virtual_candidates[type_id].yield_row.items():
                if y > 0:
                    confirmed_depth[mineral_id] = confirmed_depth.get(mineral_id, 0.0) + total_units * y

    def shortfall(mineral_id: int) -> float:
        return mineral_required[mineral_id] * DEPTH_SAFETY_MARGIN - confirmed_depth.get(mineral_id, 0.0)

    for _round in range(MAX_EXPANSION_ROUNDS):
        short = [mid for mid in mineral_ids if shortfall(mid) > 0]
        if not short:
            break
        batch: set[int] = set()
        for mineral_id in short:
            picked = 0
            for type_id in ranked_by_mineral.get(mineral_id, []):
                if type_id in deep_fetched or type_id in batch:
                    continue
                batch.add(type_id)
                picked += 1
                if picked >= CANDIDATES_PER_ROUND:
                    break
        if not batch:
            break  # nothing left to try for any still-short mineral
        deep_fetch(batch)

    candidates, fetched_with_tiers = _build_milp_candidates(
        deep_fetched, virtual_candidates, tiers_by_candidate, price_source
    )
    if not candidates:
        return [], 0.0, {}, f"No real sell orders found for any candidate ore or mineral in {price_source.label} -- click Refresh Prices first."

    res = _solve_ore_milp(candidates, mineral_ids, mineral_required)

    if not res.success and len(deep_fetched) < len(virtual_candidates):
        # The greedy margin didn't actually combine into a feasible batch
        # allocation (can happen with awkward portion sizes/refine %, or a
        # genuine shortage that only shows up once every option is on the
        # table) -- fall back once to fetching everything remaining rather
        # than reporting infeasible off an incomplete candidate set.
        remaining = [tid for tid in virtual_candidates if tid not in deep_fetched]
        deep_fetch(remaining)
        candidates, fetched_with_tiers = _build_milp_candidates(
            deep_fetched, virtual_candidates, tiers_by_candidate, price_source
        )
        res = _solve_ore_milp(candidates, mineral_ids, mineral_required)

    if not res.success:
        missing = len(deep_fetched) - fetched_with_tiers
        reason = (
            "No feasible combination of ore/direct mineral purchases covers all "
            "required minerals with the real sell-order depth available and the "
            "given refine % -- try lowering refine % requirements, allowing more "
            "stations, or refreshing prices."
        )
        if missing:
            reason += f" ({missing} candidate ore(s) had no real sell orders and were excluded.)"
        return [], 0.0, {}, reason

    produced: dict[int, float] = {mid: 0.0 for mid in mineral_ids}
    total_cost = 0.0
    # Multiple tiers of the same (type_id, location, price) can each get a
    # nonzero batch count -- merge those into one purchase line rather than
    # showing every tier separately when they land at the same price; a
    # different price at the same location, or the same ore at a different
    # location, still shows as its own line (see app.py, per the plan: no
    # blending across genuinely different prices).
    merged: dict[tuple, OrePurchase] = {}
    for j, (type_id, name, tier, portion_size, unit_price, yield_row, unit_volume, ub_batches, location_name) in enumerate(candidates):
        batches = round(res.x[j])
        if batches < 1:
            continue
        qty = batches * portion_size
        cost = qty * unit_price
        total_cost += cost
        key = (type_id, location_name, unit_price)
        if key in merged:
            existing = merged[key]
            existing.quantity += qty
            existing.cost += cost
            existing.volume_m3 += qty * unit_volume
        else:
            merged[key] = OrePurchase(
                type_id=type_id,
                type_name=name,
                tier=tier,
                quantity=qty,
                unit_price=unit_price,
                location_name=location_name,
                cost=cost,
                volume_m3=qty * unit_volume,
            )
        for mat_id, per_unit in yield_row.items():
            produced[mat_id] = produced.get(mat_id, 0.0) + qty * per_unit

    purchases = sorted(merged.values(), key=lambda p: -p.cost)
    return purchases, total_cost, produced, None


def _price_components(
    component_required: dict[int, int],
    db_path: Path,
    price_source: PriceSource,
) -> tuple[list[ComponentPurchase], dict[int, int]]:
    """Components aren't batch-reprocessed, so unlike ore they don't compete
    against each other for the same mineral requirement -- no MILP needed,
    just a greedy walk that spends each item's cheapest tier first and
    spills into the next until the required quantity is covered (or tiers
    run out, which makes that item -- and the whole location -- infeasible,
    same as an uncoverable mineral)."""
    component_ids = [tid for tid, qty in component_required.items() if qty > 0]
    if not component_ids:
        return [], {}

    tiers_by_type = price_source.any_tiers(component_ids)
    names = blueprint_calc.material_names(component_ids, db_path)
    volumes = blueprint_calc.material_volumes(component_ids, db_path)

    merged: dict[tuple, ComponentPurchase] = {}
    unpriced: dict[int, int] = {}
    for type_id in component_ids:
        remaining = component_required[type_id]
        rows_this_type: list[tuple] = []
        for price_tier in tiers_by_type.get(type_id) or []:
            if remaining <= 0:
                break
            take = min(remaining, price_tier.max_qty)
            if take <= 0:
                continue
            remaining -= take
            rows_this_type.append((take, price_tier))
        if remaining > 0:
            unpriced[type_id] = component_required[type_id]
            continue
        for take, price_tier in rows_this_type:
            location_name = price_source.location_name(price_tier.location_id)
            cost = take * price_tier.price
            key = (type_id, location_name, price_tier.price)
            if key in merged:
                existing = merged[key]
                existing.quantity += take
                existing.cost += cost
                existing.volume_m3 += take * volumes.get(type_id, 0.0)
            else:
                merged[key] = ComponentPurchase(
                    type_id=type_id,
                    type_name=names.get(type_id, str(type_id)),
                    quantity=take,
                    unit_price=price_tier.price,
                    location_name=location_name,
                    cost=cost,
                    volume_m3=take * volumes.get(type_id, 0.0),
                )
    return sorted(merged.values(), key=lambda p: -p.cost), unpriced


def optimize(
    required: dict[int, int],
    refine_pct: dict[str, float],
    db_path: Path = DB_PATH,
    region_names: list[str] | None = None,
    station_id: int | None = None,
    station_label: str | None = None,
    station_ids: list[int] | None = None,
    ore_mode: str = "any",
    allow_direct_minerals: bool = True,
    allow_build_components: bool = False,
    allow_build_reactions: bool = False,
    component_me_percent: float = 0.0,
    reaction_reduction_percent: float = 0.0,
    pre_expanded: production_chain.ExpansionResult | None = None,
) -> OptimizationResult:
    """Solve for the cheapest way to fill `required` (blueprint materials at
    the chosen ME/runs).

    By default (region_names=None, station_id=None, station_ids=None) prices
    are pulled from the cheapest across *all* cached highsec regions -- the
    absolute cost minimum, but purchases may be scattered across many
    stations/regions. Pass a single trade hub's region (e.g. ["The Forge"]
    for Jita) to restrict the plan to one region, trading a bit of ISK for
    the convenience of fewer stops. Pass `station_id` (+ `station_label` for
    display) instead to price *everything* from one specific station --
    genuinely one stop, not just one region -- which will be infeasible
    unless every required item is actually liquid there. Pass `station_ids`
    (a list) to pool several stations -- each item is priced at whichever of
    those stations is cheapest for it (see best_station_combo() for
    searching which N-station combination is cheapest overall).

    `ore_mode` controls raw vs. compressed ore sourcing -- see ORE_MODES.
    `allow_direct_minerals=False` forces every mineral to come from ore
    reprocessing, never bought directly on the market.
    `allow_build_components`/`allow_build_reactions` let non-mineral
    requirements be built from their own materials instead of always bought
    directly, when that's cheaper -- see production_chain.py for the (capped
    at two levels) recursion this expands. That expansion's build-vs-buy
    decision already uses a region-independent price estimate (see its
    docstring), so its result doesn't depend on region_names/station_id/
    station_ids at all -- callers comparing several locations for the same
    blueprint should compute it once (production_chain.expand_requirements)
    and pass it in as `pre_expanded` instead of letting every call redo the
    same expansion (each of which fetches component prices too).
    """
    if ore_mode not in ORE_MODES:
        raise ValueError(f"ore_mode must be one of {ORE_MODES}, got {ore_mode!r}")

    if station_ids is not None:
        price_source = multi_station_price_source(db_path, station_ids)
    elif station_id is not None:
        price_source = station_price_source(db_path, station_id, station_label or str(station_id))
    else:
        price_source = region_price_source(db_path, region_names)

    build_decisions: list[BuildDecision] = []
    if pre_expanded is not None:
        required = pre_expanded.expanded_required
        build_decisions = pre_expanded.build_decisions
    elif allow_build_components or allow_build_reactions:
        expansion = production_chain.expand_requirements(
            required,
            allow_build_components,
            allow_build_reactions,
            component_me_percent,
            reaction_reduction_percent,
            db_path,
        )
        required = expansion.expanded_required
        build_decisions = expansion.build_decisions

    mineral_required = {tid: qty for tid, qty in required.items() if tid in MINERAL_TYPE_IDS}
    component_required = {tid: qty for tid, qty in required.items() if tid not in MINERAL_TYPE_IDS}

    ore_purchases, ore_cost, produced, infeasible_reason = _optimize_ore(
        mineral_required, refine_pct, db_path, price_source, ore_mode, allow_direct_minerals
    )
    if infeasible_reason:
        return _empty_result(required, infeasible_reason)

    component_purchases, unpriced_components = _price_components(
        component_required, db_path, price_source
    )
    if unpriced_components:
        # A component with no price at this location was previously just
        # dropped from the total -- silently treating "can't buy this here"
        # as "costs 0 here," which made locations that are actually missing
        # items look artificially cheap (even "the best place to buy
        # everything" when they didn't sell everything). Missing coverage
        # has to make the whole location infeasible, the same way an
        # uncoverable mineral already does via the MILP.
        names = blueprint_calc.material_names(list(unpriced_components.keys()), db_path)
        missing = ", ".join(names.get(tid, str(tid)) for tid in unpriced_components)
        return _empty_result(
            required, f"No cached price in {price_source.label} for: {missing} -- click Refresh Prices first."
        )
    component_cost = sum(p.cost for p in component_purchases)

    mineral_ids = sorted(mineral_required)
    waste_qty = {
        mid: max(0.0, produced.get(mid, 0.0) - mineral_required[mid]) for mid in mineral_ids
    }
    mineral_prices = best_prices(mineral_ids, db_path) if mineral_ids else {}
    waste_isk = {
        mid: waste_qty[mid] * mineral_prices.get(mid, {}).get("price", 0.0)
        for mid in mineral_ids
    }
    total_volume_m3 = sum(p.volume_m3 for p in ore_purchases) + sum(
        p.volume_m3 for p in component_purchases
    )

    return OptimizationResult(
        purchases=ore_purchases,
        total_cost=ore_cost + component_cost,
        required=required,
        produced=produced,
        waste_qty=waste_qty,
        waste_isk=waste_isk,
        total_waste_isk=sum(waste_isk.values()),
        infeasible_reason=None,
        component_purchases=component_purchases,
        total_volume_m3=total_volume_m3,
        build_decisions=build_decisions,
    )


def optimize_many(job_kwargs_list: list[dict]) -> list[OptimizationResult]:
    """Run several independent optimize() calls and return results in the
    same order as the input list. Used for comparing many buy locations at
    once (e.g. the region+station check for every trade hub, or every
    candidate combination in best_station_combo).

    Deliberately sequential, not threaded. scipy's HiGHS MILP solver has
    documented segfaults/assertion failures tied to its own internal
    threading (scipy issues #17220, #17250, #22188), so calling milp() from
    several Python threads concurrently risks a native-level crash that
    takes down the whole process with no Python traceback -- confirmed this
    session as the actual cause of the app dying under exactly this kind of
    concurrent load. Not worth the speedup; st.cache_data (see app.py) is
    what actually keeps this from being slow on repeat renders.
    """
    return [optimize(**kwargs) for kwargs in job_kwargs_list]


@dataclass
class StationComboResult:
    station_names: tuple[str, ...]
    result: OptimizationResult


def best_station_combo(
    required: dict[int, int],
    refine_pct: dict[str, float],
    max_stations: int,
    db_path: Path = DB_PATH,
    **optimize_kwargs,
) -> tuple[StationComboResult | None, list[StationComboResult]]:
    """Try every combination of up to `max_stations` of the 5 known hub
    stations (small enough to brute-force: at most C(5,1)+...+C(5,5) = 31
    combos, run in parallel via optimize_many) and return the cheapest
    feasible one, plus every combo tried (for a "here's what we checked"
    comparison table).

    Only the 5 major hub stations are candidates -- this tool doesn't have
    station-level price data for anywhere else, and realistically those are
    the only stations liquid enough for bulk purchases anyway. (An earlier
    version of this tool could expand the search to stations discovered
    near a hub, or across all of highsec -- measured taking long enough
    that it wasn't practical to even time, so it was dropped rather than
    kept as a "use at your own risk" option.)
    """
    hub_names = list(price_cache.HUB_STATION_IDS)
    combos = [
        combo for k in range(1, max_stations + 1) for combo in itertools.combinations(hub_names, k)
    ]
    job_kwargs_list = [
        dict(
            required=required,
            refine_pct=refine_pct,
            db_path=db_path,
            station_ids=[price_cache.HUB_STATION_IDS[name] for name in combo],
            **optimize_kwargs,
        )
        for combo in combos
    ]
    results = optimize_many(job_kwargs_list)

    all_results = [StationComboResult(combo, r) for combo, r in zip(combos, results)]
    best: StationComboResult | None = None
    for entry in all_results:
        if entry.result.infeasible_reason:
            continue
        if best is None or entry.result.total_cost < best.result.total_cost:
            best = entry

    return best, all_results
