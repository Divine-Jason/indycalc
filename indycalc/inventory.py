"""Credit materials you already have (ore, minerals, components, PI, ...)
against a blueprint's required materials, before any buy/build pricing runs.

Two things happen with a pasted inventory:
  - Owned ore is reprocessed into minerals, in whole batches only (the same
    portion-size rule the purchase-plan MILP itself uses in optimizer.py) --
    a partial batch you're still mining toward isn't counted as free minerals
    it can't actually yield yet.
  - Everything else owned (minerals, components, PI materials, ...) credits
    1:1 against the matching required type_id -- no conversion needed, the
    units already match what `blueprint_calc.required_materials` asks for.

The result is a reduced `required` dict that the rest of the app (build-vs-buy
expansion, the buying-location comparison, the final purchase plan) uses
exactly as before -- this module's whole job is to shrink that dict before
anyone else sees it, not to change how anything downstream works.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from indycalc import blueprint_calc, db
from indycalc.sde_loader import DB_PATH

# A line copied straight from an EVE inventory/PI window is tab-separated:
# "Name\tQuantity\t...other columns...". A line typed or pasted by hand might
# just be "Name 1500" or "Name, 1500" instead -- both are accepted.
_TRAILING_QTY_RE = re.compile(r"^(.*?)[\s,]+([\d,]+)\s*$")


@dataclass
class ParsedInventory:
    owned: dict[int, int]
    unmatched_lines: list[str] = field(default_factory=list)


def _type_name_lookup(db_path: Path) -> dict[str, int]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT typeID, typeName FROM inv_types WHERE typeName IS NOT NULL").fetchall()
        return {str(name).strip().lower(): int(tid) for tid, name in rows}
    finally:
        conn.close()


def parse_inventory_text(text: str, db_path: Path = DB_PATH) -> ParsedInventory:
    """One item per line. Accepts EVE's tab-separated inventory-window paste
    (name is always the first column; whichever later column is a plain
    integer is taken as the quantity) or a simpler hand-typed "Name Qty" /
    "Name, Qty" line. Matches names against the local SDE cache, case-
    insensitively; a line that doesn't resolve to a known type name or a
    trailing quantity is reported back rather than silently dropped, so a
    typo doesn't just quietly vanish from the credited total."""
    name_to_id = _type_name_lookup(db_path)
    owned: dict[int, int] = {}
    unmatched: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        name_part: str | None = None
        qty_part: str | None = None

        if "\t" in line:
            fields = [f.strip() for f in line.split("\t") if f.strip() != ""]
            if len(fields) >= 2:
                name_part = fields[0]
                for candidate in fields[1:]:
                    if re.fullmatch(r"[\d,]+", candidate):
                        qty_part = candidate
                        break
        if qty_part is None:
            m = _TRAILING_QTY_RE.match(line)
            if m:
                name_part, qty_part = m.group(1).strip().rstrip(","), m.group(2)

        if name_part is None or qty_part is None:
            unmatched.append(raw_line)
            continue

        try:
            qty = int(qty_part.replace(",", ""))
        except ValueError:
            unmatched.append(raw_line)
            continue

        type_id = name_to_id.get(name_part.strip().lower())
        if type_id is None or qty <= 0:
            unmatched.append(raw_line)
            continue

        owned[type_id] = owned.get(type_id, 0) + qty

    return ParsedInventory(owned=owned, unmatched_lines=unmatched)


def _ore_tier_info(type_ids: list[int], db_path: Path) -> dict[int, tuple[str, int]]:
    """{type_id: (tier, portion_size)} for whichever of `type_ids` are ore."""
    if not type_ids:
        return {}
    conn = db.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in type_ids)
        rows = conn.execute(
            f"SELECT type_id, tier, portion_size FROM ore_tiers WHERE type_id IN ({placeholders})",
            type_ids,
        ).fetchall()
        return {int(tid): (tier, int(portion)) for tid, tier, portion in rows}
    finally:
        conn.close()


def _reprocess_yields(ore_type_ids: list[int], db_path: Path) -> dict[int, dict[int, float]]:
    """{ore_type_id: {material_type_id: base_quantity_per_portion}}"""
    if not ore_type_ids:
        return {}
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
            yields.setdefault(int(ore_id), {})[int(mat_id)] = qty
        return yields
    finally:
        conn.close()


@dataclass
class OwnedItemCredit:
    type_id: int
    name: str
    owned_qty: int
    kind: str  # "ore" or "direct"
    tier: str | None = None
    batches_reprocessed: int = 0
    leftover_qty: int = 0  # ore short of a full batch -- not reprocessed, not lost
    credited: dict[int, float] = field(default_factory=dict)


@dataclass
class InventoryCredit:
    reduced_required: dict[int, int]
    items: list[OwnedItemCredit] = field(default_factory=list)


def apply_inventory(
    required: dict[int, int],
    owned: dict[int, int],
    refine_pct: dict[str, float],
    db_path: Path = DB_PATH,
) -> InventoryCredit:
    if not owned:
        return InventoryCredit(reduced_required=dict(required))

    owned_ids = list(owned.keys())
    tier_info = _ore_tier_info(owned_ids, db_path)
    ore_ids = [tid for tid in owned_ids if tid in tier_info]
    yields = _reprocess_yields(ore_ids, db_path)
    names = blueprint_calc.material_names(owned_ids, db_path)

    credit: dict[int, float] = {}
    items: list[OwnedItemCredit] = []

    for type_id, qty in owned.items():
        name = names.get(type_id, str(type_id))
        if type_id in tier_info:
            tier, portion_size = tier_info[type_id]
            pct = refine_pct.get(tier, 0.0) / 100.0
            batches = qty // portion_size
            leftover = qty - batches * portion_size
            base_yield = yields.get(type_id, {})
            credited = {
                mid: base_qty * pct * batches
                for mid, base_qty in base_yield.items()
                if base_qty * pct * batches > 0
            }
            for mid, v in credited.items():
                credit[mid] = credit.get(mid, 0.0) + v
            items.append(OwnedItemCredit(type_id, name, qty, "ore", tier, batches, leftover, credited))
        else:
            credit[type_id] = credit.get(type_id, 0.0) + qty
            items.append(OwnedItemCredit(type_id, name, qty, "direct", credited={type_id: qty}))

    reduced: dict[int, int] = {}
    for mid, need in required.items():
        left = need - credit.get(mid, 0.0)
        if left > 1e-9:
            reduced[mid] = math.ceil(left)

    return InventoryCredit(reduced_required=reduced, items=items)
