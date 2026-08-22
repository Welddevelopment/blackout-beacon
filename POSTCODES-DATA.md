# Full-postcode dataset (`data/postcodes.sqlite`)

Every live GB **unit postcode** whose centroid falls inside the offline map
coverage bbox (lon −0.52…0.34, lat 51.28…51.70 — same bbox as
`data/london.pmtiles`), resolving to precise WGS84 coordinates, fully offline
at serve time. Upgrades the beacon from outward-code-only resolution
(`data/postcodes.json`, N20 → district centroid) to unit resolution
(N20 9AQ → the street).

## Counts and size

| What                    | Value                              |
| ----------------------- | ---------------------------------- |
| Unit postcodes (`pc`)   | 213,901                            |
| Outward codes (`outcode`) | 361 (superset of the 326 in `postcodes.json`) |
| File size               | 10.6 MB (committed to git)         |

## Source and licence

**ONS Postcode Directory (ONSPD), May 2026 release**, Office for National
Statistics, via the [ONS Open Geography portal](https://geoportal.statistics.gov.uk)
(direct zip, ~247 MB — ArcGIS item `6fff67d204fd4f339591ed667a6e3642`).

Licensed under the [Open Government Licence v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
Required attribution:

> Contains OS data © Crown copyright and database right 2026.
> Contains Royal Mail data © Royal Mail copyright and database right 2026.
> Source: Office for National Statistics licensed under the Open Government
> Licence v.3.0.

Filtering applied: terminated postcodes skipped (`doterm` non-empty), rows
with no usable grid reference skipped (`gridind` = 9), then bbox filter on
`lat`/`long`. Coordinates kept at source precision (6 dp, ≈ 0.1 m).

## Schema

```sql
CREATE TABLE pc      (pc TEXT PRIMARY KEY, lat REAL, lon REAL, outcode TEXT);
  -- pc normalized: uppercase, NO spaces ("N209AQ", "SW72AZ")
CREATE TABLE outcode (oc TEXT PRIMARY KEY, lat REAL, lon REAL, n INTEGER);
  -- centroid = mean of member unit postcodes; n = member count
CREATE TABLE meta    (k TEXT PRIMARY KEY, v TEXT);
  -- source, source_date, source_url, licence, bbox, generated_at,
  -- count, outcode_count
```

Lookups (PKs give the indexes):

```sql
SELECT lat, lon FROM pc      WHERE pc = 'SW72AZ';   -- normalize input first
SELECT lat, lon FROM outcode WHERE oc = 'SW7';      -- fallback for partials
```

## Regeneration (needs internet once; build itself is offline)

```sh
python3 tools/fetch_postcodes_full.py
```

Downloads the ONSPD zip to the system temp dir (never the repo; reused if
already there), streams the 1.45 GB CSV straight out of the zip, writes
`data/postcodes.sqlite`, `VACUUM`s, and runs validation. Use
`--zip PATH` to point at an already-downloaded ONSPD zip, `--url` for a
newer release (swap the ArcGIS item id), `--out` for a different output path.
Takes ~1–2 min after download; exit code 0 only if all checks pass.

## Validation (run at build time, all passing)

- Unit count 213,901 — plausible for Greater London + margin (150k–400k).
- Spot checks match api.postcodes.io (independent ONSPD-derived service) to
  5 dp exactly: SW7 2AZ (51.49933, −0.17918 — Imperial College), N20 9AQ,
  E1 4NS, SE15 5DT, NW1 2BU, EC3N 4AB, W1A 1AA, CR0 2YR.
- All 326 outcodes in `data/postcodes.json` present in the `outcode` table;
  centroids agree with it to ≤ 0.00001°.
