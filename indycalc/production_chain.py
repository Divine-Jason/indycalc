"""Build-vs-buy expansion for components and reaction materials.

Two independent toggles:
  - allow_build_components: any non-mineral Manufacturing product encountered
    anywhere in the tree -- the top-level requirement itself, or a material
    several Manufacturing levels below it -- is compared build-vs-buy and
    recursed into if building wins. Capital ship/structure components can
    nest several Manufacturing levels deep (e.g. Neurolink Protection Cell ->
    Neurolink Enhancer Reservoir -> Programmable Purification Membrane), so
    this recursion is not depth-limited.
  - allow_build_reactions: any non-mineral Reaction product encountered is
    likewise compared build-vs-buy. A Reaction formula's own materials are
    always a leaf, though -- bought directly, never built further -- since
    reaction inputs (fuel blocks, moon materials, gas, PI) don't have their
    own producer in the SDE to begin with.
A cycle guard prevents infinite recursion in the pathological case of a
blueprint that (directly or indirectly) requires its own product.

The build-vs-buy decision uses a quick, order-independent price estimate
(direct market price for every leaf, no region restriction) rather than the
real ore-reprocessing MILP -- that decision only makes sense to run once,
globally, after this expansion has determined the full mineral total across
the whole tree. See optimizer.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from indycalc import blueprint_calc, db
from indycalc.price_cache import MINERAL_TYPE_IDS, get_or_fetch_prices
from indycalc.sde_loader import DB_PATH, MANUFACTURING_ACTIVITY_ID, REACTION_ACTIVITY_ID


@dataclass
class BuildDecision:
    type_id: int
    name: str
    decision: str  # "build" or "buy"
    build_cost: float | None
    buy_cost: float | None
    runs: int = 0
    produced_qty: int = 0
    needed_qty: int = 0


@dataclass
class ExpansionResult:
    expanded_required: dict[int, int]
    build_decisions: list[BuildDecision] = field(default_factory=list)


def get_producer(type_id: int, db_path: Path = DB_PATH) -> dict | None:
    """What produces this item, and how much per run. Prefers a Manufacturing
    producer over a Reaction one if (unexpectedly) both exist."""
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT blueprint_type_id, activity_id, quantity FROM producers "
            "WHERE product_type_id = ? ORDER BY activity_id LIMIT 1",
            (type_id,),
        ).fetchone()
        if row is None:
            return None
        return {"blueprint_type_id": int(row[0]), "activity_id": int(row[1]), "quantity": row[2] or 1}
    finally:
        conn.close()


def _blueprint_materials(blueprint_type_id: int, db_path: Path) -> dict[int, float]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT material_type_id, quantity FROM blueprint_materials WHERE blueprint_type_id = ?",
            (blueprint_type_id,),
        ).fetchall()
        return {int(mid): qty for mid, qty in rows}
    finally:
        conn.close()


def _reaction_materials(formula_type_id: int, db_path: Path) -> dict[int, float]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT material_type_id, quantity FROM reaction_materials WHERE formula_type_id = ?",
            (formula_type_id,),
        ).fetchall()
        return {int(mid): qty for mid, qty in rows}
    finally:
        conn.close()


def _me_adjusted_qty(base_qty: float, runs: int, pct: float) -> int:
    return max(runs, math.ceil(base_qty * runs * (1 - pct)))


def _discover(
    type_id: int,
    allow_build_components: bool,
    allow_build_reactions: bool,
    db_path: Path,
    discovered: set[int],
) -> None:
    """Populate `discovered` with every type_id whose price might be needed,
    so they can all be fetched in one batched call before any decisions are
    made (instead of one live fetch per item during recursion). Recurses
    through the full Manufacturing chain to whatever depth it actually goes;
    a Reaction producer's own materials are always a leaf (see module
    docstring). `discovered` doubles as the visited set, so a type_id
    reachable multiple ways (or, pathologically, cyclically) is only walked
    once."""
    if type_id in discovered:
        return
    discovered.add(type_id)
    if type_id in MINERAL_TYPE_IDS:
        return
    producer = get_producer(type_id, db_path)
    if producer is None:
        return
    if producer["activity_id"] == MANUFACTURING_ACTIVITY_ID and allow_build_components:
        materials = _blueprint_materials(producer["blueprint_type_id"], db_path)
        for mat_id in materials:
            _discover(mat_id, allow_build_components, allow_build_reactions, db_path, discovered)
    elif producer["activity_id"] == REACTION_ACTIVITY_ID and allow_build_reactions:
        discovered.update(_reaction_materials(producer["blueprint_type_id"], db_path).keys())


def expand_requirements(
    required: dict[int, int],
    allow_build_components: bool,
    allow_build_reactions: bool,
    component_me_percent: float,
    reaction_reduction_percent: float,
    db_path: Path = DB_PATH,
) -> ExpansionResult:
    if not allow_build_components and not allow_build_reactions:
        return ExpansionResult(expanded_required=dict(required))

    discovered: set[int] = set()
    for type_id in required:
        _discover(type_id, allow_build_components, allow_build_reactions, db_path, discovered)

    prices = get_or_fetch_prices(list(discovered), db_path) if discovered else {}
    names = blueprint_calc.material_names(list(discovered), db_path)

    def price_of(type_id: int) -> float | None:
        info = prices.get(type_id)
        return info["price"] if info else None

    decisions: list[BuildDecision] = []

    def build_from_reaction(type_id: int, qty: int, producer: dict):
        """Returns (build_cost, leaf_minerals, leaf_other, runs, produced_qty).
        A reaction formula's own materials are always a leaf -- bought
        directly, never built further (see module docstring)."""
        materials = _reaction_materials(producer["blueprint_type_id"], db_path)
        output_qty = producer["quantity"]
        runs = math.ceil(qty / output_qty)
        pct = reaction_reduction_percent / 100.0
        leaf_minerals: dict[int, int] = {}
        leaf_other: dict[int, int] = {}
        cost = 0.0
        for mat_id, base_qty in materials.items():
            need = _me_adjusted_qty(base_qty, runs, pct)
            p = price_of(mat_id)
            if p is None:
                return None
            target = leaf_minerals if mat_id in MINERAL_TYPE_IDS else leaf_other
            target[mat_id] = target.get(mat_id, 0) + need
            cost += need * p
        return cost, leaf_minerals, leaf_other, runs, runs * output_qty

    def build_from_manufacture(type_id: int, qty: int, producer: dict, visiting: frozenset[int]):
        """Returns (build_cost, leaf_minerals, leaf_other, runs, produced_qty).
        Non-mineral materials are resolved recursively via `resolve()` --
        each may itself be built (Manufacturing, to any depth, or Reaction)
        or bought, whichever is cheaper."""
        materials = _blueprint_materials(producer["blueprint_type_id"], db_path)
        output_qty = producer["quantity"]
        runs = math.ceil(qty / output_qty)
        pct = component_me_percent / 100.0
        leaf_minerals: dict[int, int] = {}
        leaf_other: dict[int, int] = {}
        cost = 0.0
        for mat_id, base_qty in materials.items():
            need = _me_adjusted_qty(base_qty, runs, pct)
            if mat_id in MINERAL_TYPE_IDS:
                p = price_of(mat_id)
                if p is None:
                    return None
                leaf_minerals[mat_id] = leaf_minerals.get(mat_id, 0) + need
                cost += need * p
                continue

            sub = resolve(mat_id, need, visiting)
            if sub is None:
                return None
            sub_cost, sub_minerals, sub_other = sub
            for sid, sq in sub_minerals.items():
                leaf_minerals[sid] = leaf_minerals.get(sid, 0) + sq
            for sid, sq in sub_other.items():
                leaf_other[sid] = leaf_other.get(sid, 0) + sq
            cost += sub_cost
        return cost, leaf_minerals, leaf_other, runs, runs * output_qty

    def resolve(type_id: int, qty: int, visiting: frozenset[int] = frozenset()):
        """Best (cheapest) way to obtain `qty` units of a non-mineral
        type_id: build it (recursively) if that's possible and cheaper, else
        buy it outright. Returns (cost, leaf_minerals, leaf_other) -- the
        flattened set of raw purchases needed, all the way down -- or None
        if `type_id` can neither be bought (no liquid market price anywhere)
        nor built (same problem, recursively, for something it needs).
        Appends a BuildDecision whenever there was an actual build option to
        weigh against buying, so the "why" is visible even when buy wins or
        the whole thing turns out infeasible."""
        if type_id in visiting:
            return None  # cycle guard against a pathological self-referencing BOM

        producer = get_producer(type_id, db_path)
        build = None
        if producer is not None:
            if producer["activity_id"] == MANUFACTURING_ACTIVITY_ID and allow_build_components:
                build = build_from_manufacture(type_id, qty, producer, visiting | {type_id})
            elif producer["activity_id"] == REACTION_ACTIVITY_ID and allow_build_reactions:
                build = build_from_reaction(type_id, qty, producer)

        buy_p = price_of(type_id)
        buy_cost = qty * buy_p if buy_p is not None else None

        if build is not None:
            build_cost, leaf_minerals, leaf_other, runs, produced = build
            if buy_cost is not None and buy_cost <= build_cost:
                decisions.append(
                    BuildDecision(type_id, names.get(type_id, str(type_id)), "buy", build_cost, buy_cost, 0, 0, qty)
                )
                return buy_cost, {}, {type_id: qty}
            decisions.append(
                BuildDecision(type_id, names.get(type_id, str(type_id)), "build", build_cost, buy_cost, runs, produced, qty)
            )
            return build_cost, leaf_minerals, leaf_other

        if buy_cost is not None:
            return buy_cost, {}, {type_id: qty}

        # Neither buildable (no producer, or building it hit this same dead
        # end further down) nor buyable (no liquid market price anywhere).
        # Surfaced explicitly rather than just vanishing from the decisions
        # list, since a silent failure here is indistinguishable from "never
        # tried" -- e.g. some newer capital-ship materials are reward-only
        # Commodities items with no blueprint in the SDE at all, so there's
        # nothing to build from and nobody selling one either.
        decisions.append(BuildDecision(type_id, names.get(type_id, str(type_id)), "unavailable", None, None, 0, 0, qty))
        return None

    expanded: dict[int, int] = {}

    def add(type_id: int, qty: int) -> None:
        expanded[type_id] = expanded.get(type_id, 0) + qty

    for type_id, qty in required.items():
        if type_id in MINERAL_TYPE_IDS:
            add(type_id, qty)
            continue

        result = resolve(type_id, qty)
        if result is None:
            add(type_id, qty)
            continue
        _, leaf_minerals, leaf_other = result
        for mid, mq in leaf_minerals.items():
            add(mid, mq)
        for oid, oq in leaf_other.items():
            add(oid, oq)

    return ExpansionResult(expanded_required=expanded, build_decisions=decisions)
