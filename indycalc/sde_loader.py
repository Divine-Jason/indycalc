"""Download EVE Online Static Data Export CSVs and load the tables we need
into a local SQLite cache (data/sde.db). Run this once, and again only when
you want to pick up a new SDE (game update) -- not on every app run.

Note: the correct host is fuzzwork.co.uk (singular). "fuzzworks.co.uk" (with
an s) does not resolve.
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

from indycalc import db
from indycalc.ore_tiers import get_tier

SDE_CSV_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"
DB_PATH = Path(__file__).parent / "data" / "sde.db"

MANUFACTURING_ACTIVITY_ID = 1
COPYING_ACTIVITY_ID = 5
REACTION_ACTIVITY_ID = 11
ASTEROID_CATEGORY_ID = 25  # invCategories: ore (incl. compressed variants) lives here


def _fetch_csv(name: str) -> pd.DataFrame:
    url = f"{SDE_CSV_BASE}/{name}.csv"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return pd.read_csv(io.BytesIO(resp.content))


def load_sde(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        print("Fetching invTypes.csv ...")
        inv_types = _fetch_csv("invTypes")[
            ["typeID", "groupID", "typeName", "published", "portionSize", "volume"]
        ]
        inv_types.to_sql("inv_types", conn, if_exists="replace", index=False)

        print("Fetching invGroups.csv ...")
        inv_groups = _fetch_csv("invGroups")[["groupID", "categoryID", "groupName"]]
        inv_groups.to_sql("inv_groups", conn, if_exists="replace", index=False)

        print("Fetching invTypeMaterials.csv (reprocessing yields) ...")
        reprocess = _fetch_csv("invTypeMaterials")[["typeID", "materialTypeID", "quantity"]]
        reprocess.columns = ["ore_type_id", "material_type_id", "quantity"]
        reprocess.to_sql("reprocess_materials", conn, if_exists="replace", index=False)

        print("Fetching industryActivityMaterials.csv (blueprint + reaction requirements) ...")
        activity_mats = _fetch_csv("industryActivityMaterials")

        bp_mats = activity_mats[activity_mats["activityID"] == MANUFACTURING_ACTIVITY_ID]
        bp_mats = bp_mats[["typeID", "materialTypeID", "quantity"]]
        bp_mats.columns = ["blueprint_type_id", "material_type_id", "quantity"]
        bp_mats.to_sql("blueprint_materials", conn, if_exists="replace", index=False)

        reaction_mats = activity_mats[activity_mats["activityID"] == REACTION_ACTIVITY_ID]
        reaction_mats = reaction_mats[["typeID", "materialTypeID", "quantity"]]
        reaction_mats.columns = ["formula_type_id", "material_type_id", "quantity"]
        reaction_mats.to_sql("reaction_materials", conn, if_exists="replace", index=False)

        print("Fetching industryActivityProducts.csv (blueprint/reaction outputs) ...")
        activity_products = _fetch_csv("industryActivityProducts")
        producers = activity_products[
            activity_products["activityID"].isin([MANUFACTURING_ACTIVITY_ID, REACTION_ACTIVITY_ID])
        ][["productTypeID", "typeID", "activityID", "quantity"]]
        producers.columns = ["product_type_id", "blueprint_type_id", "activity_id", "quantity"]
        producers.to_sql("producers", conn, if_exists="replace", index=False)

        print("Fetching industryActivity.csv (job times) ...")
        activity_times = _fetch_csv("industryActivity")
        activity_times = activity_times[
            activity_times["activityID"].isin([MANUFACTURING_ACTIVITY_ID, COPYING_ACTIVITY_ID, REACTION_ACTIVITY_ID])
        ][["typeID", "activityID", "time"]]
        activity_times.columns = ["type_id", "activity_id", "time_seconds"]
        activity_times.to_sql("activity_times", conn, if_exists="replace", index=False)

        print("Fetching mapRegions.csv ...")
        regions = _fetch_csv("mapRegions")[["regionID", "regionName"]]
        regions.columns = ["region_id", "region_name"]
        regions.to_sql("regions", conn, if_exists="replace", index=False)

        print("Fetching mapSolarSystems.csv (highsec systems, for job-cost system picker) ...")
        systems = _fetch_csv("mapSolarSystems")
        systems = systems[systems["security"] >= 0.5][
            ["solarSystemID", "solarSystemName", "regionID", "security"]
        ]
        systems.columns = ["system_id", "system_name", "region_id", "security"]
        systems.to_sql("solar_systems", conn, if_exists="replace", index=False)

        print("Deriving ore tier table from ore_tiers.py ...")
        asteroid_group_ids = set(inv_groups[inv_groups["categoryID"] == ASTEROID_CATEGORY_ID]["groupID"])
        ore_names = inv_types[
            inv_types["groupID"].isin(asteroid_group_ids) & inv_types["typeName"].notna()
        ][["typeID", "typeName", "portionSize", "volume"]]
        ore_tiers = []
        for type_id, type_name, portion_size, volume in zip(
            ore_names["typeID"], ore_names["typeName"], ore_names["portionSize"], ore_names["volume"]
        ):
            tier = get_tier(str(type_name))
            if tier:
                is_compressed = "compressed" in str(type_name).lower()
                ore_tiers.append(
                    (int(type_id), str(type_name), tier, int(portion_size), float(volume), is_compressed)
                )
        ore_tier_df = pd.DataFrame(
            ore_tiers,
            columns=["type_id", "type_name", "tier", "portion_size", "volume", "is_compressed"],
        )
        ore_tier_df.to_sql("ore_tiers", conn, if_exists="replace", index=False)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_types_name ON inv_types(typeName)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reprocess_ore ON reprocess_materials(ore_type_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bp_mats_bp ON blueprint_materials(blueprint_type_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reaction_mats_formula ON reaction_materials(formula_type_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_producers_product ON producers(product_type_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_solar_systems_name ON solar_systems(system_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_times_type ON activity_times(type_id, activity_id)"
        )
        conn.commit()

        for table in [
            "inv_types",
            "inv_groups",
            "reprocess_materials",
            "blueprint_materials",
            "reaction_materials",
            "producers",
            "activity_times",
            "regions",
            "solar_systems",
            "ore_tiers",
        ]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    load_sde()
    print(f"Done. DB at {DB_PATH}")
