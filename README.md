# CSO Datasette

A local [Datasette](https://datasette.io/) explorer for Central Statistics Office (Ireland) data from [PxStat](https://data.cso.ie/), with an AI agent for natural-language querying, computation, and charting.

Serves three databases:

| Database | Contents |
|---|---|
| `curated` | Pre-loaded CSO datasets + full PxStat catalog + subject tree (persistent, refreshed via ETL) |
| `search` | Full-text search index over the full PxStat catalog |
| `user_tables` | Tables you load on demand — in-memory, reset on restart |

---

## Requirements

- [uv](https://docs.astral.sh/uv/) — handles the Python environment and dependencies
- An LLM API key — stored via `llm keys set openrouter` (currently using **deepseek-v4-flash** via OpenRouter)

---

## Setup

```bash
uv sync
```

### First-time ETL

**1. Build the catalog, FTS index, and subject tree** — fetches the full CSO PxStat catalog (~12,600 tables) into `curated.db`, builds the `search.db` full-text index, and stores the Theme → Subject navigation tree:

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

## Loading tables on demand (`/-/pxstat`)

Go to **Load PxStat Table** in the nav menu. You can:

- Type a matrix code directly (e.g. `HPM09`) and click **Load table**
- **Browse by theme** — 9 CSO themes (Business Sectors, Census, Economy, …) with subject chips beneath each; click a subject to load its tables into the results list
- Search the full catalog by keyword — filters whichever view is active

Loaded tables appear in `user_tables` with their CSO description shown beneath the table name. They are available for SQL queries and joins until the server restarts.

---

## AI Agent (`/-/agent`)

The agent has access to the following tools:

| Tool | Purpose |
|---|---|
| `search_pxstat_catalog` | Full-text search across 12,000+ PxStat table titles — use this to find relevant tables before loading |
| `load_pxstat_table` | Fetch a PxStat table by code and load it into `user_tables` |
| `sql_query` | Run SQL against any database |
| `suggest_pxstat_joins` | Find joinable columns across loaded PxStat tables |
| `execute_micropython` | Run sandboxed Python (MicroPython/WASM) for computation SQL can't express — derived stats, growth rates, transformations |
| `render_chart` | Generate Observable Plot charts (bar, line, dot, area, waffle) from SQL query results |

### Typical agent workflow

1. **Find** — `search_pxstat_catalog("housing prices")` → returns ranked codes and titles
2. **Load** — `load_pxstat_table("HPM09")` → table available in `user_tables`
3. **Query** — `sql_query` to filter, aggregate, or join
4. **Compute** — `execute_micropython` for derived statistics (year-on-year % change, rolling averages, etc.)
5. **Visualise** — `render_chart` to draw a bar or line chart inline

The MicroPython sandbox has no network or filesystem access. It supports `math`, `json`, `re`, and can query Datasette databases via the built-in `read_only_sql_query()` helper.

---

## Project structure

```
CSO_datasette/
├── etl/
│   ├── build_catalog.py     # fetch catalog + build FTS index + subject tree
│   └── load_curated.py      # fetch curated datasets into curated.db
├── plugins/
│   └── datasette_pxstat.py  # /-/pxstat routes + agent tools
├── static/
│   └── custom.css           # custom theme
├── templates/
│   ├── base.html            # site-wide layout override
│   └── pxstat_index.html    # PxStat loader page (theme browser + catalog search)
├── datasette.yml            # Datasette config (settings, plugin config)
├── dogsheep-beta.yml        # FTS index config
├── pyproject.toml
├── run.ps1                  # serve command (PowerShell)
└── run.sh                   # serve command (bash)
```
