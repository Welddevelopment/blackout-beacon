#!/usr/bin/env python3
"""Fetch emergency POI layers for Blackout Beacon from the Overpass API.

Queries Greater London (bbox -0.52,51.28,0.34,51.70) for:
  - pharmacies       (amenity=pharmacy)        -> data/pois/pharmacies.json
  - defibrillators   (emergency=defibrillator) -> data/pois/defibrillators.json
  - police           (amenity=police)          -> data/pois/police.json
  - fire stations    (amenity=fire_station)    -> data/pois/fire_stations.json
  - hospitals (ALL)  (amenity=hospital)        -> data/pois/hospitals_all.json

Run once while online; the JSON is then served offline by beacon_server.py.
Polite to Overpass: one query per category, sleep between queries, mirror
fallback with retry/backoff.

Usage: python3 tools/fetch_pois.py
"""

import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# bbox given as (west, south, east, north); Overpass wants (south, west, north, east)
WEST, SOUTH, EAST, NORTH = -0.52, 51.28, 0.34, 51.70
BBOX = f"({SOUTH},{WEST},{NORTH},{EAST})"

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

SLEEP_BETWEEN_QUERIES = 10  # seconds; be polite, one query per category
USER_AGENT = "BlackoutBeacon/1.0 (offline emergency map; one-off data fetch)"

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "pois"

# category -> (overpass tag filter, output filename, fallback-name noun)
CATEGORIES = [
    ("pharmacies", '["amenity"="pharmacy"]', "pharmacies.json", "pharmacy"),
    ("defibrillators", '["emergency"="defibrillator"]', "defibrillators.json", "defibrillator"),
    ("police", '["amenity"="police"]', "police.json", "police station"),
    ("fire_stations", '["amenity"="fire_station"]', "fire_stations.json", "fire station"),
    ("hospitals_all", '["amenity"="hospital"]', "hospitals_all.json", "hospital"),
]

KEEP_TAGS = ("opening_hours", "phone", "wheelchair")

DEDUPE_METERS = 30.0


def overpass_query(tag_filter: str) -> str:
    return (
        "[out:json][timeout:180];"
        f"nwr{tag_filter}{BBOX};"
        "out center tags;"
    )


def fetch(query: str) -> dict:
    """POST the query to Overpass, walking the mirror list with backoff."""
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for attempt in range(6):
        mirror = MIRRORS[attempt % len(MIRRORS)]
        req = urllib.request.Request(
            mirror, data=body, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            last_err = e
            wait = 15 * (attempt + 1)
            print(f"  [warn] {mirror} failed ({e}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"All Overpass attempts failed: {last_err}")


def element_coords(el: dict):
    """lat/lon for nodes; centroid ('center') for ways/relations."""
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def to_record(el: dict, fallback_noun: str):
    lat, lon = element_coords(el)
    if lat is None or lon is None:
        return None
    tags = el.get("tags") or {}
    rec = {
        "name": (tags.get("name") or "").strip() or f"Unnamed {fallback_noun}",
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
    }
    for key in KEEP_TAGS:
        val = tags.get(key) or (tags.get("contact:phone") if key == "phone" else None)
        if val:
            rec[key] = val
    return rec


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dedupe(records):
    """Drop records with the same (case-insensitive) name within ~30m.

    Grid-bucketed so it stays O(n). Keeps the record with the most tags
    (opening_hours/phone/wheelchair) when duplicates collide.
    """
    # ~30m grid: 0.00027 deg lat, 0.00045 deg lon at London's latitude
    cell_lat, cell_lon = 0.00030, 0.00048
    grid = {}
    kept = []

    def cell_of(rec):
        return (int(rec["lat"] / cell_lat), int(rec["lon"] / cell_lon))

    # Prefer richer records: sort so tag-rich ones are inserted first
    for rec in sorted(records, key=lambda r: -len(r)):
        cx, cy = cell_of(rec)
        name = rec["name"].lower()
        dup = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other["name"].lower() == name and haversine_m(
                        rec["lat"], rec["lon"], other["lat"], other["lon"]
                    ) <= DEDUPE_METERS:
                        dup = True
                        break
                if dup:
                    break
            if dup:
                break
        if not dup:
            grid.setdefault((cx, cy), []).append(rec)
            kept.append(rec)
    return kept


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for i, (category, tag_filter, filename, noun) in enumerate(CATEGORIES):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_QUERIES)
        print(f"[{category}] querying Overpass ...")
        data = fetch(overpass_query(tag_filter))
        elements = data.get("elements", [])
        records = [r for r in (to_record(el, noun) for el in elements) if r]
        before = len(records)
        records = dedupe(records)
        records.sort(key=lambda r: (r["name"].lower(), r["lat"], r["lon"]))
        out = {
            "category": category,
            "source": "OpenStreetMap via Overpass API (ODbL)",
            "bbox": [WEST, SOUTH, EAST, NORTH],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(records),
            "items": records,
        }
        path = OUT_DIR / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        size_kb = path.stat().st_size / 1024
        print(
            f"[{category}] raw={len(elements)} valid={before} "
            f"deduped={len(records)} -> {path.name} ({size_kb:.0f} KB)"
        )
        summary[category] = len(records)

    print("\nFinal counts:")
    for cat, n in summary.items():
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
