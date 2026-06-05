"""
build_catalog.py — fetch the full CSO PxStat catalog and store it in curated.db,
then build the dogsheep-beta FTS search index.

Usage:
    python etl/build_catalog.py

Run from the CSO_datasette project root.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx

DB_PATH      = Path(__file__).parent.parent / "curated.db"
SEARCH_PATH  = Path(__file__).parent.parent / "search.db"
CONFIG_PATH  = Path(__file__).parent.parent / "dogsheep-beta.yml"
CATALOG_URL  = "https://ws.cso.ie/public/api.jsonrpc"


def fetch_catalog() -> list[dict]:
    print("Fetching catalog from CSO PxStat...", flush=True)
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            CATALOG_URL,
            json={
                "jsonrpc": "2.0",
                "method": "PxStat.Data.Cube_API.ReadCollection",
                "params": {"language": "en"},
            },
        )
    resp.raise_for_status()
    data = resp.json()
    items = data["result"]["link"]["item"]
    print(f"  {len(items):,} tables found.")
    return items


def store_catalog(conn: sqlite3.Connection, items: list[dict]):
    conn.execute("DROP TABLE IF EXISTS pxstat_catalog")
    conn.execute("""
        CREATE TABLE pxstat_catalog (
            code  TEXT PRIMARY KEY,
            title TEXT
        )
    """)
    rows = [
        (
            item["extension"]["matrix"],
            item.get("label", ""),
        )
        for item in items
        if item.get("extension", {}).get("matrix")
    ]
    conn.executemany("INSERT OR REPLACE INTO pxstat_catalog VALUES (?, ?)", rows)
    conn.commit()
    print(f"  Stored {len(rows):,} rows in pxstat_catalog.")


def build_fts_index():
    print("\nBuilding dogsheep-beta FTS index into search.db...")
    result = subprocess.run(
        ["dogsheep-beta", "index", str(SEARCH_PATH), str(CONFIG_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("ERROR:", result.stderr)
        sys.exit(1)
    print("  FTS index built.")


def main():
    items = fetch_catalog()

    conn = sqlite3.connect(DB_PATH)
    store_catalog(conn, items)
    conn.close()

    build_fts_index()
    print(f"\nDone. Search ready at /-/beta")


if __name__ == "__main__":
    main()
