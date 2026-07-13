"""Build-vs-buy expansion for components and reaction materials.

Scope is deliberately capped at two levels, matching the two toggles this
supports:
  - A non-mineral *top-level* blueprint requirement may be built from its own
    Manufacturing blueprint materials (gated by allow_build_components), or
    from its own Reaction formula materials if it's a raw reaction product
    required directly (gated by allow_build_reactions).
  - A non-mineral *sub-material* encountered while building a component may
    itself be built from its Reaction formula (gated by allow_build_reactions)
    -- but never recursed into as a further component build.
Everything below that (fuel blocks, moon materials, gas, PI, deeper nested
components) is always bought directly. See README for why.

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
    made (instead of one live fetch per item during recursion)."""
    discovered.add(type_id)
    if type_id in MINERAL_TYPE_IDS:
        return
    producer = get_producer(type_id, db_path)
    if producer is None:
        return
    if producer["activity_id"] == MANUFACTURING_ACTIVITY_ID and allow_build_components:
        materials = _blueprint_materials(producer["blueprint_type_id"], db_path)
        for mat_id in materials:
            discovered.add(mat_id)
            if mat_id in MINERAL_TYPE_IDS or not allow_build_reactions:
                continue
            r_producer = get_producer(mat_id, db_path)
            if r_producer and r_producer["activity_id"] == REACTION_ACTIVITY_ID:
                discovered.update(_reaction_materials(r_producer["blueprint_type_id"], db_path).keys())
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

    def evaluate_reaction(type_id: int, qty: int):
        """Returns (build_cost, leaf_requirements, runs, produced_qty) or None."""
        producer = get_producer(type_id, db_path)
        if producer is None or producer["activity_id"] != REACTION_ACTIVITY_ID:
            return None
        materials = _reaction_materials(producer["blueprint_type_id"], db_path)
        output_qty = producer["quantity"]
        runs = math.ceil(qty / output_qty)
        pct = reaction_reduction_percent / 100.0
        leaf: dict[int, int] = {}
        cost = 0.0
        for mat_id, base_qty in materials.items():
            need = _me_adjusted_qty(base_qty, runs, pct)
            p = price_of(mat_id)
            if p is None:
                return None
            leaf[mat_id] = leaf.get(mat_id, 0) + need
            cost += need * p
        return cost, leaf, runs, runs * output_qty

    def evaluate_manufacture(type_id: int, qty: int, producer: dict):
        """Returns (build_cost, leaf_minerals, leaf_other, runs, produced_qty)."""
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

            sub = evaluate_reaction(mat_id, need) if allow_build_reactions else None
            buy_p = price_of(mat_id)
            buy_cost = need * buy_p if buy_p is not None else None
            if sub is not None:
                sub_build_cost, sub_leaf, sub_runs, sub_produced = sub
                if buy_cost is not None and buy_cost <= sub_build_cost:
                    leaf_other[mat_id] = leaf_other.get(mat_id, 0) + need
                    cost += buy_cost
                    decisions.append(
                        BuildDecision(mat_id, names.get(mat_id, str(mat_id)), "buy", sub_build_cost, buy_cost, 0, 0, need)
                    )
                else:
                    for sid, sq in sub_leaf.items():
                        target = leaf_minerals if sid in MINERAL_TYPE_IDS else leaf_other
                        target[sid] = target.get(sid, 0) + sq
                    cost += sub_build_cost
                    decisions.append(
                        BuildDecision(
                            mat_id, names.get(mat_id, str(mat_id)), "build", sub_build_cost, buy_cost, sub_runs, sub_produced, need
                        )
                    )
            else:
                if buy_cost is None:
                    return None
                leaf_other[mat_id] = leaf_other.get(mat_id, 0) + need
                cost += buy_cost
        return cost, leaf_minerals, leaf_other, runs, runs * output_qty

    expanded: dict[int, int] = {}
    decisions: list[BuildDecision] = []

    def add(type_id: int, qty: int) -> None:
        expanded[type_id] = expanded.get(type_id, 0) + qty

    for type_id, qty in required.items():
        if type_id in MINERAL_TYPE_IDS:
            add(type_id, qty)
            continue

        producer = get_producer(type_id, db_path)
        buy_p = price_of(type_id)
        buy_cost = qty * buy_p if buy_p is not None else None

        build_result = None
        if producer is not None:
            if producer["activity_id"] == MANUFACTURING_ACTIVITY_ID and allow_build_components:
                build_result = evaluate_manufacture(type_id, qty, producer)
                if build_result is not None:
                    build_cost, leaf_minerals, leaf_other, runs, produced = build_result
            elif producer["activity_id"] == REACTION_ACTIVITY_ID and allow_build_reactions:
                sub = evaluate_reaction(type_id, qty)
                if sub is not None:
                    build_cost, leaf, runs, produced = sub
                    leaf_minerals = {mid: q for mid, q in leaf.items() if mid in MINERAL_TYPE_IDS}
                    leaf_other = {mid: q for mid, q in leaf.items() if mid not in MINERAL_TYPE_IDS}
                    build_result = (build_cost, leaf_minerals, leaf_other, runs, produced)

        if build_result is not None:
            build_cost, leaf_minerals, leaf_other, runs, produced = build_result
            if buy_cost is not None and buy_cost <= build_cost:
                add(type_id, qty)
                decisions.append(
                    BuildDecision(type_id, names.get(type_id, str(type_id)), "buy", build_cost, buy_cost, 0, 0, qty)
                )
            else:
                for mid, mq in leaf_minerals.items():
                    add(mid, mq)
                for oid, oq in leaf_other.items():
                    add(oid, oq)
                decisions.append(
                    BuildDecision(type_id, names.get(type_id, str(type_id)), "build", build_cost, buy_cost, runs, produced, qty)
                )
        else:
            add(type_id, qty)

    return ExpansionResult(expanded_required=expanded, build_decisions=decisions)
