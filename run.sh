#!/usr/bin/env bash
# Start the CSO Datasette server.
# Run from the CSO_datasette directory.
#
# First time (or to refresh curated tables):
#   python etl/load_curated.py
#
# Then serve:
uv run datasette serve curated.db search.db \
  -c datasette.yml \
  --plugins-dir plugins/ \
  --template-dir templates/ \
  --static static:static \
  --internal internal.db \
  --port 8001 \
  --root \
  --reload
