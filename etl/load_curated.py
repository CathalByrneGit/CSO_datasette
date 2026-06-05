"""
load_curated.py — fetch a curated set of CSO PxStat tables and write them to curated.db.

Usage:
    python etl/load_curated.py

Run from the CSO_datasette project root. Overwrites curated.db tables on each run.
"""

import csv
import io
import sqlite3
import sys
from pathlib import Path

import httpx

DB_PATH = Path(__file__).parent.parent / "curated.db"

DATASET_URL = (
    "https://ws.cso.ie/public/api.restful"
    "/PxStat.Data.Cube_API.ReadDataset/{code}/CSV/1.0/en"
)

# Curated list: (code, friendly_description)
CURATED_TABLES = [
    # Population
    ("E2003", "Population Estimates by Age, Sex and Year"),
    ("PEA01", "Population by Region and Year"),
    # Labour market
    ("QLF01", "Quarterly Labour Force Survey — Employment & Unemployment"),
    ("MUM01", "Monthly Unemployment Estimates"),
    # Housing & prices
    ("HPM09", "Residential Property Price Index"),
    ("CPM01", "Consumer Price Index by Category and Month"),
    # Income
    ("IIA01", "Survey on Income and Living Conditions"),
    # Tourism
    ("TOA01", "Overseas Travel Arrivals by Country of Residence"),
    # Trade
    ("TSM06", "Merchandise Trade — Imports and Exports"),
    # Agriculture
    ("AAA23", "Area, Yield and Production of Crops"),
]


def fetch_csv(code: str) -> list[dict]:
    url = DATASET_URL.format(code=code)
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url)
    if resp.status_code == 404:
        raise ValueError(f"Table {code} not found on PxStat")
    resp.raise_for_status()
    csv_text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    columns = list(reader.fieldnames or [])
    return rows, columns


def infer_type(vals: list) -> str:
    non_empty = [v for v in vals if v.strip()]
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


def coerce(value: str, t: str):
    v = value.strip()
    if t in ("INTEGER", "REAL") and v == "":
        return None
    if t == "INTEGER":
        try:
            return int(v.replace(",", ""))
        except ValueError:
            return v or None
    if t == "REAL":
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return v or None
    return v


def load_table(conn: sqlite3.Connection, code: str, description: str):
    table_name = f"px_{code}"
    print(f"  Fetching {code} …", end=" ", flush=True)

    try:
        rows, columns = fetch_csv(code)
    except Exception as exc:
        print(f"SKIPPED ({exc})")
        return

    sample = rows[:200]
    col_types = {
        c: infer_type([r.get(c, "") for r in sample]) for c in columns
    }

    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    col_defs = ", ".join(f'"{c}" {col_types[c]}' for c in columns)
    conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')

    # Store friendly description as table comment via sqlite_master isn't possible;
    # use a companion _meta table instead.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _pxstat_meta (
            table_name TEXT PRIMARY KEY,
            code       TEXT,
            description TEXT
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO _pxstat_meta VALUES (?, ?, ?)",
        (table_name, code, description),
    )

    placeholders = ", ".join("?" * len(columns))
    conn.executemany(
        f'INSERT INTO "{table_name}" VALUES ({placeholders})',
        [
            [coerce(row.get(c, ""), col_types[c]) for c in columns]
            for row in rows
        ],
    )
    conn.commit()
    print(f"{len(rows):,} rows loaded.")


def main():
    print(f"Writing to {DB_PATH}\n")
    conn = sqlite3.connect(DB_PATH)

    ok = 0
    for code, description in CURATED_TABLES:
        load_table(conn, code, description)
        ok += 1

    conn.close()
    print(f"\nDone. {ok}/{len(CURATED_TABLES)} tables loaded into {DB_PATH.name}")


if __name__ == "__main__":
    main()
