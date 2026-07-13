# EVE Industry Calculator

A local tool for EVE Online industry: pick a blueprint, set its Material Efficiency and
run count, and it works out the cheapest way to source every material -- ore to
reprocess, or minerals/components to buy outright -- across the major highsec trade
hubs (or scattered across all of highsec for the absolute minimum). It also tracks
cargo volume, so you can weigh "cheapest" against "fits in one hauling trip."

Runs entirely on your own machine as a local web app (via [Streamlit](https://streamlit.io)).
The only network calls it makes are to [Fuzzwork's](https://www.fuzzwork.co.uk) EVE
Static Data Export and market price mirrors, and only when you explicitly ask it to
(first-time setup, or clicking "Refresh Prices").

## Requirements

- Windows
- Python 3.11 or newer

## 1. Install Python (skip if you already have it)

1. Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest Windows installer.
2. Run it. **On the first screen, check the box "Add python.exe to PATH"** before clicking Install Now -- this is what lets Windows find Python when you double-click the setup scripts below.
3. To check it worked: open Start, type `cmd`, hit Enter, and type `python --version`. It should print a version number. (You won't need the command prompt again after this.)

## 2. First-time setup

Double-click **`install.pyw`**.

A small window opens and, with no further input needed:
- installs the Python packages this app needs (Streamlit, SciPy, pandas, requests)
- downloads EVE's static data (blueprints, ore reprocessing yields, regions) into a local cache
- fetches an initial set of market prices across the highsec trade regions

This takes a couple of minutes the first time (mostly the static data download). It's
safe to re-run any time -- for example after a game update, to refresh the static data.
If anything fails, the window will say so and point you at `install.log` for details.

## 3. Running the app

Double-click **`launch_indycalc.pyw`**. It starts the local server, opens it in your
browser, and shows a small "EVE Industry Calculator" window in the taskbar. To stop the
server, either click "Stop Server" in that window or just close it -- there's no
separate stop script to hunt down.

## What each part does

| File | Purpose |
|---|---|
| `install.pyw` | One-time (or re-run anytime) setup: installs dependencies, builds the local data cache. |
| `launch_indycalc.pyw` | Day-to-day launcher with a taskbar control window (start/open/stop). |
| `requirements.txt` | The Python packages `install.pyw` installs. |
| `indycalc/app.py` | The Streamlit UI -- everything you interact with in the browser. |
| `indycalc/sde_loader.py` | Downloads EVE's Static Data Export (blueprints, ore, regions) from Fuzzwork and builds the local `sde.db` cache. Run manually (`python -m indycalc.sde_loader`) to refresh after a game update. |
| `indycalc/ore_tiers.py` | Maps ore names to their reprocessing rig tier (Simple/Coherent/Variegated/Complex/Abyssal/Mercoxit/Erratic) and holds the default refine % shown in the UI. |
| `indycalc/blueprint_calc.py` | Blueprint search, and the ME%/run-count math for required material quantities. |
| `indycalc/price_cache.py` | Fetches and caches sell prices from Fuzzwork's market aggregates across highsec regions, on demand only. Also defines the 8 standard minerals and the 5 major trade hubs. |
| `indycalc/optimizer.py` | The actual optimization: a mixed-integer program that picks the cheapest combination of ore batches (and/or direct mineral purchases) to cover required minerals, plus market pricing for non-ore components. |
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
  salvage -- has its own multi-step build chain that may or may not beat buying the
  finished item. Rather than modeling that whole tree, these are just priced at the
  cheapest cached market sell price and added to the total directly.
- **Ore sourcing** can be restricted to raw, compressed only, or either. Compressed ore
  reprocesses into the same minerals as raw but takes roughly 1/100th the cargo volume,
  usually for a small premium -- worth comparing if you're hauling.
- A single troll sell order (a handful of units at a giveaway price) is ignored --
  prices only count if there's real listed volume and more than one order behind them.

## Data sources and refresh cadence

- **Static data** (blueprints, ore reprocessing yields, regions): from Fuzzwork's SDE
  CSV dumps. Barely changes; only refresh via `install.pyw` or
  `python -m indycalc.sde_loader` after a game update.
- **Market prices**: from Fuzzwork's market aggregates API, one batched call per highsec
  region (never per item), and only when you click "Refresh Prices" -- never
  automatically, to avoid hammering their servers.
- Regions covered are the ~19 that are (almost) entirely highsec. The five major trade
  hubs (Jita, Amarr, Dodixie, Rens, Hek) are each just their dominant region, since
  region-level pricing can't isolate a single station.

## Known simplifications

- Region-level price aggregation can't exclude the handful of lowsec systems inside an
  otherwise-highsec region.
- Component/PI/reaction-material build chains aren't modeled -- they're always priced
  as a direct market buy, even if building them yourself would be cheaper.
- Waste (leftover minerals above what's required) is valued at the current cheapest
  market sell price, not what you'd actually realize reprocessing/selling it.

## Troubleshooting

- **"No local SDE database found"**: run `install.pyw` (or `python -m indycalc.sde_loader`).
- **Prices missing / "click Refresh Prices first"**: open the app, click "Refresh Prices" in the sidebar.
- Logs: `launcher.log` and `streamlit_server.log` (from `launch_indycalc.pyw`), `install.log` (from `install.pyw`).
