# Offline map data

Everything the `/map` page needs at serve time. All of it is briefing-phase
output — fetched once while the internet was up, then served fully offline.

## london.pmtiles (NOT in git — 134 MB, over GitHub's 100 MB hard limit)

Protomaps basemap extract for Greater London, zoom 0–15, vector MVT,
tile schema 4.15.2 (source-layer names verified against the vendored
`@protomaps/basemaps` 5.7.2 style in `static/vendor/`).

Regenerate on any machine with internet (~2 min; the daily planet build is
range-readable, only the London bytes are downloaded):

```sh
# pmtiles CLI: https://github.com/protomaps/go-pmtiles/releases (v1.31.2 used)
pmtiles extract https://build.protomaps.com/20260821.pmtiles \
  data/london.pmtiles --bbox=-0.52,51.28,0.34,51.70
```

Any newer daily build (`YYYYMMDD.pmtiles`) also works. The beacon serves this
file with HTTP Range support (`/data/london.pmtiles`); the pmtiles JS client
reads it by byte range, so it is never loaded into RAM whole.

## postcodes.json (18 KB, in git)

Every London-area outward code (326 of them: E, EC, N, NW, SE, SW, W, WC plus
metro BR, CR, DA, EN, HA, IG, KT, RM, SM, TW, UB, WD) → centroid + borough:

```json
{"SW7": {"lat": 51.49631, "lon": -0.17698, "area": "Kensington and Chelsea"}}
```

Source: postcodes.io outcode API, filtered to centroids inside the tile bbox.
Regenerate: briefing script queried `https://api.postcodes.io/outcodes/<code>`
for ~600 candidates (see git history / WRITEUP for the script).

## hospitals.json (4 KB, in git)

32 hospitals with 24 h A&E departments in Greater London:
`[{"name", "lat", "lon", "address", "note"?}]`. `note` marks specialist EDs
("Eye emergencies only", "Children's A&E") — the map page excludes noted
entries from its nearest-3 ranking but still plots them.

Source: Overpass API (`amenity=hospital` + `emergency=yes`, bbox as above),
manually curated: node/way duplicates dropped, two mental-health mis-tags
removed, West Middlesex + Newham added (real A&Es missing `emergency=yes`
in OSM). Data © OpenStreetMap contributors (ODbL).

## pois/, graph/

Owned by the POI-layers and walking-routes agents — see POIS-INTEGRATION.md
and ROUTES-INTEGRATION.md at the repo root.
