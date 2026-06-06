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


def fetch_navigation() -> list[dict]:
    print("Fetching navigation tree from CSO PxStat...", flush=True)
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            CATALOG_URL,
            json={
                "jsonrpc": "2.0",
                "method": "PxStat.System.Navigation.Navigation_API.Read",
                "params": {"LngIsoCode": "en"},
            },
        )
    resp.raise_for_status()
    tree = resp.json()["result"]
    n_subjects = sum(len(t["subject"]) for t in tree)
    print(f"  {len(tree)} themes, {n_subjects} subjects found.")
    return tree


def store_subjects(conn: sqlite3.Connection, tree: list[dict]):
    conn.execute("DROP TABLE IF EXISTS pxstat_subjects")
    conn.execute("""
        CREATE TABLE pxstat_subjects (
            thm_code  INTEGER,
            thm_value TEXT,
            sbj_code  INTEGER PRIMARY KEY,
            sbj_value TEXT
        )
    """)
    rows = [
        (t["ThmCode"], t["ThmValue"], s["SbjCode"], s["SbjValue"])
        for t in tree
        for s in t["subject"]
    ]
    conn.executemany("INSERT INTO pxstat_subjects VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    print(f"  Stored {len(rows)} subjects in pxstat_subjects.")


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
    tree = fetch_navigation()

    conn = sqlite3.connect(DB_PATH)
    store_catalog(conn, items)
    store_subjects(conn, tree)
    conn.close()

    build_fts_index()
    print(f"\nDone. Search ready at /-/beta")


if __name__ == "__main__":
    main()
