"""Streamlit UI: paste an EVE blueprint, get the cheapest ore purchase plan.

Run with: streamlit run indycalc/app.py
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from indycalc import blueprint_calc, optimizer, price_cache
from indycalc.ore_tiers import DEFAULT_REFINE_PCT, TIERS
from indycalc.sde_loader import DB_PATH

st.set_page_config(page_title="EVE Industry Calculator", layout="wide")

# The sidebar can end up taller than the viewport (blueprint search, ME/runs,
# refine % per tier, market price controls, buy-from picker). Streamlit's
# sidebar is supposed to scroll on its own, but some browser/zoom/theme
# combinations clip the overflow instead of scrolling to it -- force it.
st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] > div {
        overflow-y: auto;
        max-height: 100vh;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("EVE Industry Calculator")

if not DB_PATH.exists():
    st.error(
        f"No local SDE database found at {DB_PATH}.\n\n"
        "Run `python -m indycalc.sde_loader` first to build the local data cache."
    )
    st.stop()

with st.sidebar:
    st.header("Blueprint")
    query = st.text_input("Search blueprint name", value="Rifter")
    matches = blueprint_calc.search_blueprints(query) if query else []
    if not matches:
        st.warning("No blueprints match that search.")
        st.stop()
    labels = [f"{name} (#{type_id})" for type_id, name in matches]
    selected = st.selectbox("Select blueprint", options=range(len(matches)), format_func=lambda i: labels[i])
    bp_type_id, bp_name = matches[selected]

    me_percent = st.number_input("Material Efficiency %", min_value=0, max_value=10, value=10, step=1)
    runs = st.number_input("Runs", min_value=1, value=1, step=1)

    st.header("Where to buy")
    buy_mode_options = ["Cheapest overall (scattered across regions)"] + list(price_cache.TRADE_HUBS)
    buy_mode = st.selectbox(
        "Buy from",
        options=buy_mode_options,
        help=(
            "\"Cheapest overall\" picks the lowest-cost ore per material across all "
            "cached highsec regions -- the absolute ISK minimum, but you may need to "
            "collect ore from several different regions. Picking a single trade hub "
            "buys everything from that one hub's region instead, trading some ISK "
            "for the convenience of one shopping trip."
        ),
    )
    selected_region_names = (
        None if buy_mode == buy_mode_options[0] else [price_cache.TRADE_HUBS[buy_mode]]
    )

    st.header("Ore sourcing")
    ore_mode_labels = {
        "Cheapest (raw or compressed)": "any",
        "Compressed only (~100x less volume)": "compressed",
        "Uncompressed only": "uncompressed",
    }
    ore_mode_label = st.selectbox(
        "Ore type",
        options=list(ore_mode_labels),
        help=(
            "Compressed ore reprocesses into the same minerals as raw ore but takes "
            "about 1/100th the cargo volume, usually for a small ISK premium. Pick "
            "\"Uncompressed only\" if you're hauling raw ore to a refinery in bulk "
            "(e.g. with a freighter) and compressing it yourself, so market "
            "compression pricing doesn't matter to you."
        ),
    )
    ore_mode = ore_mode_labels[ore_mode_label]

    no_direct = st.toggle(
        "No direct mineral purchases",
        value=False,
        help=(
            "By default, a mineral that's only cheaply available from a rare/thin "
            "ore (e.g. Morphite from Mercoxit) can be bought directly on the market "
            "instead of reprocessing a whole ore batch for it. Turn this on to force "
            "every mineral to come from ore reprocessing only, even if that's pricier."
        ),
    )
    allow_direct_minerals = not no_direct

    st.header("Market prices")
    last_refresh = price_cache.last_refreshed()
    if last_refresh:
        age_min = (time.time() - last_refresh) / 60
        st.caption(f"Last refreshed {age_min:.0f} min ago")
    else:
        st.caption("Prices never refreshed yet")
    if st.button("Refresh Prices (highsec regions)"):
        with st.spinner("Fetching prices from Fuzzworks across highsec regions..."):
            n = price_cache.refresh_prices()
        st.success(f"Refreshed {n} region/ore price rows.")
        st.rerun()

    with st.expander("Refine % by ore tier"):
        refine_pct: dict[str, float] = {}
        for tier in TIERS:
            refine_pct[tier] = st.number_input(
                f"{tier} ore refine %",
                min_value=0.0,
                max_value=100.0,
                value=DEFAULT_REFINE_PCT[tier],
                step=0.1,
                key=f"refine_{tier}",
            )

st.subheader(f"{bp_name} — ME {me_percent}% — {runs} run(s)")

required = blueprint_calc.required_materials(bp_type_id, me_percent, runs)
if not required:
    st.warning("This blueprint has no manufacturing materials in the local SDE cache.")
    st.stop()

mat_names = blueprint_calc.material_names(list(required.keys()))
req_df = pd.DataFrame(
    [
        {
            "Material": mat_names.get(mid, str(mid)),
            "Required Qty": qty,
            "Source": "Ore (reprocessed)" if mid in price_cache.MINERAL_TYPE_IDS else "Component (bought directly)",
        }
        for mid, qty in required.items()
    ]
).sort_values("Material")
st.dataframe(req_df, hide_index=True, width='stretch')

st.subheader("Compare buying locations")
comparison_rows = []
for label, region_names in [("Cheapest overall (scattered)", None)] + [
    (hub, [region]) for hub, region in price_cache.TRADE_HUBS.items()
]:
    r = optimizer.optimize(
        required,
        refine_pct,
        region_names=region_names,
        ore_mode=ore_mode,
        allow_direct_minerals=allow_direct_minerals,
    )
    comparison_rows.append(
        {
            "Buy from": label,
            "Total Cost (ISK)": round(r.total_cost, 2) if not r.infeasible_reason else None,
            "Total Volume (m3)": round(r.total_volume_m3, 2) if not r.infeasible_reason else None,
            "Status": "OK" if not r.infeasible_reason else r.infeasible_reason,
        }
    )
comparison_df = pd.DataFrame(comparison_rows).sort_values("Total Cost (ISK)", na_position="last")
st.dataframe(comparison_df, hide_index=True, width='stretch')

result = optimizer.optimize(
    required,
    refine_pct,
    region_names=selected_region_names,
    ore_mode=ore_mode,
    allow_direct_minerals=allow_direct_minerals,
)

if result.infeasible_reason:
    st.error(result.infeasible_reason)
    st.stop()

st.subheader(f"Purchase plan — {buy_mode}")

if result.purchases:
    st.markdown("**Ore to buy and reprocess (or mineral to buy directly, when that's cheaper)**")
    purchase_df = pd.DataFrame(
        [
            {
                "Ore / Mineral": p.type_name,
                "Tier": p.tier,
                "Quantity": p.quantity,
                "Cheapest Region": p.region_name,
                "Unit Price (ISK)": round(p.unit_price, 2),
                "Cost (ISK)": round(p.cost, 2),
                "Volume (m3)": round(p.volume_m3, 2),
            }
            for p in result.purchases
        ]
    )
    st.dataframe(purchase_df, hide_index=True, width='stretch')

if result.component_purchases:
    st.markdown("**Components / PI / reaction materials to buy directly**")
    st.caption(
        "These aren't ore-derived, so rather than modeling their own build chain "
        "(reactions, PI, sub-components...) they're just priced at the cheapest "
        "cached market sell price."
    )
    component_df = pd.DataFrame(
        [
            {
                "Component": p.type_name,
                "Quantity": p.quantity,
                "Cheapest Region": p.region_name,
                "Unit Price (ISK)": round(p.unit_price, 2),
                "Cost (ISK)": round(p.cost, 2),
                "Volume (m3)": round(p.volume_m3, 2),
            }
            for p in result.component_purchases
        ]
    )
    st.dataframe(component_df, hide_index=True, width='stretch')

if result.unpriced_components:
    unpriced_names = blueprint_calc.material_names(list(result.unpriced_components.keys()))
    st.warning(
        "No cached market price for: "
        + ", ".join(
            f"{unpriced_names.get(tid, tid)} (need {qty})"
            for tid, qty in result.unpriced_components.items()
        )
        + ". Not included in total cost below."
    )

col1, col2 = st.columns(2)
col1.metric("Total cost", f"{result.total_cost:,.2f} ISK")
col2.metric("Total volume", f"{result.total_volume_m3:,.2f} m3")

if result.waste_qty:
    st.subheader("Waste (leftover minerals above requirement)")
    waste_df = pd.DataFrame(
        [
            {
                "Mineral": mat_names.get(mid, str(mid)),
                "Produced": round(result.produced.get(mid, 0), 1),
                "Required": required[mid],
                "Leftover": round(result.waste_qty.get(mid, 0), 1),
                "Leftover Value (ISK)": round(result.waste_isk.get(mid, 0), 2),
            }
            for mid in result.waste_qty
        ]
    ).sort_values("Leftover Value (ISK)", ascending=False)
    st.dataframe(waste_df, hide_index=True, width='stretch')
    st.metric("Total waste value", f"{result.total_waste_isk:,.2f} ISK")
