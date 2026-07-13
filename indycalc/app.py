"""Streamlit UI: paste an EVE blueprint, get the cheapest ore purchase plan.

Run with: streamlit run indycalc/app.py
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from indycalc import blueprint_calc, job_cost, optimizer, price_cache, production_chain
from indycalc.ore_tiers import DEFAULT_REFINE_PCT, TIERS
from indycalc.sde_loader import DB_PATH

# Streamlit reruns this whole script on every widget interaction. Without
# caching, every rerun redoes the full MILP-based optimize() call for every
# entry in the buying-location comparison table (region + station for each
# of 5 hubs = 11 calls), even when the change that triggered the rerun was
# something unrelated (e.g. a job-cost slider that doesn't feed into buying
# at all). st.cache_data memoizes by argument hash across reruns; cleared
# explicitly whenever cached market/industry data is refreshed so results
# can't go stale silently.
cached_expand = st.cache_data(show_spinner=False)(production_chain.expand_requirements)
cached_optimize = st.cache_data(show_spinner=False)(optimizer.optimize)
cached_best_station_combo = st.cache_data(show_spinner=False)(optimizer.best_station_combo)


@st.cache_data(show_spinner=False)
def cached_comparison_table(
    required, refine_pct, ore_mode, allow_direct_minerals,
    allow_build_components, allow_build_reactions,
    component_me_percent, reaction_reduction_percent, pre_expanded,
) -> pd.DataFrame:
    """The "compare buying locations" table: region-wide + single-station
    cost for every hub, plus the unrestricted "cheapest overall" row -- 11
    independent optimize() calls (sequential -- see optimize_many()'s
    docstring for why not threaded). Cached as a whole so this only actually
    runs once per distinct set of inputs, not on every rerun."""
    common = dict(
        required=required,
        refine_pct=refine_pct,
        ore_mode=ore_mode,
        allow_direct_minerals=allow_direct_minerals,
        allow_build_components=allow_build_components,
        allow_build_reactions=allow_build_reactions,
        component_me_percent=component_me_percent,
        reaction_reduction_percent=reaction_reduction_percent,
        pre_expanded=pre_expanded,
    )

    jobs = [{"region_names": None, **common}]
    job_meta: list[tuple[str, str | None]] = [("Cheapest overall (scattered)", None)]
    for hub, region in price_cache.TRADE_HUBS.items():
        jobs.append({"region_names": [region], **common})
        job_meta.append((hub, "region"))
        jobs.append({"station_id": price_cache.HUB_STATION_IDS[hub], "station_label": hub, **common})
        job_meta.append((hub, "station"))

    results = optimizer.optimize_many(jobs)

    by_hub: dict[str, dict] = {}
    rows = []
    for (label, kind), r in zip(job_meta, results):
        if kind is None:
            rows.append(
                {
                    "Buy from": label,
                    "Total Cost (ISK)": round(r.total_cost, 2) if not r.infeasible_reason else None,
                    "Total Volume (m3)": round(r.total_volume_m3, 2) if not r.infeasible_reason else None,
                    "Status": "OK" if not r.infeasible_reason else r.infeasible_reason,
                    "Single Station?": "",
                    "Single Station Cost (ISK)": None,
                }
            )
        elif kind == "region":
            by_hub[label] = {
                "Buy from": label,
                "Total Cost (ISK)": round(r.total_cost, 2) if not r.infeasible_reason else None,
                "Total Volume (m3)": round(r.total_volume_m3, 2) if not r.infeasible_reason else None,
                "Status": "OK" if not r.infeasible_reason else r.infeasible_reason,
            }
        else:  # station
            by_hub[label]["Single Station?"] = "Yes" if not r.infeasible_reason else "No"
            by_hub[label]["Single Station Cost (ISK)"] = (
                round(r.total_cost, 2) if not r.infeasible_reason else None
            )

    rows.extend(by_hub.values())
    return pd.DataFrame(rows).sort_values("Total Cost (ISK)", na_position="last")


def _merge_for_display(purchases) -> list[dict]:
    """Collapse purchases to one row per (item, location) for the table --
    multibuy fills from cheapest orders automatically up to whatever total
    quantity you enter, so showing e.g. 4 separate rows for the same item
    at the same location (one per price tier) just to buy one thing is
    noise, not useful detail. Unit Price becomes a quantity-weighted
    average; Price Range keeps a hint of the original tier spread (min-max)
    without needing a row per tier -- the underlying total cost is still
    the real tier-aware sum, only the display is collapsed."""
    merged: dict[tuple, dict] = {}
    for p in purchases:
        key = (p.type_name, p.location_name)
        row = merged.setdefault(
            key,
            {"name": p.type_name, "tier": getattr(p, "tier", None), "location": p.location_name,
             "quantity": 0, "cost": 0.0, "volume_m3": 0.0, "min_price": p.unit_price, "max_price": p.unit_price},
        )
        row["quantity"] += p.quantity
        row["cost"] += p.cost
        row["volume_m3"] += p.volume_m3
        row["min_price"] = min(row["min_price"], p.unit_price)
        row["max_price"] = max(row["max_price"], p.unit_price)
    return list(merged.values())


def _multibuy_blocks(result: optimizer.OptimizationResult) -> dict[str, str]:
    """{location_name: multibuy-paste text}, one block per location -- "Item
    Name Quantity" per line (EVE's Multi-buy paste format), combining ore/
    mineral and component purchases. Quantities are summed across every
    price tier for the same item at the same location, since multibuy just
    wants a total to fill, not a breakdown of which order supplies it."""
    totals: dict[str, dict[str, float]] = {}
    for p in list(result.purchases) + list(result.component_purchases):
        totals.setdefault(p.location_name, {})
        totals[p.location_name][p.type_name] = totals[p.location_name].get(p.type_name, 0.0) + p.quantity
    return {
        location: "\n".join(f"{name} {int(round(qty))}" for name, qty in sorted(items.items()))
        for location, items in totals.items()
    }


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)

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
    buy_mode_options = ["Cheapest overall (scattered across hub regions)"] + list(price_cache.TRADE_HUBS)
    buy_mode = st.selectbox(
        "Buy from",
        options=buy_mode_options,
        help=(
            "\"Cheapest overall\" picks the lowest-cost ore per material across the 5 "
            "regions that contain a major trade hub (still region-wide, not just each "
            "hub's own station -- e.g. Perimeter's market counts as part of Jita's "
            "region) -- the absolute ISK minimum within that scope, but you may need "
            "to collect ore from several different regions. Picking a single trade hub "
            "buys everything from that one hub's region instead, trading some ISK "
            "for the convenience of one shopping trip -- still possibly several "
            "systems within that region, though. Check \"single station\" below for "
            "genuinely one stop."
        ),
    )
    selected_region_names = (
        None if buy_mode == buy_mode_options[0] else [price_cache.TRADE_HUBS[buy_mode]]
    )
    selected_station_id = None
    selected_station_label = None
    if buy_mode != buy_mode_options[0]:
        prefer_single_station = st.toggle(
            f"Buy everything from {buy_mode}'s station",
            value=False,
            help=(
                "Prices everything from that hub's single busiest trade station "
                "instead of the whole region -- genuinely one stop, but will fail "
                "if something needed isn't liquid at that specific station (check "
                "the comparison table below first)."
            ),
        )
        if prefer_single_station:
            selected_station_id = price_cache.HUB_STATION_IDS[buy_mode]
            selected_station_label = buy_mode
            selected_region_names = None

    use_station_combo = st.toggle(
        "Limit to at most N stations",
        value=False,
        help=(
            "Searches every combination of up to N of the candidate hub stations "
            "below and uses whichever combination is cheapest while covering "
            "everything -- a middle ground between one station (most convenient) "
            "and scattering across all of highsec (cheapest). Shown in its own "
            "section below since the search takes a few seconds."
        ),
    )
    max_stations_combo = 2
    if use_station_combo:
        max_stations_combo = st.number_input("N", min_value=1, max_value=5, value=2, step=1)
        selected_region_names = None
        selected_station_id = None
        selected_station_label = None

    combo_candidate_hubs = st.multiselect(
        "Stations eligible for the combo search",
        options=list(price_cache.HUB_STATION_IDS),
        default=list(price_cache.HUB_STATION_IDS),
        help=(
            "Which hubs the combo search (above, and the \"Best combo\" section "
            "shown for \"Cheapest overall\") is allowed to combine. Deselect a hub "
            "that's numerically cheap but impractical to actually route to -- e.g. "
            "Amarr is roughly 30 jumps from the other 4 hubs, so a combo that "
            "includes it can be cheaper on paper while being a much worse shopping "
            "trip in practice."
        ),
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

    st.header("Production")
    allow_build_components = st.toggle(
        "Craft components myself",
        value=False,
        help=(
            "Components (Tech II items, etc.) have their own Manufacturing blueprint. "
            "When on, each component is priced both ways -- buy it outright, or build "
            "it from its own materials -- and whichever is cheaper is used."
        ),
    )
    component_me_percent = 0.0
    if allow_build_components:
        component_me_percent = st.number_input(
            "Component ME %",
            min_value=0.0,
            max_value=10.0,
            value=0.0,
            step=1.0,
            help="Material Efficiency level of the component blueprints you'd use. Invented T2 BPCs are usually 0-4%.",
        )

    allow_build_reactions = st.toggle(
        "Craft reaction materials myself",
        value=False,
        help=(
            "Reaction materials (e.g. Fernite Carbide) have their own Reaction formula. "
            "When on, any reaction material pulled in while building a component is "
            "priced both ways too. Only matters if \"Craft components myself\" finds "
            "something worth building."
        ),
    )
    reaction_reduction_percent = 0.0
    if allow_build_reactions:
        reaction_reduction_percent = st.number_input(
            "Reaction material reduction %",
            min_value=0.0,
            max_value=50.0,
            value=0.0,
            step=1.0,
            help=(
                "Reaction formulas have no ME research -- this represents your "
                "refinery's Reaction rig bonus instead, if any."
            ),
        )

    st.header("Build location & job cost")
    include_job_cost = st.toggle(
        "Include job installation cost",
        value=False,
        help=(
            "Off by default since it needs inputs you may not know offhand: which "
            "system you'll run the job in, and that station's facility tax rate. "
            "This is a separate location from \"Where to buy\" above -- you can buy "
            "materials in one place and build somewhere else entirely."
        ),
    )
    build_system_id = None
    build_system_name = None
    facility_tax_pct = 0.0
    manufacturing_slots = 1
    reaction_slots = 1
    time_efficiency_pct = 0.0
    include_copy_cost = False
    if include_job_cost:
        build_system_query = st.text_input(
            "Build system search",
            value="Jita",
            help=(
                "Any system -- highsec, lowsec, null, or J-space/wormhole. This is "
                "just where the job runs, unrelated to \"Where to buy\" above."
            ),
        )
        system_matches = job_cost.search_systems(build_system_query) if build_system_query else []
        if system_matches:
            system_labels = [f"{name} ({sec:.2f})" for _sid, name, sec in system_matches]
            build_system_selected = st.selectbox("Build system", options=range(len(system_matches)), format_func=lambda i: system_labels[i])
            build_system_id, build_system_name, _build_system_security = system_matches[build_system_selected]
        else:
            st.warning("No system matches that search.")

        facility_tax_pct = st.number_input(
            "Facility tax %", min_value=0.0, max_value=100.0, value=0.0, step=0.1,
            help="Set by whoever owns the station/structure you build in -- 0% for your own unrigged structure, more at NPC stations or other players' structures.",
        )
        col_a, col_b = st.columns(2)
        manufacturing_slots = col_a.number_input("Manufacturing job slots", min_value=1, value=1, step=1)
        reaction_slots = col_b.number_input("Reaction job slots", min_value=1, value=1, step=1)
        time_efficiency_pct = st.number_input(
            "Time Efficiency %", min_value=0.0, max_value=20.0, value=0.0, step=2.0,
            help="TE research level of the blueprints you're building with (manufacturing only -- reactions have no TE research).",
        )

        include_copy_cost = st.toggle(
            "I'll copy a BPC instead of using the BPO directly",
            value=False,
            help=(
                "If you own a researched BPO but don't want to tie it up running the "
                "job, adds the cost to make a disposable copy with just enough runs "
                "instead. Applies to the top-level blueprint and every component "
                "chosen to build (not reactions -- reaction formulas can't be copied)."
            ),
        )

        last_industry_refresh = job_cost.last_refreshed()
        if last_industry_refresh:
            age_min = (time.time() - last_industry_refresh) / 60
            st.caption(f"Industry data last refreshed {age_min:.0f} min ago")
        else:
            st.caption("Industry data never refreshed yet")
        if st.button("Refresh Industry Data (adjusted prices + cost indices)"):
            with st.spinner("Fetching adjusted prices, system cost indices, and industry facilities from ESI..."):
                n_prices, n_indices, n_facilities = job_cost.refresh_industry_data()
            st.success(
                f"Refreshed {n_prices} adjusted prices, {n_indices} cost index rows, "
                f"{n_facilities} systems with an industry facility."
            )
            st.rerun()

    st.header("Market prices")
    st.caption(
        "Actual purchase costs come from real sell-order depth, fetched from ESI "
        "per item and auto-refreshed hourly -- no button needed for that. This "
        "refresh is just the cheap Fuzzworks liquidity scan used to decide which "
        "ore/mineral candidates are even worth checking a real order book for."
    )
    last_refresh = price_cache.last_refreshed()
    if last_refresh:
        age_min = (time.time() - last_refresh) / 60
        st.caption(f"Last refreshed {age_min:.0f} min ago")
    else:
        st.caption("Prices never refreshed yet")
    if st.button("Refresh Prices (trade hub regions)"):
        with st.spinner("Fetching prices from Fuzzworks across the 5 trade hub regions..."):
            n = price_cache.refresh_prices()
        st.cache_data.clear()  # prices changed -- don't serve stale cached optimize() results
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

# Everything below this point is the expensive part (the buy-side MILP,
# potentially 11+ solves for the comparison table). Sidebar widgets above
# still update instantly (conditional sections appearing/disappearing etc.)
# on every interaction since that's just cheap rerendering, but the actual
# recompute is gated behind this button so adjusting several sidebar options
# in a row doesn't trigger a fresh solve after each individual change --
# only once, when you're done and click it.
live_settings = dict(
    bp_type_id=bp_type_id,
    bp_name=bp_name,
    me_percent=me_percent,
    runs=runs,
    ore_mode=ore_mode,
    allow_direct_minerals=allow_direct_minerals,
    allow_build_components=allow_build_components,
    allow_build_reactions=allow_build_reactions,
    component_me_percent=component_me_percent,
    reaction_reduction_percent=reaction_reduction_percent,
    buy_mode=buy_mode,
    selected_region_names=selected_region_names,
    selected_station_id=selected_station_id,
    selected_station_label=selected_station_label,
    use_station_combo=use_station_combo,
    max_stations_combo=max_stations_combo,
    refine_pct=dict(refine_pct),
)

recalc_clicked = st.button("🔄 Recalculate", type="primary")
if recalc_clicked or "committed_settings" not in st.session_state:
    st.session_state["committed_settings"] = live_settings
    st.session_state.pop("combo_search", None)  # tied to the old settings, no longer valid

committed = st.session_state["committed_settings"]
is_stale = committed != live_settings
if is_stale:
    st.info(
        "Sidebar settings have changed since the last calculation -- showing results for the "
        "previous settings. Click \"Recalculate\" above to update."
    )

# Everything from here on uses the *committed* settings, not whatever the
# sidebar currently shows, so results stay stable until Recalculate is clicked.
bp_type_id = committed["bp_type_id"]
bp_name = committed["bp_name"]
me_percent = committed["me_percent"]
runs = committed["runs"]
ore_mode = committed["ore_mode"]
allow_direct_minerals = committed["allow_direct_minerals"]
allow_build_components = committed["allow_build_components"]
allow_build_reactions = committed["allow_build_reactions"]
component_me_percent = committed["component_me_percent"]
reaction_reduction_percent = committed["reaction_reduction_percent"]
buy_mode = committed["buy_mode"]
selected_region_names = committed["selected_region_names"]
selected_station_id = committed["selected_station_id"]
selected_station_label = committed["selected_station_label"]
use_station_combo = committed["use_station_combo"]
max_stations_combo = committed["max_stations_combo"]
refine_pct = committed["refine_pct"]

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

# The build-vs-buy decision is region-independent (see production_chain.py),
# so it's computed once here and reused for every location comparison below
# instead of every one of them redoing the same expansion + component price
# lookups.
expansion = None
if allow_build_components or allow_build_reactions:
    expansion = cached_expand(
        required, allow_build_components, allow_build_reactions,
        component_me_percent, reaction_reduction_percent,
    )

st.subheader("Compare buying locations")
st.caption(
    "Prices come from real sell-order depth (not a single blended average), fetched "
    "live from ESI per ore/mineral/component as needed -- this table runs 11 "
    "independent checks (every hub's region and station, plus the unrestricted scan "
    "across the 5 trade hub regions), so the first calculation for a given "
    "blueprint/ME/runs combination can take anywhere from a few seconds to under a "
    "minute depending on how many candidates need a fresh check. Cached after that, so "
    "tweaking other options and clicking Recalculate is fast until you change the "
    "blueprint, ME%, or runs."
)
with st.spinner("Checking real sell-order depth across regions/stations (first run only, then cached)..."):
    comparison_df = cached_comparison_table(
        required, refine_pct, ore_mode, allow_direct_minerals,
        allow_build_components, allow_build_reactions,
        component_me_percent, reaction_reduction_percent, expansion,
    )
st.dataframe(comparison_df, hide_index=True, width='stretch')

combo_search = None
is_cheapest_overall = buy_mode == buy_mode_options[0]
show_combo_section = use_station_combo or is_cheapest_overall
if show_combo_section:
    st.subheader(f"Best combo of up to {max_stations_combo} stations")
    if is_cheapest_overall and not use_station_combo:
        st.caption(
            "Shown automatically because \"Cheapest overall\" is region-wide and doesn't "
            "say where to actually go shopping -- this is the cheapest specific set of "
            "stations to visit instead. It does not change the purchase plan below, "
            "which still reflects the unrestricted scattered result; toggle \"Limit to "
            "at most N stations\" in the sidebar to use this combo as the plan instead."
        )
    if not combo_candidate_hubs:
        st.warning("No stations selected under \"Stations eligible for the combo search\" in the sidebar.")
        best_combo, all_combo_results = None, []
    else:
        with st.spinner(f"Searching combinations of up to {max_stations_combo} of {len(combo_candidate_hubs)} candidate stations..."):
            best_combo, all_combo_results = cached_best_station_combo(
                required,
                refine_pct,
                max_stations_combo,
                candidate_hubs=combo_candidate_hubs,
                ore_mode=ore_mode,
                allow_direct_minerals=allow_direct_minerals,
                allow_build_components=allow_build_components,
                allow_build_reactions=allow_build_reactions,
                component_me_percent=component_me_percent,
                reaction_reduction_percent=reaction_reduction_percent,
                pre_expanded=expansion,
            )
    combo_search = (best_combo, all_combo_results)

    if best_combo is None:
        if combo_candidate_hubs:
            st.error(f"No combination of up to {max_stations_combo} of the selected stations covers every required item.")
    else:
        st.success(f"Best: {' + '.join(best_combo.station_names)} — {best_combo.result.total_cost:,.2f} ISK")
        ranked = sorted(
            all_combo_results,
            key=lambda e: e.result.total_cost if not e.result.infeasible_reason else float("inf"),
        )
        combo_df = pd.DataFrame(
            [
                {
                    "Stations": " + ".join(e.station_names),
                    "Total Cost (ISK)": round(e.result.total_cost, 2) if not e.result.infeasible_reason else None,
                    "Status": "OK" if not e.result.infeasible_reason else "Missing coverage",
                }
                for e in ranked[:10]
            ]
        )
        st.dataframe(combo_df, hide_index=True, width='stretch')

if use_station_combo:
    if not combo_search or combo_search[0] is None:
        st.stop()
    result = combo_search[0].result
    plan_location = f"Best {len(combo_search[0].station_names)}-station combo: {' + '.join(combo_search[0].station_names)}"
else:
    with st.spinner("Building purchase plan from real sell-order depth..."):
        result = cached_optimize(
            required,
            refine_pct,
            region_names=selected_region_names,
            station_id=selected_station_id,
            station_label=selected_station_label,
            ore_mode=ore_mode,
            allow_direct_minerals=allow_direct_minerals,
            allow_build_components=allow_build_components,
            allow_build_reactions=allow_build_reactions,
            component_me_percent=component_me_percent,
            reaction_reduction_percent=reaction_reduction_percent,
            pre_expanded=expansion,
        )
    plan_location = f"{buy_mode} (single station)" if selected_station_id is not None else buy_mode

if result.infeasible_reason:
    st.error(result.infeasible_reason)
    st.stop()

# Crafted components can pull in minerals not part of the top-level blueprint's
# own requirements -- make sure their names are resolvable too.
mat_names.update(blueprint_calc.material_names(list(result.required.keys())))

st.subheader(f"Purchase plan — {plan_location}")

if result.purchases:
    st.markdown("**Ore to buy and reprocess (or mineral to buy directly, when that's cheaper)**")
    st.caption(
        "One row per item per location -- quantity and cost still reflect real "
        "sell-order depth (a location without enough cheap depth spills the rest into "
        "a higher price tier, a different ore/grade, or a different location, same as "
        "always), just combined here since multibuy fills from cheapest orders "
        "automatically up to whatever quantity you enter. \"Price Range\" shows the "
        "spread when more than one tier got combined into this row."
    )
    purchase_df = pd.DataFrame(
        [
            {
                "Ore / Mineral": row["name"],
                "Tier": row["tier"],
                "Quantity": row["quantity"],
                "Location": row["location"],
                "Avg Unit Price (ISK)": round(row["cost"] / row["quantity"], 2) if row["quantity"] else 0.0,
                "Price Range (ISK)": (
                    f"{row['min_price']:.2f}"
                    if round(row["min_price"], 2) == round(row["max_price"], 2)
                    else f"{row['min_price']:.2f} - {row['max_price']:.2f}"
                ),
                "Cost (ISK)": round(row["cost"], 2),
                "Volume (m3)": round(row["volume_m3"], 2),
            }
            for row in _merge_for_display(result.purchases)
        ]
    )
    st.dataframe(purchase_df, hide_index=True, width='stretch')

if result.component_purchases:
    st.markdown("**Components / PI / reaction materials to buy directly**")
    st.caption(
        "These aren't ore-derived, so rather than modeling their own build chain "
        "(reactions, PI, sub-components...) they're just bought outright -- cheapest "
        "real sell-order tier first, spilling into the next tier or location if one "
        "spot doesn't have enough depth, same as ore above, combined into one row per "
        "item per location the same way too."
    )
    component_df = pd.DataFrame(
        [
            {
                "Component": row["name"],
                "Quantity": row["quantity"],
                "Location": row["location"],
                "Avg Unit Price (ISK)": round(row["cost"] / row["quantity"], 2) if row["quantity"] else 0.0,
                "Price Range (ISK)": (
                    f"{row['min_price']:.2f}"
                    if round(row["min_price"], 2) == round(row["max_price"], 2)
                    else f"{row['min_price']:.2f} - {row['max_price']:.2f}"
                ),
                "Cost (ISK)": round(row["cost"], 2),
                "Volume (m3)": round(row["volume_m3"], 2),
            }
            for row in _merge_for_display(result.component_purchases)
        ]
    )
    st.dataframe(component_df, hide_index=True, width='stretch')

if result.purchases or result.component_purchases:
    st.subheader("Multibuy export")
    st.caption(
        "One block per location -- \"Item Name Quantity\" per line, ready to paste "
        "into EVE's Multi-buy window. Quantities are summed across every price tier "
        "for the same item at the same location, since multibuy doesn't care which "
        "specific order fills it, just the total. For \"Cheapest overall\" (region-wide) "
        "buying, the location shown is the region, not a specific station -- these "
        "blocks are most directly usable as-is for single-station or station-combo plans."
    )
    for location, text in sorted(_multibuy_blocks(result).items()):
        st.caption(location)
        st.code(text, language=None)

if result.build_decisions:
    st.subheader("Build vs buy decisions")
    decisions_df = pd.DataFrame(
        [
            {
                "Item": d.name,
                "Decision": d.decision,
                "Needed": d.needed_qty,
                "Runs": d.runs or None,
                "Produced": d.produced_qty or None,
                "Build Cost (ISK)": round(d.build_cost, 2) if d.build_cost is not None else None,
                "Buy Cost (ISK)": round(d.buy_cost, 2) if d.buy_cost is not None else None,
            }
            for d in result.build_decisions
        ]
    )
    st.dataframe(decisions_df, hide_index=True, width='stretch')

col1, col2 = st.columns(2)
col1.metric("Total material cost", f"{result.total_cost:,.2f} ISK")
col2.metric("Total volume", f"{result.total_volume_m3:,.2f} m3")

if include_job_cost and build_system_id is not None:
    st.subheader(f"Job installation cost — {build_system_name}")
    job_rows = []
    missing_job_data = []

    top_cost = job_cost.manufacturing_job_cost(bp_type_id, runs, build_system_id, facility_tax_pct)
    if top_cost is None:
        missing_job_data.append(bp_name)
    else:
        job_rows.append({"Item": bp_name, "Activity": "manufacturing", "Runs": runs, "Job Cost (ISK)": round(top_cost, 2)})

    for d in result.build_decisions:
        if d.decision != "build":
            continue
        producer = production_chain.get_producer(d.type_id)
        if producer is None:
            missing_job_data.append(d.name)
            continue
        if producer["activity_id"] == 1:
            cost = job_cost.manufacturing_job_cost(producer["blueprint_type_id"], d.runs, build_system_id, facility_tax_pct)
            activity = "manufacturing"
        else:
            cost = job_cost.reaction_job_cost(producer["blueprint_type_id"], d.runs, build_system_id, facility_tax_pct)
            activity = "reaction"
        if cost is None:
            missing_job_data.append(d.name)
            continue
        job_rows.append({"Item": d.name, "Activity": activity, "Runs": d.runs, "Job Cost (ISK)": round(cost, 2)})

    if job_rows:
        job_df = pd.DataFrame(job_rows)
        st.dataframe(job_df, hide_index=True, width='stretch')
        total_job_cost = sum(r["Job Cost (ISK)"] for r in job_rows)
        st.metric("Total job installation cost", f"{total_job_cost:,.2f} ISK")
    if missing_job_data:
        st.warning(
            "No adjusted-price/cost-index data for: " + ", ".join(missing_job_data)
            + " -- click \"Refresh Industry Data\" if you haven't yet."
        )

    st.subheader("Find a cheaper highsec build system")
    st.caption(
        "Scans highsec systems that currently have a public NPC station offering "
        "industry (not every station does, and player structures aren't guessable -- "
        "so only real, usable options show up) and ranks the cheapest cost index, "
        "tagged with jump distance from your build system above via a highsec-only "
        "(\"secure\") route -- a system with no such route (rare) just won't appear. "
        "Restricted to highsec since this is a \"recommend something safe\" "
        "convenience; the search box above accepts any system, low/null/J-space "
        "included, if you already know where you're going."
    )
    rec_col1, rec_col2, rec_col3 = st.columns([2, 1, 1])
    rec_activity_label = rec_col1.selectbox("Rank by", options=["Manufacturing", "Reaction"], key="rec_activity")
    rec_max_jumps = rec_col2.number_input("Max jumps (0 = no limit)", min_value=0, value=20, step=5, key="rec_max_jumps")
    rec_col3.write("")  # vertical spacer to align the button with the inputs
    rec_col3.write("")
    if rec_col3.button("Find recommendations"):
        rec_activity = "manufacturing" if rec_activity_label == "Manufacturing" else "reaction"
        with st.spinner(f"Scanning highsec systems within {rec_max_jumps or 'any number of'} jumps..."):
            recs = job_cost.recommend_build_systems(
                rec_activity, build_system_id, max_jumps=rec_max_jumps or None, top_n=10
            )
        st.session_state["system_recs"] = (rec_activity_label, recs)
    stored_recs = st.session_state.get("system_recs")
    if stored_recs:
        rec_label, recs = stored_recs
        if not recs:
            st.info("No highsec systems found within that jump range (or no cost-index data -- try Refresh Industry Data).")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"System": r.system_name, "Jumps": r.jumps, f"{rec_label} Cost Index": round(r.cost_index, 4)}
                        for r in recs
                    ]
                ),
                hide_index=True,
                width='stretch',
            )

    if include_copy_cost:
        st.subheader("BPC copying cost")
        st.caption(
            "Lower confidence than the job cost above -- the documented formula for "
            "copying is less well established. Treat as directional."
        )
        copy_rows = []
        top_copy = job_cost.copy_job_cost(bp_type_id, runs, build_system_id, facility_tax_pct)
        if top_copy is not None:
            copy_rows.append({"Item": bp_name, "Runs": runs, "Copy Cost (ISK)": round(top_copy, 2)})
        for d in result.build_decisions:
            if d.decision != "build":
                continue
            producer = production_chain.get_producer(d.type_id)
            if producer is None or producer["activity_id"] != 1:
                continue  # reactions can't be copied
            c = job_cost.copy_job_cost(producer["blueprint_type_id"], d.runs, build_system_id, facility_tax_pct)
            if c is not None:
                copy_rows.append({"Item": d.name, "Runs": d.runs, "Copy Cost (ISK)": round(c, 2)})
        if copy_rows:
            copy_df = pd.DataFrame(copy_rows)
            st.dataframe(copy_df, hide_index=True, width='stretch')
            st.metric("Total BPC copying cost", f"{sum(r['Copy Cost (ISK)'] for r in copy_rows):,.2f} ISK")

    st.subheader("Estimated build time")
    time_estimate = job_cost.estimate_build_time(
        bp_type_id, runs, result.build_decisions, manufacturing_slots, reaction_slots, time_efficiency_pct
    )
    tcol1, tcol2, tcol3 = st.columns(3)
    tcol1.metric("Reaction phase", _format_duration(time_estimate.reaction_phase_seconds))
    tcol2.metric("Manufacturing phase", _format_duration(time_estimate.manufacturing_phase_seconds))
    tcol3.metric("Total (sequential phases)", _format_duration(time_estimate.total_seconds))
    if time_estimate.jobs:
        with st.expander("Per-job breakdown"):
            jobs_df = pd.DataFrame(
                [
                    {"Item": j.label, "Activity": j.activity, "Duration": _format_duration(j.seconds)}
                    for j in time_estimate.jobs
                ]
            )
            st.dataframe(jobs_df, hide_index=True, width='stretch')
    if time_estimate.missing_time_data:
        st.warning("No job time data for: " + ", ".join(time_estimate.missing_time_data))

if result.waste_qty:
    st.subheader("Waste (leftover minerals above requirement)")
    waste_df = pd.DataFrame(
        [
            {
                "Mineral": mat_names.get(mid, str(mid)),
                "Produced": round(result.produced.get(mid, 0), 1),
                "Required": result.required.get(mid, 0),
                "Leftover": round(result.waste_qty.get(mid, 0), 1),
                "Leftover Value (ISK)": round(result.waste_isk.get(mid, 0), 2),
            }
            for mid in result.waste_qty
        ]
    ).sort_values("Leftover Value (ISK)", ascending=False)
    st.dataframe(waste_df, hide_index=True, width='stretch')
    st.metric("Total waste value", f"{result.total_waste_isk:,.2f} ISK")
