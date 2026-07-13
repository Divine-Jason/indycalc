"""Solve for the cheapest way to fill a blueprint's material list.

Required materials fall into two buckets, handled differently:
  - The 8 standard minerals (Tritanium..Morphite) are ore-derived: solve a
    MILP for the cheapest combination of ore (across cached highsec regions)
    that reprocesses into enough of each, given a refine % per ore tier.
    This has to be integer (whole reprocessing batches, e.g. 100-unit
    portions), not a continuous LP -- rounding a fractional continuous
    solution up to the nearest batch after the fact can massively overshoot
    cost when a candidate's fractional share was small but its batch size
    was large, and it makes a good-looking candidate a trap the solver keeps
    picking blind to that trap.
  - Everything else (Tech II components, PI materials, reaction intermediates,
    salvage, ...) has its own multi-step build chain that may or may not beat
    buying the finished item outright. Rather than modeling that whole tree,
    these are just priced at the cheapest cached market sell price and added
    to the total directly -- "buy the component."
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scipy.optimize import Bounds, LinearConstraint, milp

from indycalc import blueprint_calc, db, price_cache, production_chain
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
    """Abstracts "where do prices come from" so _optimize_ore/_price_components
    don't need to know whether they're pricing across a region, a single
    station, or several pooled stations -- same MILP either way, different
    lookup. `label` is the uniform location shown for single-location
    sources; a pooled multi-station source instead returns a per-item
    "location_name" (which specific station in the pool was cheapest for
    that item), which takes priority over `label` when present."""
    label: str
    bulk: Callable[[list[int]], dict[int, dict]]  # ore/mineral type_ids, pre-cached
    any: Callable[[list[int]], dict[int, dict]]  # arbitrary type_ids, fetches on demand

    def bulk_prices(self, type_ids: list[int]) -> dict[int, dict]:
        return {
            tid: {"price": info["price"], "location_name": info.get("location_name", self.label)}
            for tid, info in self.bulk(type_ids).items()
        }

    def any_prices(self, type_ids: list[int]) -> dict[int, dict]:
        return {
            tid: {"price": info["price"], "location_name": info.get("location_name", self.label)}
            for tid, info in self.any(type_ids).items()
        }


def region_price_source(db_path: Path, region_names: list[str] | None) -> PriceSource:
    label = region_names[0] if region_names else "Cheapest overall"
    return PriceSource(
        label=label,
        bulk=lambda ids: best_prices(ids, db_path, region_names=region_names),
        any=lambda ids: get_or_fetch_prices(ids, db_path, region_names=region_names),
    )


def station_price_source(db_path: Path, station_id: int, station_label: str) -> PriceSource:
    return PriceSource(
        label=station_label,
        bulk=lambda ids: best_station_prices(ids, station_id, db_path),
        any=lambda ids: get_or_fetch_station_prices(ids, station_id, db_path),
    )


def multi_station_price_source(db_path: Path, station_ids: list[int]) -> PriceSource:
    """Pools several stations -- each item is priced at whichever station in
    the list is cheapest for it, tagged with that specific station's name."""
    return PriceSource(
        label="",  # unused: best_multi_station_prices always returns a per-item location_name
        bulk=lambda ids: price_cache.best_multi_station_prices(ids, station_ids, db_path),
        any=lambda ids: price_cache.get_or_fetch_multi_station_prices(ids, station_ids, db_path),
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


def _optimize_ore(
    mineral_required: dict[int, int],
    refine_pct: dict[str, float],
    db_path: Path,
    price_source: PriceSource,
    ore_mode: str,
    allow_direct_minerals: bool,
):
    """Returns (purchases, total_cost, produced, infeasible_reason)."""
    mineral_ids = sorted(mid for mid, qty in mineral_required.items() if qty > 0)
    if not mineral_ids:
        return [], 0.0, {}, None

    ores = _load_relevant_ores(set(mineral_ids), db_path, ore_mode)
    if not ores:
        mode_note = "" if ore_mode == "any" else f" ({ore_mode} only)"
        return [], 0.0, {}, f"No ore{mode_note} yields any required mineral."

    ore_type_ids = [o[0] for o in ores]
    base_yields = _load_yields(ore_type_ids, db_path)
    ore_prices = price_source.bulk_prices(ore_type_ids)
    # A rare mineral (e.g. Morphite) may only come from an expensive/thin ore
    # (Mercoxit): reprocessing a whole 100-unit portion just to cover a
    # required qty of 1 can cost vastly more than simply buying that 1 unit
    # of the mineral on the market. Offer "buy the mineral directly" as a
    # candidate alongside every ore so the solver picks whichever is cheaper --
    # including a mix (bulk ore for the big minerals, direct buy for a small
    # leftover amount of a rare one). Set allow_direct_minerals=False to force
    # every mineral to come from ore reprocessing instead.

    # Each candidate: (type_id, name, tier, portion_size, unit_price, yield_row, unit_volume)
    candidates: list[tuple[int, str, str, int, float, dict[int, float], float]] = []
    for type_id, name, tier, portion_size, unit_volume in ores:
        price_info = ore_prices.get(type_id)
        if price_info is None:
            continue
        pct = refine_pct.get(tier, 0.0) / 100.0
        yield_row = {
            mat_id: (base_yields.get(type_id, {}).get(mat_id, 0.0) / portion_size) * pct
            for mat_id in mineral_ids
        }
        candidates.append(
            (type_id, name, tier, portion_size, price_info["price"], yield_row, unit_volume)
        )
    n_ore_candidates = len(candidates)

    if allow_direct_minerals:
        mineral_direct_prices = price_source.bulk_prices(mineral_ids)
        mineral_names = blueprint_calc.material_names(mineral_ids, db_path)
        mineral_volumes = blueprint_calc.material_volumes(mineral_ids, db_path)
        for mat_id in mineral_ids:
            price_info = mineral_direct_prices.get(mat_id)
            if price_info is None:
                continue
            candidates.append(
                (
                    mat_id,
                    mineral_names.get(mat_id, str(mat_id)),
                    "Direct",
                    1,
                    price_info["price"],
                    {mat_id: 1.0},
                    mineral_volumes.get(mat_id, 0.0),
                )
            )
    else:
        mineral_direct_prices = {}

    missing = len(ores) - n_ore_candidates
    if not candidates:
        return [], 0.0, {}, f"No cached prices for any candidate ore or mineral in {price_source.label} -- click Refresh Prices first."

    n = len(candidates)
    m = len(mineral_ids)

    # Decision variable per candidate is an integer *batch count* (one batch =
    # one portion_size worth), not a continuous unit quantity -- see module
    # docstring for why. cost/yield are expressed per batch accordingly.
    cost_per_batch = [cand[4] * cand[3] for cand in candidates]  # unit_price * portion_size
    yield_per_batch = [
        [candidates[j][5].get(mineral_ids[i], 0.0) * candidates[j][3] for j in range(n)]
        for i in range(m)
    ]
    required_vec = [mineral_required[mid] for mid in mineral_ids]

    constraints = LinearConstraint(yield_per_batch, lb=required_vec, ub=math.inf)
    res = milp(
        c=cost_per_batch,
        constraints=constraints,
        integrality=[1] * n,
        bounds=Bounds(lb=0, ub=math.inf),
    )

    if not res.success:
        reason = (
            "No feasible combination of ore/direct mineral purchases covers all "
            "required minerals with the cached prices/refine % -- try lowering "
            "refine % requirements or refreshing prices."
        )
        if missing:
            reason += f" ({missing} candidate ore(s) had no cached price and were excluded.)"
        return [], 0.0, {}, reason

    purchases: list[OrePurchase] = []
    produced: dict[int, float] = {mid: 0.0 for mid in mineral_ids}
    total_cost = 0.0
    for j, (type_id, name, tier, portion_size, unit_price, yield_row, unit_volume) in enumerate(candidates):
        batches = round(res.x[j])
        if batches < 1:
            continue
        qty = batches * portion_size
        location_name = (
            mineral_direct_prices[type_id]["location_name"]
            if tier == "Direct"
            else ore_prices[type_id]["location_name"]
        )
        cost = qty * unit_price
        total_cost += cost
        purchases.append(
            OrePurchase(
                type_id=type_id,
                type_name=name,
                tier=tier,
                quantity=qty,
                unit_price=unit_price,
                location_name=location_name,
                cost=cost,
                volume_m3=qty * unit_volume,
            )
        )
        for mat_id, per_unit in yield_row.items():
            produced[mat_id] = produced.get(mat_id, 0.0) + qty * per_unit

    return sorted(purchases, key=lambda p: -p.cost), total_cost, produced, None


def _price_components(
    component_required: dict[int, int],
    db_path: Path,
    price_source: PriceSource,
) -> tuple[list[ComponentPurchase], dict[int, int]]:
    component_ids = [tid for tid, qty in component_required.items() if qty > 0]
    if not component_ids:
        return [], {}

    prices = price_source.any_prices(component_ids)
    names = blueprint_calc.material_names(component_ids, db_path)
    volumes = blueprint_calc.material_volumes(component_ids, db_path)

    purchases: list[ComponentPurchase] = []
    unpriced: dict[int, int] = {}
    for type_id in component_ids:
        qty = component_required[type_id]
        price_info = prices.get(type_id)
        if price_info is None:
            unpriced[type_id] = qty
            continue
        cost = qty * price_info["price"]
        purchases.append(
            ComponentPurchase(
                type_id=type_id,
                type_name=names.get(type_id, str(type_id)),
                quantity=qty,
                unit_price=price_info["price"],
                location_name=price_info["location_name"],
                cost=cost,
                volume_m3=qty * volumes.get(type_id, 0.0),
            )
        )
    return sorted(purchases, key=lambda p: -p.cost), unpriced


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
    the only stations liquid enough for bulk purchases anyway.
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
