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

import math
from dataclasses import dataclass, field
from pathlib import Path

from scipy.optimize import Bounds, LinearConstraint, milp

from indycalc import blueprint_calc, db
from indycalc.price_cache import MINERAL_TYPE_IDS, best_prices, get_or_fetch_prices
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
    region_name: str
    cost: float
    volume_m3: float


@dataclass
class ComponentPurchase:
    type_id: int
    type_name: str
    quantity: int
    unit_price: float
    region_name: str
    cost: float
    volume_m3: float


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
    unpriced_components: dict[int, int] = field(default_factory=dict)
    total_volume_m3: float = 0.0


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
    region_names: list[str] | None,
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
    ore_prices = best_prices(ore_type_ids, db_path, region_names=region_names)
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
        mineral_direct_prices = best_prices(mineral_ids, db_path, region_names=region_names)
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
        where = f" in {region_names[0]}" if region_names else ""
        return [], 0.0, {}, f"No cached prices for any candidate ore or mineral{where} -- click Refresh Prices first."

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
        region_name = (
            mineral_direct_prices[type_id]["region_name"]
            if tier == "Direct"
            else ore_prices[type_id]["region_name"]
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
                region_name=region_name,
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
    region_names: list[str] | None,
) -> tuple[list[ComponentPurchase], dict[int, int]]:
    component_ids = [tid for tid, qty in component_required.items() if qty > 0]
    if not component_ids:
        return [], {}

    prices = get_or_fetch_prices(component_ids, db_path, region_names=region_names)
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
                region_name=price_info["region_name"],
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
    ore_mode: str = "any",
    allow_direct_minerals: bool = True,
) -> OptimizationResult:
    """Solve for the cheapest way to fill `required` (blueprint materials at
    the chosen ME/runs).

    By default (region_names=None) prices are pulled from the cheapest
    across *all* cached highsec regions -- the absolute cost minimum, but
    purchases may be scattered across many stations/regions. Pass a single
    trade hub's region (e.g. ["The Forge"] for Jita) to restrict the plan to
    one place, trading a bit of ISK for the convenience of not hauling ore
    in from all over the map.

    `ore_mode` controls raw vs. compressed ore sourcing -- see ORE_MODES.
    `allow_direct_minerals=False` forces every mineral to come from ore
    reprocessing, never bought directly on the market.
    """
    if ore_mode not in ORE_MODES:
        raise ValueError(f"ore_mode must be one of {ORE_MODES}, got {ore_mode!r}")

    mineral_required = {tid: qty for tid, qty in required.items() if tid in MINERAL_TYPE_IDS}
    component_required = {tid: qty for tid, qty in required.items() if tid not in MINERAL_TYPE_IDS}

    ore_purchases, ore_cost, produced, infeasible_reason = _optimize_ore(
        mineral_required, refine_pct, db_path, region_names, ore_mode, allow_direct_minerals
    )
    if infeasible_reason:
        return _empty_result(required, infeasible_reason)

    component_purchases, unpriced_components = _price_components(
        component_required, db_path, region_names
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
        unpriced_components=unpriced_components,
        total_volume_m3=total_volume_m3,
    )
