# CSO Datasette

A local [Datasette](https://datasette.io/) explorer for Central Statistics Office (Ireland) data from [PxStat](https://data.cso.ie/).

Serves three databases:

| Database | Contents |
|---|---|
| `curated` | Pre-loaded CSO datasets (persistent, refreshed via ETL) |
| `search` | Full-text search index over the full PxStat catalog |
| `user_tables` | Tables you load on demand — in-memory, reset on restart |

---

## Requirements

- [uv](https://docs.astral.sh/uv/) — handles the Python environment and dependencies
- A Gemini API key (for the LLM query assistant) — stored via `llm keys set gemini`

---

## Setup

```bash
uv sync
```

### First-time ETL

**1. Build the catalog and FTS index** — fetches the full CSO PxStat catalog (~12,600 tables) into `curated.db` and builds the `search.db` full-text index:

```bash
uv run python etl/build_catalog.py
```

**2. Load curated tables** — fetches the pre-selected datasets from PxStat and writes them to `curated.db`:

```bash
uv run python etl/load_curated.py
```

Re-run either script any time to refresh the data.

---

## Run

**Windows (PowerShell):**
```powershell
.\run.ps1
```

**Mac / Linux / WSL:**
```bash
bash run.sh
```

Then open <http://127.0.0.1:8001>.

The `--root` flag prints a one-time login URL in the terminal on startup — use it to authenticate as the root actor.

---

## What's loaded

### Curated tables (`/curated`)

| Code | Description |
|---|---|
| E2003 | Population Estimates by Age, Sex and Year |
| PEA01 | Population by Region and Year |
| QLF01 | Quarterly Labour Force Survey — Employment & Unemployment |
| MUM01 | Monthly Unemployment Estimates |
| HPM09 | Residential Property Price Index |
| CPM01 | Consumer Price Index by Category and Month |
| IIA01 | Survey on Income and Living Conditions |
| TOA01 | Overseas Travel Arrivals by Country of Residence |
| TSM06 | Merchandise Trade — Imports and Exports |
| AAA23 | Area, Yield and Production of Crops |

### Full-text search (`/-/beta`)

`search.db` is a [dogsheep-beta](https://github.com/dogsheep/dogsheep-beta) FTS5 index over all ~12,600 PxStat table titles. Use it to find matrix codes to load.

---

## Loading tables on demand

Go to **Load PxStat Table** in the nav menu (or `/-/pxstat`). You can:

- Type a matrix code directly (e.g. `HPM09`) and click **Load table**
- Browse and search the full catalog, then click **Load →** on any row

Loaded tables appear in the `user_tables` database and are available for SQL queries and joins until the server restarts.

---

## Project structure

```
CSO_datasette/
├── etl/
│   ├── build_catalog.py   # fetch catalog + build FTS index
│   └── load_curated.py    # fetch curated datasets into curated.db
├── plugins/
│   └── datasette_pxstat.py  # /-/pxstat routes + agent tools
├── static/
│   └── custom.css         # custom theme
├── templates/
│   ├── base.html          # site-wide layout override
│   └── pxstat_index.html  # PxStat loader page
├── datasette.yml          # Datasette config (settings, plugin config)
├── dogsheep-beta.yml      # FTS index config
├── pyproject.toml
├── run.ps1                # serve command (PowerShell)
└── run.sh                 # serve command (bash)
```
