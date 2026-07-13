"""Blueprint lookup and ME/run-adjusted material requirements."""
from __future__ import annotations

import math
from pathlib import Path

from indycalc import db
from indycalc.sde_loader import DB_PATH

BLUEPRINT_CATEGORY_ID = 9


def search_blueprints(query: str, db_path: Path = DB_PATH, limit: int = 25) -> list[tuple[int, str]]:
    """Return [(type_id, type_name), ...] for blueprints whose name matches query."""
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT t.typeID, t.typeName
            FROM inv_types t
            JOIN inv_groups g ON g.groupID = t.groupID
            WHERE g.categoryID = ?
              AND t.typeName LIKE ?
              AND t.published = 1
            ORDER BY t.typeName
            LIMIT ?
            """,
            (BLUEPRINT_CATEGORY_ID, f"%{query}%", limit),
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]
    finally:
        conn.close()


def required_materials(
    blueprint_type_id: int, me_percent: float, runs: int, db_path: Path = DB_PATH
) -> dict[int, int]:
    """Return {material_type_id: quantity} needed for `runs` runs at `me_percent` ME.

    Formula matches EVE's material efficiency rule: the ME discount is applied
    to the total (base_qty * runs), rounded up, with a floor of `runs` (every
    run consumes at least 1 unit of each listed material).
    """
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT material_type_id, quantity FROM blueprint_materials WHERE blueprint_type_id = ?",
            (blueprint_type_id,),
        ).fetchall()
    finally:
        conn.close()

    needed: dict[int, int] = {}
    for material_type_id, base_qty in rows:
        total = math.ceil(base_qty * runs * (1 - me_percent / 100))
        needed[int(material_type_id)] = max(runs, total)
    return needed


def material_names(type_ids: list[int], db_path: Path = DB_PATH) -> dict[int, str]:
    conn = db.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in type_ids)
        rows = conn.execute(
            f"SELECT typeID, typeName FROM inv_types WHERE typeID IN ({placeholders})",
            type_ids,
        ).fetchall()
        return {int(tid): name for tid, name in rows}
    finally:
        conn.close()


def material_volumes(type_ids: list[int], db_path: Path = DB_PATH) -> dict[int, float]:
    """Per-unit m3 (packaged volume) for each type_id."""
    if not type_ids:
        return {}
    conn = db.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in type_ids)
        rows = conn.execute(
            f"SELECT typeID, volume FROM inv_types WHERE typeID IN ({placeholders})",
            type_ids,
        ).fetchall()
        return {int(tid): float(vol) for tid, vol in rows}
    finally:
        conn.close()
