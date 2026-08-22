# data/graph - offline London walking network

`london.graph` (52.7 MB, committed) is the compact binary street graph used by
`routes.py` for offline walking routes. Built from the OpenStreetMap Greater
London extract: 581,702 nodes / 800,059 edges / 2.9 M geometry points covering
lon -0.518..0.337, lat 51.283..51.699 (all of Greater London).

`meta.json` records counts, bbox and build stats. The binary layout is
documented in the docstring of `tools/build_graph.py`.

## Regenerating (needs internet, one-off)

```bash
curl -L -o data/graph/greater-london-latest.osm.pbf \
  https://download.geofabrik.de/europe/united-kingdom/england/greater-london-latest.osm.pbf
.venv/bin/pip install osmium
.venv/bin/python tools/build_graph.py        # ~20 s, writes london.graph + meta.json
```

The raw `.osm.pbf` (128 MB) is gitignored - it exceeds GitHub's 100 MB file
limit and is only needed at build time. Serving is fully offline: `routes.py`
loads `london.graph` with the Python 3.9 stdlib only (no pip packages).
