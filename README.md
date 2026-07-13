# EVE Industry Calculator

A local tool for EVE Online industry: pick a blueprint, set its Material Efficiency and
run count, and it works out the cheapest way to source every material -- ore to
reprocess, or minerals/components to buy outright -- across the major highsec trade
hubs, a single station, or scattered across all of highsec for the absolute minimum.
It also tracks cargo volume, and can optionally estimate the job installation fee,
BPC copying cost, and wall-clock build time for actually running the job.

Runs entirely on your own machine as a local web app (via [Streamlit](https://streamlit.io)).
The only network calls it makes are to [Fuzzwork's](https://www.fuzzwork.co.uk) EVE
Static Data Export/market price mirrors and to [ESI](https://esi.evetech.net) (EVE's
official API, for job-cost-related data), and only when you explicitly ask it to
(first-time setup, or clicking one of the "Refresh" buttons).

## Requirements

- Windows or macOS
- Python 3.11 or newer

## 1. Install Python (skip if you already have it)

**Windows:**
1. Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest Windows installer.
2. Run it. **On the first screen, check the box "Add python.exe to PATH"** before clicking Install Now -- this is what lets Windows find Python when you double-click the setup scripts below.
3. To check it worked: open Start, type `cmd`, hit Enter, and type `python --version`. It should print a version number. (You won't need the command prompt again after this.)

**macOS:**
1. Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest macOS installer (or `brew install python3` if you use Homebrew).
2. Run the installer as normal.
3. To check it worked: open Terminal (Spotlight → "Terminal") and type `python3 --version`. It should print a version number.
4. Get the project onto the Mac, either way works:
   - **`git clone`** (recommended -- no further Terminal steps needed): the double-click
     scripts (`.command` files) need their Unix "executable" bit set, and `git clone`
     preserves that bit automatically since git tracks it as part of the repo. Skip
     straight to step 2 below.
   - **Download a zip and unzip it**: zip doesn't preserve that executable bit (especially
     a zip built on Windows -- NTFS has no such bit to preserve in the first place), so
     you'll need to set it yourself, once: open Terminal, `cd` to wherever you unzipped
     the project, and run `chmod +x install.command launch_indycalc.command`.
   - Either way, the very first time you double-click one of the `.command` files,
     macOS Gatekeeper will likely warn that it's from an "unidentified developer" --
     right-click (or Control-click) the file and choose **Open** instead of
     double-clicking, once, to approve it. No Terminal needed for that part.

## 2. First-time setup

Double-click **`install.pyw`** (Windows) or **`install.command`** (macOS).

A small window opens and, with no further input needed:
- installs the Python packages this app needs (Streamlit, SciPy, pandas, requests)
- downloads EVE's static data (blueprints, ore reprocessing yields, regions) into a local cache
- fetches an initial set of market prices across the highsec trade regions

This takes a couple of minutes the first time (mostly the static data download). It's
safe to re-run any time -- for example after a game update, to refresh the static data.
If anything fails, the window will say so and point you at `install.log` for details.
(On macOS a Terminal window will flash briefly as `install.command` hands off to the
actual installer -- that's expected, not an error.)

## 3. Running the app

Double-click **`launch_indycalc.pyw`** (Windows) or **`launch_indycalc.command`**
(macOS). It starts the local server, opens it in your browser, and shows a small "EVE
Industry Calculator" control window (in the taskbar on Windows, the Dock on macOS). To
stop the server, either click "Stop Server" in that window or just close it -- there's
no separate stop script to hunt down.

## What each part does

| File | Purpose |
|---|---|
| `install.pyw` / `install.command` | One-time (or re-run anytime) setup: installs dependencies, builds the local data cache. The `.command` file is just a thin macOS double-click shim around the same `.pyw` script -- see its comments for why that works. |
| `launch_indycalc.pyw` / `launch_indycalc.command` | Day-to-day launcher with a taskbar/Dock control window (start/open/stop). Both platforms share one script, branching internally on `sys.platform` for the handful of OS-specific bits (how the server process is detached, how it's killed). |
| `requirements.txt` | The Python packages `install.pyw` installs. |
| `indycalc/app.py` | The Streamlit UI -- everything you interact with in the browser. |
| `indycalc/sde_loader.py` | Downloads EVE's Static Data Export (blueprints, ore, regions) from Fuzzwork and builds the local `sde.db` cache. Run manually (`python -m indycalc.sde_loader`) to refresh after a game update. |
| `indycalc/ore_tiers.py` | Maps ore names to their reprocessing rig tier (Simple/Coherent/Variegated/Complex/Abyssal/Mercoxit/Erratic) and holds the default refine % shown in the UI. |
| `indycalc/blueprint_calc.py` | Blueprint search, and the ME%/run-count math for required material quantities. |
| `indycalc/price_cache.py` | Fetches and caches sell prices from Fuzzwork's market aggregates across highsec regions *and* the 5 major hub stations, on demand only. Also defines the 8 standard minerals, the 5 major trade hubs, and their station IDs. |
| `indycalc/optimizer.py` | The actual optimization: a mixed-integer program that picks the cheapest combination of ore batches (and/or direct mineral purchases) to cover required minerals, plus market pricing for non-ore components -- from a region, a single station, or unrestricted. |
| `indycalc/production_chain.py` | Build-vs-buy: expands components/reaction materials into their own sub-materials when that's cheaper than buying them outright. See "Building your own components/reactions" below. |
| `indycalc/job_cost.py` | Job installation fee, BPC copying cost, and build-time estimates -- pulls adjusted prices and system cost indices from ESI. See "Job installation cost, BPC copying, and build time" below. |
| `indycalc/db.py` | Shared SQLite connection helper (WAL mode + busy timeout, so the app doesn't choke if two things touch the database at once). |
| `indycalc/data/sde.db` | The local SQLite cache -- not checked into git, rebuilt by `sde_loader.py`/`price_cache.py`. |

## How it decides what to buy

For each material a blueprint needs (after applying ME% and run count):

- **The 8 standard minerals** (Tritanium, Pyerite, Mexallon, Isogen, Nocxium, Zydrine,
  Megacyte, Morphite) can come from reprocessing ore, or be bought directly on the
  market. The optimizer solves an integer program (not just a continuous approximation)
  because ore can only be reprocessed in whole batches -- it has to decide *whole batch
  counts* per ore type, not fractional quantities that get rounded up afterward and
  potentially blow the budget on an unlucky rounding. It'll mix strategies: bulk ore for
  the minerals you need a lot of, a direct buy for a small leftover amount of a rare one
  (e.g. 1 unit of Morphite is usually far cheaper to buy outright than reprocessing a
  whole 100-unit Mercoxit batch for it). The **"No direct mineral purchases"** toggle
  forces ore-only sourcing if you'd rather see that cost.
- **Everything else** -- Tech II components, PI materials, reaction intermediates,
  salvage -- is priced at the cheapest cached market sell price by default. Turn on
  "Craft components myself" / "Craft reaction materials myself" to have the tool also
  price *building* these and use whichever is cheaper -- see below.
- **Ore sourcing** can be restricted to raw, compressed only, or either. Compressed ore
  reprocesses into the same minerals as raw but takes roughly 1/100th the cargo volume,
  usually for a small premium -- worth comparing if you're hauling.
- A single troll sell order (a handful of units at a giveaway price) is ignored --
  prices only count if there's real listed volume and more than one order behind them.
- **A location that's missing a price for anything required makes that whole location
  infeasible, never a silent $0.** This applies everywhere -- a single station, a
  region, or the unrestricted "cheapest overall" scan. Otherwise a station that simply
  doesn't sell some item would look artificially cheap (missing = free) and could get
  ranked as the best place to buy everything, when it's actually missing something.

## Building your own components/reactions

Off by default (everything is bought). Two independent toggles, each comparing build
cost vs. buy cost and picking the cheaper:

- **Craft components myself**: a component (e.g. a Tech II item) has its own
  Manufacturing blueprint. When on, each non-mineral top-level requirement is priced
  both as "buy it" and as "build it from its own materials at the ME% you set," and
  whichever wins is used. Minerals pulled in this way are folded into the same ore
  purchase plan as the ship's own minerals, so they compete for the same bulk buy.
- **Craft reaction materials myself**: a reaction material (e.g. Fernite Carbide) has
  its own Reaction formula instead of a Manufacturing blueprint, and reactions have no
  ME research -- the % you set represents a refinery Reaction rig bonus instead, if you
  have one. This only kicks in for reaction materials pulled in *while building a
  component* (so it does nothing unless "Craft components myself" is also on and finds
  something worth building).

**Scope is deliberately capped at these two levels.** Raw reaction inputs (fuel blocks,
moon materials, gas, PI) and anything nested deeper than that are always bought
directly -- modeling their own production chains would be a much bigger undertaking.
Reactions produce in large fixed batches (often thousands of units per run), so
whether building one is worth it depends heavily on scale: needing a handful of units
almost always loses to buying, needing enough to fill most of a batch often wins. The
"Build vs buy decisions" table in the results shows exactly which way each call went
and why.

The build-vs-buy comparison itself uses a quick, region-independent price estimate
(not the same batch-optimized ore MILP used for the final purchase plan) -- see the
simplification note below.

## Buying from a single station

Region-level pricing ("Buy from" a hub) can still mean hauling between several
different systems within that region. The "Compare buying locations" table also shows
a "Single Station?" column and cost -- whether *every* required item is actually
liquid at that hub's one busiest trade station (Jita 4-4, Amarr's Emperor Family
Academy, etc.), for a genuinely single stop. If a hub shows "No" there, something
needed isn't sold at that specific station in enough volume; the region-wide number is
still valid, just not a one-stop trip. Toggle "Buy everything from `<hub>`'s station"
once you've confirmed it says "Yes" to actually price the plan that way.

## Buying from a combination of a few stations

A middle ground between "one station" (most convenient, but sometimes pricier or
infeasible) and "scattered across all of highsec" (cheapest, but could mean many
stops: toggle "Limit to at most N stations" and pick N. Clicking "Find best combo"
brute-forces every combination of up to N of the 5 hub stations (at most
C(5,1)+...+C(5,5) = 31 combos, a few seconds), and uses whichever combination is
cheapest while covering every required item -- showing a ranked table of the combos
tried, and the winning purchase plan tagged with exactly which station to buy each
item at. Only the 5 major hub stations are candidates; this tool doesn't have
station-level price data anywhere else, and realistically those are the only stations
liquid enough for bulk purchases anyway. The search only runs when you click the
button (not on every page interaction), since it's slower than the other modes.

## Job installation cost, BPC copying, and build time

All off by default -- "Include job installation cost" in the sidebar turns them on.
This uses a **separate location from "Where to buy"**: which system you'll actually
run the job in, since there's no reason your buy and build locations have to match.

- **Job installation fee**: `EIV x (system cost index + facility tax % + 4% SCC
  surcharge)`, computed for the top-level blueprint and every component/reaction
  chosen to build. EIV comes from ESI's adjusted prices (a slower-moving reference
  price CCP publishes, not the live sell price already used elsewhere) times the
  blueprint's *unresearched* material quantities -- the fee doesn't get cheaper just
  because you researched ME. System cost index is per-system, per-activity, and
  fetched from ESI too (`/industry/systems/`) -- some systems (Jita notably) have a
  much higher manufacturing index than others precisely because they're so busy.
  Facility tax is set by whoever owns the station/structure and can't be looked up
  generically, so it's a number you enter.
- **BPC copying cost**: if you'd rather run the job off a disposable copy than tie up
  a researched BPO, this estimates that copy's cost, scaled by however many runs it
  needs. **Lower confidence than the job fee above** -- the documented formula for
  copying is less well established publicly than manufacturing's, so treat it as
  directional. In practice this tends to come out very cheap (a few ISK to low
  thousands) which does match how EVE players generally talk about copying -- the real
  cost of copying is time, not ISK.
- **Build time**: given how many manufacturing job slots and reaction job slots you
  have (separate skills/slot pools in EVE), schedules every job (top-level blueprint +
  everything chosen to build) across those slots and estimates wall-clock completion
  time. Manufacturing and reaction jobs are scheduled independently, then summed as
  sequential phases (reactions must finish before a component that needs them can
  start, but this tool doesn't track *which* component needs *which* reaction, so it
  conservatively assumes the whole reaction phase finishes before manufacturing
  starts -- this can overestimate total time, never underestimate it). Time Efficiency
  % applies to manufacturing jobs only; reactions have no TE research, only an
  unmodeled refinery duration rig bonus.

## Performance: caching and the Recalculate button

Comparing every buying location involves 11+ independent MILP solves (region and
station cost for every hub, plus the unrestricted scan), and the build-vs-buy
expansion does its own set of price lookups on top of that -- expensive enough that
naively redoing it on every sidebar interaction (Streamlit reruns the whole script on
every widget change) would make the app feel sluggish. Two things keep that in check:

- **Sidebar changes don't recompute anything by themselves.** Everything below the
  blueprint header uses whatever settings you last clicked **"🔄 Recalculate"** with,
  not whatever the sidebar currently shows -- so you can change the blueprint, ME%,
  buy location, and every toggle in one go without triggering a solve after each
  individual change. If the sidebar has drifted from what's currently displayed, a
  banner says so.
- **Results are cached** (`st.cache_data`) keyed on the exact settings used, so
  clicking Recalculate with settings you've already computed before returns instantly
  instead of re-solving. The cache is cleared automatically whenever you refresh
  market prices, so it can't serve stale prices.
- The build-vs-buy expansion itself is computed once per Recalculate (not once per
  comparison-table row) since its result doesn't depend on which location you're
  pricing from -- see "Building your own components/reactions" above.

**Not parallelized, deliberately.** An earlier version ran the comparison table's
independent solves concurrently across threads for a real wall-clock speedup, but
scipy's HiGHS MILP solver has documented segfaults/assertion failures tied to its own
internal threading (upstream scipy issues #17220, #17250, #22188) -- calling it from
several Python threads at once risked crashing the whole process with no Python
traceback, which is exactly what happened during testing. It's sequential now; caching
and the Recalculate gate are what actually keep it fast, not concurrency.

## Data sources and refresh cadence

- **Static data** (blueprints, ore reprocessing yields, regions, systems, job times):
  from Fuzzwork's SDE CSV dumps. Barely changes; only refresh via `install.pyw` or
  `python -m indycalc.sde_loader` after a game update.
- **Market prices**: from Fuzzwork's market aggregates API, one batched call per highsec
  region *and* per hub station (never per item), and only when you click "Refresh
  Prices" -- never automatically, to avoid hammering their servers.
- **Industry data** (adjusted prices, system cost indices, for job cost estimates):
  from ESI directly, two global calls (not per-region/per-item), only when you click
  "Refresh Industry Data."
- Regions covered are the ~19 that are (almost) entirely highsec. The five major trade
  hubs (Jita, Amarr, Dodixie, Rens, Hek) map to both their dominant region and their
  actual trade station (IDs looked up from the SDE, not guessed).

## Known simplifications

- Region-level price aggregation can't exclude the handful of lowsec systems inside an
  otherwise-highsec region.
- Build-vs-buy for components/reactions is capped at two levels deep (see above) --
  raw reaction inputs and deeper nested components always buy, never build.
- The build-vs-buy cost estimate uses direct market prices everywhere (no region
  restriction, no ore-batch optimization), which is a conservative estimate of true
  build cost -- real bulk ore reprocessing is usually cheaper than this estimate
  implies, so it won't wrongly favor "build" when "buy" is actually better, but it may
  occasionally favor "buy" when a fully-optimized build would have won by a small
  margin. The final mineral purchase plan (once build/buy is decided) *is* the full
  batch-optimized MILP across the whole tree, ship + built components together.
- Waste (leftover minerals above what's required) is valued at the current cheapest
  market sell price, not what you'd actually realize reprocessing/selling it.
- BPC copying cost formula is lower-confidence than the other cost math -- see above.
- Build time assumes reactions fully complete before manufacturing starts (safe
  overestimate, not dependency-aware) and doesn't model character skill bonuses to
  job time, only TE research and job slot count.

## Troubleshooting

- **"No local SDE database found"**: run `install.pyw` (or `python -m indycalc.sde_loader`).
- **Prices missing / "click Refresh Prices first"**: open the app, click "Refresh Prices" in the sidebar.
- **The page looks frozen -- nothing happens when you change anything**: this almost
  always means the server itself stopped, not that the app is unresponsive. Check
  whether the "EVE Industry Calculator" window is still in the taskbar/Dock; if it's
  gone, double-click `launch_indycalc.pyw`/`launch_indycalc.command` again. Once it's
  confirmed running (browser reloads and works normally), remember sidebar changes
  need a click on "🔄 Recalculate" to take effect -- see "Performance" above.
- **macOS: double-clicking a `.command` file does nothing, or opens it in a text
  editor**: the executable bit likely isn't set -- run `chmod +x install.command
  launch_indycalc.command` once in Terminal (see step 1 above).
- **macOS: "\<file\> cannot be opened because it is from an unidentified developer"**:
  expected for any unsigned script the first time. Control-click (or right-click) the
  file and choose **Open** instead of double-clicking; after that first approval,
  double-clicking works normally.
- Logs: `launcher.log` and `streamlit_server.log` (from `launch_indycalc.pyw`/`.command`), `install.log` (from `install.pyw`/`.command`).
