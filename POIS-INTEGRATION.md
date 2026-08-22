# Emergency POI Layers — Integration Spec

Data for the offline map page (MapLibre). Fetched once from OpenStreetMap via
Overpass (`tools/fetch_pois.py`); served fully offline by the beacon.

## Fetch paths

All files are compact JSON with the same shape:

```json
{
  "category": "pharmacies",
  "source": "OpenStreetMap via Overpass API (ODbL)",
  "bbox": [-0.52, 51.28, 0.34, 51.70],
  "generated_at": "2026-08-22T…Z",
  "count": 1431,
  "items": [
    {"name": "Boots", "lat": 51.5142, "lon": -0.1585,
     "opening_hours": "Mo-Sa 09:00-18:00", "phone": "+44 …", "wheelchair": "yes"}
  ]
}
```

`opening_hours`, `phone`, `wheelchair` are present only when OSM has them —
guard with `item.phone ?? ""` etc.

| Layer | Path | Count | Size |
|---|---|---|---|
| Pharmacies | `/data/pois/pharmacies.json` | 1431 | 120 KB |
| Defibrillators | `/data/pois/defibrillators.json` | 774 | 53 KB |
| Police | `/data/pois/police.json` | 152 | 11 KB |
| Fire stations | `/data/pois/fire_stations.json` | 135 | 9 KB |
| Hospitals (all) | `/data/pois/hospitals_all.json` | 240 | 18 KB |

Note: `hospitals_all.json` is EVERY `amenity=hospital` (inc. specialist and
mental-health sites with no emergency dept). Keep it separate from the A&E-only
layer — render it dimmer so A&E stays visually dominant.

## Layer spec (dark emergency-green aesthetic)

Page palette: bg `#041207`/`#052e16`, accent `#4ade80`, alert `#ef4444`.

| Layer | Emoji | Marker color | Default | Notes |
|---|---|---|---|---|
| A&E (sibling layer) | 🏥 | `#ef4444` red | ON | Always on top; largest markers |
| Pharmacies | 💊 | `#4ade80` green | ON | Show `opening_hours` in popup |
| Defibrillators | ⚡ | `#facc15` yellow | ON | Mostly unnamed — popup title "Defibrillator (AED)" |
| Hospitals (all) | 🩺 | `#f87171` dim red @ 60% opacity | OFF (toggle) | Label "no A&E confirmed" in popup |
| Police | 🚓 | `#60a5fa` blue | OFF (toggle) | |
| Fire stations | 🚒 | `#fb923c` orange | OFF (toggle) | |
| Water/supermarket (if added later) | 🚰 | `#22d3ee` cyan | OFF (toggle) | No dataset yet — placeholder row |

Marker style that reads well on the dark basemap: filled circle in the layer
color with a 1.5px `#041207` stroke, emoji or letter glyph only at zoom >= 14.

## Clustering advice

- Pharmacies (1431) and defibrillators (774) WILL smear central London at low
  zoom. Load each as a GeoJSON source with `cluster: true`,
  `clusterRadius: 45`, `clusterMaxZoom: 13`; style cluster circles in the layer
  color with the count as a `#041207` label.
- Police / fire / hospitals are sparse (<250 each) — no clustering needed;
  optionally `minzoom: 10` to keep the overview clean.
- Convert items to GeoJSON client-side (trivial):
  `{type:"Feature", geometry:{type:"Point", coordinates:[item.lon, item.lat]}, properties:item}`.

## Popup fields

Title = `name`; then any of `opening_hours` ("Hours: …"), `phone` (render as
`tel:` link — phones may still have cell service in a blackout), `wheelchair`
("Wheelchair: yes/no/limited"). Omit missing fields rather than showing blanks.

## Data quality caveats

- Deduped at ~30 m + same name; records without coordinates dropped;
  ways/relations use Overpass centroids (`out center`).
- Defibrillator coverage in OSM is incomplete for London (774 mapped; the real
  number is higher) — worth a one-line disclaimer in the layer toggle UI.
- Only ~30% of pharmacies carry `opening_hours`; treat hours as advisory.
- One hospital (Kingsley Green) centroid sits ~400 m outside the bbox because
  its grounds straddle the boundary — harmless.
- Attribution: data (c) OpenStreetMap contributors, ODbL — the map page's
  existing OSM attribution line covers it.
