uv run datasette serve curated.db search.db `
  -c datasette.yml `
  --plugins-dir plugins/ `
  --template-dir templates/ `
  --static static:static `
  --internal internal.db `
  --port 8001 `
  --root `
  --reload