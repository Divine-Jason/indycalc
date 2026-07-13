"""Static mapping of EVE Online asteroid ore to its reprocessing rig/skill tier.

Sourced from the reprocessing skill groupings (Simple/Coherent/Variegated/Complex
Ore Processing skills) documented on the EVE University wiki. This is small and
stable enough to hardcode rather than derive from SDE group IDs.

Type names in the SDE follow the pattern "<Quality> <BaseOre>" (e.g. "Concentrated
Veldspar", "Dense Veldspar") and compressed variants are named "Compressed <...>".
Rather than enumerate every quality/compressed variant, tier lookup matches on
whether a base ore name appears in the full type name.
"""

TIERS: dict[str, list[str]] = {
    "Simple": ["Veldspar", "Scordite", "Pyroxeres", "Plagioclase"],
    "Coherent": ["Omber", "Kernite", "Jaspet", "Hemorphite", "Hedbergite", "Ytirium"],
    "Variegated": ["Gneiss", "Dark Ochre", "Crokite"],
    "Complex": ["Bistot", "Arkonor", "Spodumain", "Eifyrium", "Ducinium"],
    "Abyssal": ["Bezdnacine", "Rakovene", "Talassonite"],
    "Erratic": ["Prismaticite"],
    "Mercoxit": ["Mercoxit"],
}

# Default refine % pre-filled in the UI per tier (edit freely in-app).
DEFAULT_REFINE_PCT: dict[str, float] = {tier: 87.6 for tier in TIERS}

_BASE_ORE_TO_TIER: dict[str, str] = {
    base_ore: tier for tier, base_ores in TIERS.items() for base_ore in base_ores
}


def get_tier(type_name: str) -> str | None:
    """Return the reprocessing tier for an ore type name, or None if not an ore."""
    for base_ore, tier in _BASE_ORE_TO_TIER.items():
        if base_ore.lower() in type_name.lower():
            return tier
    return None


def all_base_ore_names() -> list[str]:
    return list(_BASE_ORE_TO_TIER.keys())
