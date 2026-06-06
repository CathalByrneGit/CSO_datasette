"""
datasette-pxstat: load CSO PxStat tables on demand into an in-memory database.

Routes added:
  GET  /-/pxstat            catalog browser + load form
  GET  /-/pxstat/catalog    proxy CSO catalog JSON to the browser
  POST /-/pxstat/load       fetch a table by code and redirect to it

Agent tools (when datasette-agent is installed):
  search_pxstat_catalog     search the catalog for table codes by keyword
  suggest_pxstat_joins      find joinable columns across loaded tables
  load_pxstat_table         load a PxStat table by code from within the agent
"""

import csv
import io
import json
import re

import httpx
from datasette import hookimpl, Response

try:
    from datasette_agent.tools import AgentTool
    _AGENT_AVAILABLE = True
except ImportError:
    _AGENT_AVAILABLE = False

CATALOG_URL = "https://ws.cso.ie/public/api.jsonrpc"
DATASET_URL = (
    "https://ws.cso.ie/public/api.restful"
    "/PxStat.Data.Cube_API.ReadDataset/{code}/CSV/1.0/en"
)


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------

@hookimpl
def startup(datasette):
    """Create the in-memory database for user-loaded tables on server start."""
    datasette.add_memory_database("user_tables")


@hookimpl
def register_routes():
    return [
        (r"^/-/pxstat$", pxstat_index),
        (r"^/-/pxstat/catalog$", pxstat_catalog),
        (r"^/-/pxstat/load$", pxstat_load),
    ]


@hookimpl
def menu_links(datasette, actor):
    return [{"href": "/-/pxstat", "label": "Load PxStat Table"}]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def pxstat_index(datasette, request):
    db = datasette.get_database("user_tables")
    loaded_tables = await db.table_names()
    has_curated = "curated" in datasette.databases
    prefill_code = request.args.get("code", "").strip().upper()

    return Response.html(
        await datasette.render_template(
            "pxstat_index.html",
            {
                "loaded_tables": loaded_tables,
                "has_curated": has_curated,
                "prefill_code": prefill_code,
            },
            request=request,
        )
    )


async def pxstat_catalog(datasette, request):
    """Proxy the CSO ReadCollection call so the browser avoids CORS issues."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                CATALOG_URL,
                json={
                    "jsonrpc": "2.0",
                    "method": "PxStat.Data.Cube_API.ReadCollection",
                    "params": {"language": "en"},
                },
            )
        return Response.json(resp.json())
    except Exception as exc:
        return Response.json({"error": str(exc)}, status=500)


async def pxstat_load(datasette, request):
    if request.method != "POST":
        return Response.redirect("/-/pxstat")

    post_vars = await request.post_vars()
    code = re.sub(r"[^A-Z0-9]", "", post_vars.get("code", "").strip().upper())

    if not code:
        return _error_response("Please supply a table code (e.g. NPA03).")

    url = DATASET_URL.format(code=code)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        return _error_response(f"Request timed out fetching table {code}.")
    except httpx.RequestError as exc:
        return _error_response(f"Network error: {exc}")

    if resp.status_code == 404:
        return _error_response(f"Table <strong>{code}</strong> was not found on PxStat.")
    if resp.status_code != 200:
        return _error_response(f"CSO API returned HTTP {resp.status_code} for table {code}.")

    # Decode with utf-8-sig to strip any UTF-8 BOM from CSO CSV exports
    csv_text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    columns = list(reader.fieldnames or [])

    if not rows:
        return _error_response(f"Table {code} returned no data rows.")

    table_name = f"px_{code}"
    db = datasette.get_database("user_tables")
    col_types = _infer_types(columns, rows)

    def _load(conn):
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        col_defs = ", ".join(f'"{c}" {col_types[c]}' for c in columns)
        conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
        placeholders = ", ".join("?" * len(columns))
        conn.executemany(
            f'INSERT INTO "{table_name}" VALUES ({placeholders})',
            [_coerce_row(row, columns, col_types) for row in rows],
        )

    await db.execute_write_fn(_load, block=True)

    return Response.redirect(f"/user_tables/{table_name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_response(message: str) -> Response:
    html = f"""
    <html><body style="font-family:sans-serif;padding:2rem;">
    <h2>Error loading table</h2>
    <p>{message}</p>
    <a href="/-/pxstat">&larr; Back to PxStat loader</a>
    </body></html>
    """
    return Response.html(html, status=400)


def _infer_types(columns: list, rows: list) -> dict:
    """Sample first 200 rows to decide INTEGER / REAL / TEXT per column."""
    sample = rows[:200]
    return {col: _best_type([r.get(col, "").strip() for r in sample]) for col in columns}


def _best_type(vals: list) -> str:
    non_empty = [v for v in vals if v]
    if not non_empty:
        return "TEXT"
    try:
        [int(v.replace(",", "")) for v in non_empty]
        return "INTEGER"
    except ValueError:
        pass
    try:
        [float(v.replace(",", "")) for v in non_empty]
        return "REAL"
    except ValueError:
        pass
    return "TEXT"


def _coerce_row(row: dict, columns: list, types: dict) -> list:
    out = []
    for col in columns:
        raw = row.get(col, "").strip()
        t = types[col]
        if t in ("INTEGER", "REAL") and raw == "":
            out.append(None)
        elif t == "INTEGER":
            try:
                out.append(int(raw.replace(",", "")))
            except ValueError:
                out.append(raw or None)
        elif t == "REAL":
            try:
                out.append(float(raw.replace(",", "")))
            except ValueError:
                out.append(raw or None)
        else:
            out.append(raw)
    return out


# ---------------------------------------------------------------------------
# datasette-agent tools (registered only when datasette-agent is installed)
# ---------------------------------------------------------------------------

@hookimpl
def register_agent_tools(datasette):
    if not _AGENT_AVAILABLE:
        return []
    return [
        AgentTool(
            name="search_pxstat_catalog",
            description=(
                "Search the CSO PxStat catalog (~12,600 tables) by keyword to discover table codes "
                "and titles. Also lists tables already loaded in the curated and user_tables databases "
                "so you know what's ready to query. "
                "Call this at the start of any data task to find relevant table codes before using "
                "load_pxstat_table. Pass multiple words to narrow results (all words must appear)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keyword(s) to search table titles, e.g. 'population', "
                            "'housing price', 'unemployment quarterly'. "
                            "Omit to just list currently loaded tables."
                        ),
                    }
                },
                "required": [],
            },
            fn=_tool_search_catalog,
        ),
        AgentTool(
            name="suggest_pxstat_joins",
            description=(
                "Scan all PxStat tables loaded in curated and user_tables databases and "
                "identify columns that could be used to join them together. "
                "Returns shared column names, overlap statistics, and ready-to-run SQL examples. "
                "Use this when asked about combining tables or finding relationships between datasets."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=_tool_suggest_joins,
        ),
        AgentTool(
            name="load_pxstat_table",
            description=(
                "Fetch a CSO Ireland PxStat table by its matrix code (e.g. E2003, HPM09, CPM01) "
                "from the live PxStat API and load it into the user_tables database ready for querying. "
                "Use this when the user wants to add a table that isn't already loaded."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "PxStat matrix code, e.g. E2003 or HPM09",
                    }
                },
                "required": ["code"],
            },
            fn=_tool_load_table,
        ),
    ]


async def _tool_search_catalog(datasette, actor, query: str = ""):
    """Search pxstat_catalog by keyword and report all currently loaded tables."""
    result = {}

    # Tables already loaded in curated (persistent pre-selected datasets)
    loaded_curated = []
    if "curated" in datasette.databases:
        db = datasette.get_database("curated")
        tbl_names = await db.table_names()
        px_tables = [t for t in tbl_names if t.startswith("px_")]
        if px_tables:
            meta = {}
            if "_pxstat_meta" in tbl_names:
                meta_rows = await db.execute(
                    "SELECT table_name, code, description FROM _pxstat_meta"
                )
                meta = {r[0]: {"code": r[1], "description": r[2]} for r in meta_rows.rows}
            for tbl in px_tables:
                entry = {"table": tbl}
                entry.update(meta.get(tbl, {}))
                loaded_curated.append(entry)
    result["loaded_curated_tables"] = loaded_curated

    # Tables loaded in the current session by the user
    loaded_user = []
    if "user_tables" in datasette.databases:
        db = datasette.get_database("user_tables")
        loaded_user = [t for t in await db.table_names() if t.startswith("px_")]
    result["loaded_user_tables"] = loaded_user

    # Catalog search
    catalog_matches = []
    if "curated" in datasette.databases:
        db = datasette.get_database("curated")
        if "pxstat_catalog" in await db.table_names():
            words = [w for w in query.strip().split() if w]
            if words:
                conditions = " AND ".join("(title LIKE ? OR code LIKE ?)" for _ in words)
                params = [p for w in words for p in (f"%{w}%", f"%{w}%")]
                rows = await db.execute(
                    f"SELECT code, title FROM pxstat_catalog WHERE {conditions} "
                    f"ORDER BY title LIMIT 20",
                    params,
                )
                result["catalog_search_query"] = query
            else:
                rows = await db.execute(
                    "SELECT code, title FROM pxstat_catalog ORDER BY title LIMIT 20"
                )
            catalog_matches = [{"code": r[0], "title": r[1]} for r in rows.rows]

    result["catalog_matches"] = catalog_matches
    result["note"] = (
        "Use load_pxstat_table with a 'code' value to load any catalog entry."
        if catalog_matches else
        "No catalog matches found. Try different keywords."
        if query.strip() else
        "Pass a 'query' parameter to search the catalog by topic."
    )
    return json.dumps(result)


_SKIP_COLS = {
    "UNIT", "VALUE", "STATISTIC", "STATISTIC Label",
    "value", "unit", "statistic", "statistic label",
}


async def _tool_suggest_joins(datasette, actor):
    """
    Find joinable columns across all PxStat tables in curated + user_tables.

    Detects both exact name matches AND columns with different names that share
    significant value overlap (e.g. CensusYear ↔ Year, County ↔ Region).
    """
    db_names = [n for n in ("curated", "user_tables") if n in datasette.databases]

    # Collect columns + sampled distinct values per table
    # table_info: {(db_name, tbl): {col: frozenset(sample_values)}}
    table_info = {}

    for db_name in db_names:
        db = datasette.get_database(db_name)
        for tbl in await db.table_names():
            if not tbl.startswith("px_"):
                continue
            col_result = await db.execute(f'PRAGMA table_info("{tbl}")')
            cols = [r[1] for r in col_result.rows if r[1] not in _SKIP_COLS]

            col_values = {}
            for col in cols:
                try:
                    res = await db.execute(
                        f'SELECT DISTINCT "{col}" FROM "{tbl}" '
                        f'WHERE "{col}" IS NOT NULL AND "{col}" != \'\' LIMIT 80'
                    )
                    col_values[col] = frozenset(str(r[0]) for r in res.rows)
                except Exception:
                    col_values[col] = frozenset()

            table_info[(db_name, tbl)] = col_values

    if len(table_info) < 2:
        return json.dumps({"message": "Need at least two px_ tables loaded to suggest joins."})

    tables = list(table_info.items())
    suggestions = []

    for i, ((db_a, tbl_a), cols_a) in enumerate(tables):
        for (db_b, tbl_b), cols_b in tables[i + 1:]:
            join_candidates = []

            for col_a, vals_a in cols_a.items():
                if not vals_a:
                    continue
                for col_b, vals_b in cols_b.items():
                    if not vals_b:
                        continue

                    exact_match = col_a == col_b

                    # Name-based fuzzy: one name contains the other (case-insensitive)
                    a_norm = col_a.lower().replace(" ", "").replace("_", "")
                    b_norm = col_b.lower().replace(" ", "").replace("_", "")
                    name_fuzzy = a_norm in b_norm or b_norm in a_norm

                    # Value overlap: Jaccard similarity
                    intersection = len(vals_a & vals_b)
                    union = len(vals_a | vals_b)
                    jaccard = intersection / union if union else 0.0

                    if exact_match or name_fuzzy or jaccard >= 0.25:
                        match_type = (
                            "exact_name" if exact_match
                            else "similar_name" if name_fuzzy
                            else "value_overlap"
                        )
                        join_candidates.append({
                            "col_a": col_a,
                            "col_b": col_b,
                            "match_type": match_type,
                            "value_overlap_pct": round(jaccard * 100, 1),
                            "shared_value_examples": sorted(vals_a & vals_b)[:5],
                        })

            if not join_candidates:
                continue

            # Build example SQL using the best candidate (exact > fuzzy > value)
            best = sorted(
                join_candidates,
                key=lambda c: (c["match_type"] != "exact_name", c["match_type"] != "similar_name", -c["value_overlap_pct"])
            )[0]

            sql_example = (
                f'SELECT\n'
                f'  a.*,\n'
                f'  b."VALUE" AS "{tbl_b}_value"\n'
                f'FROM "{tbl_a}" a\n'
                f'JOIN "{tbl_b}" b\n'
                f'  ON CAST(a."{best["col_a"]}" AS TEXT) = CAST(b."{best["col_b"]}" AS TEXT)\n'
                f'LIMIT 100'
            )

            suggestions.append({
                "table_a": f"{db_a}.{tbl_a}",
                "table_b": f"{db_b}.{tbl_b}",
                "join_candidates": join_candidates,
                "recommended_join": best,
                "example_sql": sql_example,
            })

    if not suggestions:
        return json.dumps({"message": "No joinable columns found between loaded tables."})

    return json.dumps({"join_suggestions": suggestions})


async def _tool_load_table(datasette, actor, code: str):
    """Load a PxStat table by code into user_tables."""
    code = re.sub(r"[^A-Z0-9]", "", code.upper())
    if not code:
        return json.dumps({"error": "Invalid table code."})

    url = DATASET_URL.format(code=code)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
    except Exception as exc:
        return json.dumps({"error": f"Network error: {exc}"})

    if resp.status_code != 200:
        return json.dumps({"error": f"Table {code} not found on PxStat (HTTP {resp.status_code})."})

    csv_text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    columns = list(reader.fieldnames or [])

    if not rows:
        return json.dumps({"error": f"Table {code} returned no data."})

    table_name = f"px_{code}"
    db = datasette.get_database("user_tables")
    col_types = _infer_types(columns, rows)

    def _load(conn):
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        col_defs = ", ".join(f'"{c}" {col_types[c]}' for c in columns)
        conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
        placeholders = ", ".join("?" * len(columns))
        conn.executemany(
            f'INSERT INTO "{table_name}" VALUES ({placeholders})',
            [_coerce_row(row, columns, col_types) for row in rows],
        )

    await db.execute_write_fn(_load, block=True)

    return json.dumps({
        "loaded": table_name,
        "database": "user_tables",
        "rows": len(rows),
        "columns": columns,
        "message": f"Table {table_name} loaded with {len(rows):,} rows. Query it at /user_tables/{table_name}",
        "_html": (
            f'<p>✅ Loaded <strong>{table_name}</strong> — {len(rows):,} rows, {len(columns)} columns.</p>'
            f'<p class="agent-sql-edit-link"><a href="/user_tables/{table_name}">Browse {table_name} →</a></p>'
        ),
    })
