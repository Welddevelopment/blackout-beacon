#!/usr/bin/env python3
"""Build data/postcodes.sqlite: every live GB unit postcode inside the London
map coverage bbox, with WGS84 centroids, for fully-offline postcode lookup.

Source: ONS Postcode Directory (ONSPD), May 2026 release, from the ONS Open
Geography portal (Open Government Licence v3). The ~250 MB source zip is
downloaded to a temp dir (NOT the repo) and streamed without extraction.

Usage (internet required only for the initial download):
    python3 tools/fetch_postcodes_full.py                # download + build
    python3 tools/fetch_postcodes_full.py --zip PATH     # reuse a local zip
    python3 tools/fetch_postcodes_full.py --out data/postcodes.sqlite

Output schema (consumed by beacon_server.py):
    pc      (pc TEXT PRIMARY KEY, lat REAL, lon REAL, outcode TEXT)
            -- pc normalized: uppercase, no spaces ("N209AQ")
    outcode (oc TEXT PRIMARY KEY, lat REAL, lon REAL, n INTEGER)
            -- centroid = mean of member unit postcodes, n = unit count
    meta    (k TEXT PRIMARY KEY, v TEXT)
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request
import zipfile

# ONSPD May 2026 on the ONS Open Geography portal (ArcGIS item, direct zip).
# Newer releases: search https://geoportal.statistics.gov.uk for
# "ONS Postcode Directory" and swap in the new item id.
ONSPD_URL = (
    "https://www.arcgis.com/sharing/rest/content/items/"
    "6fff67d204fd4f339591ed667a6e3642/data"
)

# Map coverage bbox — must match data/london.pmtiles (see data/README.md).
LON_MIN, LON_MAX = -0.52, 0.34
LAT_MIN, LAT_MAX = 51.28, 51.70

BATCH = 50_000

SPOT_CHECKS = ["SW7 2AZ", "N20 9AQ", "E1 4NS", "SE15 5DT", "NW1 2BU"]


def download(url: str, dest: str) -> None:
    print(f"downloading {url}\n         -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "blackout-beacon/1.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        copied = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            if copied % (50 << 20) < (1 << 20):
                print(f"  {copied / (1 << 20):.0f} MB ...")
    print(f"  done ({copied / (1 << 20):.0f} MB)")


def find_member(zf: zipfile.ZipFile) -> str:
    pat = re.compile(r"Data/ONSPD_[A-Z]+_\d{4}_UK\.csv$")
    for name in zf.namelist():
        if pat.search(name):
            return name
    raise SystemExit("could not find Data/ONSPD_*_UK.csv inside the zip")


def build(zip_path: str, out_path: str) -> None:
    zf = zipfile.ZipFile(zip_path)
    member = find_member(zf)
    m = re.search(r"ONSPD_([A-Z]+)_(\d{4})", member)
    source_date = f"{m.group(1).title()} {m.group(2)}" if m else "unknown"
    print(f"parsing {member} (source date: {source_date})")

    tmp_out = out_path + ".tmp"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)
    db = sqlite3.connect(tmp_out)
    db.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE pc      (pc TEXT PRIMARY KEY, lat REAL, lon REAL, outcode TEXT);
        CREATE TABLE outcode (oc TEXT PRIMARY KEY, lat REAL, lon REAL, n INTEGER);
        CREATE TABLE meta    (k TEXT PRIMARY KEY, v TEXT);
        """
    )

    with zf.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        i_pcds = col["pcds"]      # postcode, single internal space
        i_term = col["doterm"]    # termination date; empty = live
        i_grid = col["gridind"]   # 9 = no grid reference (lat/long are dummies)
        i_lat = col["lat"]
        i_lon = col["long"]

        rows, batch, scanned = [], 0, 0
        for rec in reader:
            scanned += 1
            if rec[i_term]:          # terminated postcode
                continue
            if rec[i_grid] == "9":   # no usable grid reference
                continue
            try:
                lat = float(rec[i_lat])
                lon = float(rec[i_lon])
            except ValueError:
                continue
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue
            pcds = rec[i_pcds].strip().upper()
            pc = pcds.replace(" ", "")
            outcode = pcds.split()[0]
            rows.append((pc, round(lat, 6), round(lon, 6), outcode))
            if len(rows) >= BATCH:
                db.executemany("INSERT OR REPLACE INTO pc VALUES (?,?,?,?)", rows)
                batch += len(rows)
                print(f"  {scanned:,} scanned, {batch:,} kept ...")
                rows = []
        if rows:
            db.executemany("INSERT OR REPLACE INTO pc VALUES (?,?,?,?)", rows)

    db.execute(
        """
        INSERT INTO outcode
        SELECT outcode, ROUND(AVG(lat), 6), ROUND(AVG(lon), 6), COUNT(*)
        FROM pc GROUP BY outcode
        """
    )
    n_pc = db.execute("SELECT COUNT(*) FROM pc").fetchone()[0]
    n_oc = db.execute("SELECT COUNT(*) FROM outcode").fetchone()[0]
    meta = {
        "source": "ONS Postcode Directory (ONSPD), Office for National Statistics",
        "source_date": source_date,
        "source_url": ONSPD_URL,
        "licence": (
            "Open Government Licence v3. Contains OS data (c) Crown copyright "
            "and database right 2026; Royal Mail data (c) Royal Mail copyright "
            "and database right 2026; ONS data (c) Crown copyright and "
            "database right 2026."
        ),
        "bbox": f"lon {LON_MIN}..{LON_MAX}, lat {LAT_MIN}..{LAT_MAX}",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "count": str(n_pc),
        "outcode_count": str(n_oc),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp_out, out_path)
    print(f"wrote {out_path}: {n_pc:,} unit postcodes, {n_oc:,} outcodes, "
          f"{os.path.getsize(out_path) / (1 << 20):.1f} MB")


def validate(out_path: str, outcodes_json: str) -> bool:
    db = sqlite3.connect(out_path)
    ok = True

    n = db.execute("SELECT COUNT(*) FROM pc").fetchone()[0]
    plausible = 150_000 <= n <= 400_000
    ok &= plausible
    print(f"[{'ok' if plausible else 'FAIL'}] unit count {n:,} "
          f"(expected 150k-400k for Greater London + margin)")

    for pcds in SPOT_CHECKS:
        row = db.execute(
            "SELECT lat, lon FROM pc WHERE pc = ?", (pcds.replace(" ", ""),)
        ).fetchone()
        if row:
            print(f"[ok] {pcds} -> {row[0]:.6f}, {row[1]:.6f}")
        else:
            ok = False
            print(f"[FAIL] {pcds} not found")

    if os.path.exists(outcodes_json):
        with open(outcodes_json) as f:
            wanted = set(json.load(f))
        have = {r[0] for r in db.execute("SELECT oc FROM outcode")}
        missing = sorted(wanted - have)
        if missing:
            ok = False
            print(f"[FAIL] outcodes in {outcodes_json} missing from outcode "
                  f"table: {missing}")
        else:
            print(f"[ok] all {len(wanted)} outcodes from {outcodes_json} "
                  f"present in outcode table ({len(have)} total)")
    else:
        print(f"[skip] {outcodes_json} not found; outcode cross-check skipped")

    db.close()
    return ok


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", help="path to an already-downloaded ONSPD zip")
    ap.add_argument("--url", default=ONSPD_URL, help="ONSPD zip download URL")
    ap.add_argument("--out", default=os.path.join(root, "data", "postcodes.sqlite"))
    args = ap.parse_args()

    zip_path = args.zip
    if not zip_path:
        zip_path = os.path.join(tempfile.gettempdir(), "ONSPD_download.zip")
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            print(f"reusing cached {zip_path}")
        else:
            download(args.url, zip_path)

    build(zip_path, args.out)
    ok = validate(args.out, os.path.join(root, "data", "postcodes.json"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
