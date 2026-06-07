"""
datasette-pxstat: load CSO PxStat tables on demand into an in-memory database.

Routes added:
  GET  /-/pxstat                         catalog browser + load form
  POST /-/pxstat/load                    fetch a table by code and redirect to it
  GET  /-/pxstat/subjects                Theme > Subject tree (JSON)
  GET  /-/pxstat/browse/<sbj_code>       matrices for a subject (JSON)

Agent tools (when datasette-agent is installed):
  search_pxstat_catalog     search the catalog for table codes by keyword
  suggest_pxstat_joins      find joinable columns across loaded tables
  load_pxstat_table         load a PxStat table by code from within the agent
"""

import csv
import io
import json
import re
import sys

from datasette import hookimpl, Response

# ---------------------------------------------------------------------------
# HTTP helpers — pyfetch (Pyodide/browser) or httpx (server)
# ---------------------------------------------------------------------------

def _is_pyodide():
    return sys.platform == "emscripten"


async def _get(url):
    """HTTP GET — returns (status_code, bytes)."""
    if _is_pyodide():
        from pyodide.http import pyfetch
        resp = await pyfetch(url)
        return resp.status, await resp.bytes()
    else:
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
        return r.status_code, r.content


async def _post_json(url, payload):
    """HTTP POST with JSON body — returns (status_code, parsed_json)."""
    if _is_pyodide():
        from pyodide.http import pyfetch
        resp = await pyfetch(url, method="POST",
                             headers={"Content-Type": "application/json"},
                             body=json.dumps(payload))
        return resp.status, await resp.json()
    else:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload)
        return r.status_code, r.json()


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
    async def inner():
        datasette.add_memory_database("user_tables")
        db = datasette.get_database("user_tables")
        await db.execute_write(
            "CREATE TABLE IF NOT EXISTS _pxstat_meta "
            "(table_name TEXT PRIMARY KEY, code TEXT, description TEXT)"
        )
    return inner


@hookimpl
def register_routes():
    return [
        (r"^/-/pxstat$", pxstat_index),
        (r"^/-/pxstat/load$", pxstat_load),
        (r"^/-/pxstat/subjects$", pxstat_subjects),
        (r"^/-/pxstat/browse/(?P<sbj_code>\d+)$", pxstat_browse_subject),
    ]


@hookimpl
def menu_links(datasette, actor):
    return [{"href": "/-/pxstat", "label": "Load PxStat Table"}]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def pxstat_index(datasette, request):
    db = datasette.get_database("user_tables")
    table_names = [t for t in await db.table_names() if not t.startswith("_")]
    has_curated = "curated" in datasette.databases
    prefill_code = request.args.get("code", "").strip().upper()

    descriptions = {}
    try:
        rows = await db.execute("SELECT table_name, description FROM _pxstat_meta")
        for row in rows.rows:
            descriptions[row[0]] = row[1]
    except Exception:
        pass

    loaded = [{"name": t, "description": descriptions.get(t, "")} for t in table_names]

    return Response.html(
        await datasette.render_template(
            "pxstat_index.html",
            {
                "loaded": loaded,
                "has_curated": has_curated,
                "prefill_code": prefill_code,
            },
            request=request,
        )
    )


async def pxstat_load(datasette, request):
    if request.method != "POST":
        return Response.redirect("/-/pxstat")

    post_vars = await request.post_vars()
    code = re.sub(r"[^A-Z0-9]", "", post_vars.get("code", "").strip().upper())

    if not code:
        return _error_response("Please supply a table code (e.g. NPA03).")

    url = DATASET_URL.format(code=code)

    try:
        status, body = await _get(url)
    except Exception as exc:
        return _error_response(f"Network error: {exc}")

    if status == 404:
        return _error_response(f"Table <strong>{code}</strong> was not found on PxStat.")
    if status != 200:
        return _error_response(f"CSO API returned HTTP {status} for table {code}.")

    # Decode with utf-8-sig to strip any UTF-8 BOM from CSO CSV exports
    csv_text = body.decode("utf-8-sig")
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
    await _store_pxstat_meta(datasette, code, table_name)

    return Response.redirect(f"/user_tables/{table_name}")


# ---------------------------------------------------------------------------
# Subjects / browse routes
# ---------------------------------------------------------------------------

async def pxstat_subjects(datasette, request):
    """Return the Theme > Subject tree, from DB if available, else live API."""
    if "curated" in datasette.databases:
        try:
            rows = (await datasette.get_database("curated").execute(
                "SELECT thm_code, thm_value, sbj_code, sbj_value "
                "FROM pxstat_subjects ORDER BY thm_value, sbj_value"
            )).rows
            themes = {}
            for r in rows:
                thm = themes.setdefault(r[0], {"code": r[0], "value": r[1], "subjects": []})
                thm["subjects"].append({"code": r[2], "value": r[3]})
            return Response.json(list(themes.values()))
        except Exception:
            pass

    # Fallback: live API call
    try:
        _, data = await _post_json(CATALOG_URL, {
            "jsonrpc": "2.0",
            "method": "PxStat.System.Navigation.Navigation_API.Read",
            "params": {"LngIsoCode": "en"},
        })
        tree = data["result"]
        themes = [
            {"code": t["ThmCode"], "value": t["ThmValue"],
             "subjects": [{"code": s["SbjCode"], "value": s["SbjValue"]}
                          for s in t["subject"]]}
            for t in tree
        ]
        return Response.json(themes)
    except Exception as exc:
        return Response.json({"error": str(exc)}, status=502)


async def pxstat_browse_subject(datasette, request):
    """Return matrices for a given subject code via Navigation_API.Search."""
    sbj_code = int(request.url_vars["sbj_code"])
    try:
        _, data = await _post_json(CATALOG_URL, {
            "jsonrpc": "2.0",
            "method": "PxStat.System.Navigation.Navigation_API.Search",
            "params": {"LngIsoCode": "en", "SbjCode": sbj_code},
        })
        if "result" not in data:
            return Response.json({"error": "Navigation API error"}, status=502)
        matrices = [{"code": m["MtrCode"], "title": m["MtrTitle"]} for m in data["result"]]
        return Response.json({"matrices": matrices})
    except Exception as exc:
        return Response.json({"error": str(exc)}, status=502)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _store_pxstat_meta(datasette, code: str, table_name: str):
    """Look up table title from catalog and persist to user_tables._pxstat_meta."""
    description = ""
    if "curated" in datasette.databases:
        try:
            row = (await datasette.get_database("curated").execute(
                "SELECT title FROM pxstat_catalog WHERE code = ?", [code]
            )).first()
            if row:
                description = row["title"]
        except Exception:
            pass
    db = datasette.get_database("user_tables")
    await db.execute_write(
        "INSERT OR REPLACE INTO _pxstat_meta VALUES (?, ?, ?)",
        [table_name, code, description],
    )


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
                "Full-text search the CSO PxStat catalog of 12,000+ tables by keyword. "
                "Returns matching table codes and titles ranked by relevance. "
                "Use this to discover relevant tables before loading them with load_pxstat_table. "
                "IMPORTANT: use short single keywords for best results — the index is exact-match "
                "so 'house' finds far more than 'housing prices'. Run several focused searches "
                "with different terms to explore a topic: for housing try 'house', 'rent', "
                "'mortgage', 'property', 'dwelling' as separate calls. "
                "Single words always outperform phrases."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A single short keyword, e.g. 'house', 'rent', 'trade', 'birth'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 20, max 50)",
                    },
                },
                "required": ["query"],
            },
            fn=_tool_search_catalog,
        ),
        AgentTool(
            name="suggest_pxstat_joins",
            description=(
                "Scan all PxStat tables loaded in curated and user_tables databases and "
                "identify columns that could be used to join them together. "
                "Returns shared column names, overlap statistics, and ready-to-run SQL examples. "
                "Use this when asked about combining tables or finding relationships between datasets. "
                "You may then run the suggested SQL with sql_query, compute derived statistics "
                "with execute_micropython, or visualise the combined data with render_chart."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=_tool_suggest_joins,
        ),
        AgentTool(
            name="load_pxstat_table",
            description=(
                "Fetch a CSO Ireland PxStat table by its matrix code (e.g. E2003, HPM09, CPM01) "
                "from the live PxStat API and load it into the user_tables database ready for querying. "
                "Use this when the user wants to add a table that isn't already loaded. "
                "You may then filter and aggregate with sql_query, compute derived statistics "
                "with execute_micropython, or visualise results with render_chart."
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


async def _tool_search_catalog(datasette, actor, query: str, limit: int = 20):
    if "search" not in datasette.databases:
        return json.dumps({"error": "Search index not available."})
    limit = min(max(1, int(limit)), 50)
    # Auto-append FTS5 prefix wildcard for plain single-word queries so that
    # e.g. "house" matches house/houses/housing/household automatically.
    q = query.strip()
    if q and ' ' not in q and not any(c in q for c in '*"()'):
        q = q + '*'
    try:
        rows = await datasette.get_database("search").execute(
            "SELECT si.key, si.title "
            "FROM search_index si "
            "JOIN search_index_fts fts ON si.rowid = fts.rowid "
            "WHERE search_index_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            [q, limit],
        )
        results = [{"code": r[0], "title": r[1]} for r in rows.rows]
        if not results:
            return json.dumps({"message": f"No tables found matching '{query}'.", "results": []})
        return json.dumps({"results": results, "count": len(results)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


_SKIP_COLS = {
    "UNIT", "VALUE", "STATISTIC", "STATISTIC Label",
    "value", "unit", "statistic", "statistic label",
}


async def _tool_suggest_joins(datasette, actor):
    db_names = [n for n in ("curated", "user_tables") if n in datasette.databases]
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
                    a_norm = col_a.lower().replace(" ", "").replace("_", "")
                    b_norm = col_b.lower().replace(" ", "").replace("_", "")
                    name_fuzzy = a_norm in b_norm or b_norm in a_norm

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
        status, body = await _get(url)
    except Exception as exc:
        return json.dumps({"error": f"Network error: {exc}"})

    if status != 200:
        return json.dumps({"error": f"Table {code} not found on PxStat (HTTP {status})."})

    csv_text = body.decode("utf-8-sig")
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
    await _store_pxstat_meta(datasette, code, table_name)

    return json.dumps({
        "loaded": table_name,
        "database": "user_tables",
        "rows": len(rows),
        "columns": columns,
        "message": f"Table {table_name} loaded with {len(rows):,} rows. Query it at /user_tables/{table_name}",
        "_html": (
            f'<p>Loaded <strong>{table_name}</strong> &mdash; {len(rows):,} rows, {len(columns)} columns.</p>'
            f'<p class="agent-sql-edit-link"><a href="/user_tables/{table_name}">Browse {table_name} &rarr;</a></p>'
        ),
    })
