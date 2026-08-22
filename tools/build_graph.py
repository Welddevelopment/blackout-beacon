#!/usr/bin/env python3
"""Build a compact walking graph for London from an OpenStreetMap .osm.pbf extract.

Run ONLINE (build phase) inside the project venv (needs `pip install osmium`):

    .venv/bin/python tools/build_graph.py [path/to/extract.osm.pbf]

Default input:  data/graph/greater-london-latest.osm.pbf
                (download: https://download.geofabrik.de/europe/united-kingdom/england/greater-london-latest.osm.pbf)
Outputs:        data/graph/london.graph  (custom binary, loaded by routes.py with stdlib only)
                data/graph/meta.json     (counts, bbox, format notes)

Pipeline:
  1. Pass 1 over ways: find walkable highway=* ways, detect nodes shared by >1 way
     (intersections).
  2. Pass 2 with node locations: split each walkable way into segments at
     intersections; keep full geometry per segment.
  3. Contract degree-2 chains (segments joined end-to-end with no junction merge
     into one edge, geometry preserved as a polyline).
  4. Re-split any edge longer than SPLIT_OVER_M at ~SPLIT_TARGET_M intervals so
     nearest-node snapping stays accurate on long roads/park paths.
  5. Keep only the largest connected component (drops private/dead islands).
  6. Serialize: int32 arrays (microdegree coords, CSR adjacency, geometry pool).

Binary layout (little-endian int32 unless noted), after 8-byte magic b"BBGRAPH1"
and 4 uint32 header fields [num_nodes, num_halfedges, num_edges, num_geom_points]:

    lat_e6      int32[num_nodes]        node latitude  * 1e6
    lon_e6      int32[num_nodes]        node longitude * 1e6
    adj_off     int32[num_nodes + 1]    CSR offsets into the halfedge arrays
    adj_nbr     int32[num_halfedges]    neighbour node index
    adj_len     int32[num_halfedges]    edge length in decimetres
    adj_geom    int32[num_halfedges]    (edge_index << 1) | reversed_flag
    geom_off    int32[num_edges + 1]    offsets into geometry point arrays
    geom_lat    int32[num_geom_points]  polyline latitude  * 1e6 (stored u -> v)
    geom_lon    int32[num_geom_points]  polyline longitude * 1e6
"""
import json
import math
import os
import struct
import sys
import time
from array import array

try:
    import osmium
except ImportError:
    sys.exit("pyosmium missing - run:  .venv/bin/pip install osmium")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GRAPH_DIR = os.path.join(ROOT, "data", "graph")
DEFAULT_PBF = os.path.join(GRAPH_DIR, "greater-london-latest.osm.pbf")
OUT_GRAPH = os.path.join(GRAPH_DIR, "london.graph")
OUT_META = os.path.join(GRAPH_DIR, "meta.json")

MAGIC = b"BBGRAPH1"
SPLIT_OVER_M = 400.0   # edges longer than this get re-split ...
SPLIT_TARGET_M = 250.0  # ... into ~250 m pieces (bounds snap error)
DROP_LOOP_UNDER_M = 30.0  # tiny self-loops are noise

WALKABLE = frozenset((
    "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street", "service",
    "pedestrian", "footway", "path", "steps", "track",
    "cycleway", "bridleway", "corridor",
))
FOOT_OK = frozenset(("yes", "designated", "permissive"))


def is_walkable(tags):
    hw = tags.get("highway")
    if hw not in WALKABLE:
        return False
    if tags.get("area") == "yes":
        return False
    foot = tags.get("foot")
    if foot in ("no", "private"):
        return False
    if tags.get("access") in ("no", "private") and foot not in FOOT_OK:
        return False
    return True


# ---------------------------------------------------------------- pass 1
class UsageScan(osmium.SimpleHandler):
    """Find node ids used by more than one walkable way (or twice in one way)."""

    def __init__(self):
        super().__init__()
        self.seen = set()
        self.shared = set()
        self.n_ways = 0

    def way(self, w):
        if not is_walkable(w.tags):
            return
        self.n_ways += 1
        seen, shared = self.seen, self.shared
        for n in w.nodes:
            r = n.ref
            if r in seen:
                shared.add(r)
            else:
                seen.add(r)


# ---------------------------------------------------------------- pass 2
class SegmentBuilder(osmium.SimpleHandler):
    """Split walkable ways into segments at intersection nodes, with geometry."""

    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.vid = {}          # osm node id -> vertex index
        self.vlat = array("i")
        self.vlon = array("i")
        self.seg_u = array("i")
        self.seg_v = array("i")
        self.seg_len = array("d")
        self.seg_geom = []     # flat [lat_e6, lon_e6, ...] per segment

    def _vertex(self, ref, lat_e6, lon_e6):
        idx = self.vid.get(ref)
        if idx is None:
            idx = len(self.vlat)
            self.vid[ref] = idx
            self.vlat.append(lat_e6)
            self.vlon.append(lon_e6)
        return idx

    def way(self, w):
        if not is_walkable(w.tags):
            return
        shared = self.shared
        pts = []  # (ref, lat_e6, lon_e6)
        for n in w.nodes:
            loc = n.location
            if not loc.valid():
                continue
            pts.append((n.ref, int(round(loc.lat * 1e6)),
                        int(round(loc.lon * 1e6))))
        if len(pts) < 2:
            return
        last = len(pts) - 1
        start = 0
        for i in range(1, len(pts)):
            if i == last or pts[i][0] in shared:
                self._emit(pts, start, i)
                start = i

    def _emit(self, pts, a, b):
        chunk = pts[a:b + 1]
        length = 0.0
        geom = array("i")
        prev = None
        for _, la, lo in chunk:
            if prev is not None:
                length += dist_m(prev[0], prev[1], la, lo)
            geom.append(la)
            geom.append(lo)
            prev = (la, lo)
        if length <= 0.0 and chunk[0][0] == chunk[-1][0]:
            return
        u = self._vertex(chunk[0][0], chunk[0][1], chunk[0][2])
        v = self._vertex(chunk[-1][0], chunk[-1][1], chunk[-1][2])
        self.seg_u.append(u)
        self.seg_v.append(v)
        self.seg_len.append(length)
        self.seg_geom.append(geom)


COSLAT = math.cos(math.radians(51.5))
M_PER_DEG = 111194.9  # mean metres per degree latitude


def dist_m(lat1_e6, lon1_e6, lat2_e6, lon2_e6):
    """Equirectangular approximation - plenty accurate at city scale."""
    dlat = (lat2_e6 - lat1_e6) * 1e-6
    dlon = (lon2_e6 - lon1_e6) * 1e-6 * COSLAT
    return math.hypot(dlat, dlon) * M_PER_DEG


# ---------------------------------------------------------------- contraction
def contract(sb):
    """Merge degree-2 chains; returns edge lists (u, v, length, geometry)."""
    nseg = len(sb.seg_u)
    nvert = len(sb.vlat)
    deg = array("i", bytes(4 * nvert))
    adj = [[] for _ in range(nvert)]
    for s in range(nseg):
        u, v = sb.seg_u[s], sb.seg_v[s]
        deg[u] += 1
        deg[v] += 1
        adj[u].append(s)
        adj[v].append(s)

    visited = bytearray(nseg)
    out_u, out_v, out_len, out_geom = [], [], [], []

    def other_end(s, at):
        return sb.seg_v[s] if sb.seg_u[s] == at else sb.seg_u[s]

    def walk(start_v, first_seg):
        """Walk from start_v through degree-2 vertices; emit merged edge."""
        geom = array("i")
        total = 0.0
        cur_v = start_v
        s = first_seg
        while True:
            visited[s] = 1
            g = sb.seg_geom[s]
            if sb.seg_u[s] == cur_v:
                seq = g
            else:
                seq = array("i")
                for i in range(len(g) - 2, -2, -2):
                    seq.append(g[i])
                    seq.append(g[i + 1])
            if len(geom):
                geom.extend(seq[2:])  # skip duplicated joint point
            else:
                geom.extend(seq)
            total += sb.seg_len[s]
            nxt = other_end(s, cur_v)
            if deg[nxt] != 2 or nxt == start_v:
                return nxt, total, geom
            a, b = adj[nxt]
            s2 = b if a == s else a
            if s2 == s or visited[s2]:  # self-loop / closed chain
                return nxt, total, geom
            cur_v, s = nxt, s2

    for v in range(nvert):
        if deg[v] == 2:
            continue
        for s in adj[v]:
            if visited[s]:
                continue
            end, total, geom = walk(v, s)
            out_u.append(v)
            out_v.append(end)
            out_len.append(total)
            out_geom.append(geom)

    # pure cycles of degree-2 vertices
    for s in range(nseg):
        if visited[s]:
            continue
        v = sb.seg_u[s]
        end, total, geom = walk(v, s)
        out_u.append(v)
        out_v.append(end)
        out_len.append(total)
        out_geom.append(geom)

    return out_u, out_v, out_len, out_geom


def length_split(u, v, lengths, geoms, vlat, vlon):
    """Split long edges at ~SPLIT_TARGET_M so snapping stays tight."""
    nu, nv, nl, ng = [], [], [], []
    vlat = array("i", vlat)
    vlon = array("i", vlon)

    def new_vertex(la, lo):
        vlat.append(la)
        vlon.append(lo)
        return len(vlat) - 1

    for e in range(len(u)):
        L = lengths[e]
        g = geoms[e]
        if L <= SPLIT_OVER_M or len(g) <= 4:
            if u[e] == v[e] and L < DROP_LOOP_UNDER_M:
                continue
            nu.append(u[e])
            nv.append(v[e])
            nl.append(L)
            ng.append(g)
            continue
        cur_u = u[e]
        piece = array("i", g[0:2])
        acc = 0.0
        npts = len(g) // 2
        for i in range(1, npts):
            la1, lo1 = g[2 * i - 2], g[2 * i - 1]
            la2, lo2 = g[2 * i], g[2 * i + 1]
            acc += dist_m(la1, lo1, la2, lo2)
            piece.append(la2)
            piece.append(lo2)
            remaining = i < npts - 1
            if acc >= SPLIT_TARGET_M and remaining:
                w = new_vertex(la2, lo2)
                nu.append(cur_u)
                nv.append(w)
                nl.append(acc)
                ng.append(piece)
                cur_u = w
                piece = array("i", (la2, lo2))
                acc = 0.0
        nu.append(cur_u)
        nv.append(v[e])
        nl.append(acc)
        ng.append(piece)
    return nu, nv, nl, ng, vlat, vlon


def largest_component(u, v, nvert):
    parent = array("i", range(nvert))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(u)):
        a, b = find(u[i]), find(v[i])
        if a != b:
            parent[a] = b
    counts = {}
    for i in range(len(u)):
        r = find(u[i])
        counts[r] = counts.get(r, 0) + 1
    best = max(counts, key=counts.get)
    keep = [i for i in range(len(u)) if find(u[i]) == best]
    return keep, counts


def serialize(u, v, lengths, geoms, vlat, vlon, keep):
    # renumber vertices used by kept edges
    remap = {}
    lat = array("i")
    lon = array("i")
    for e in keep:
        for x in (u[e], v[e]):
            if x not in remap:
                remap[x] = len(lat)
                lat.append(vlat[x])
                lon.append(vlon[x])
    nnodes = len(lat)
    nedges = len(keep)

    # geometry pool
    geom_off = array("i", [0])
    glat = array("i")
    glon = array("i")
    for e in keep:
        g = geoms[e]
        for i in range(0, len(g), 2):
            glat.append(g[i])
            glon.append(g[i + 1])
        geom_off.append(len(glat))

    # CSR halfedges
    degree = array("i", bytes(4 * nnodes))
    eu = array("i")
    ev = array("i")
    elen = array("i")
    for k, e in enumerate(keep):
        a, b = remap[u[e]], remap[v[e]]
        eu.append(a)
        ev.append(b)
        elen.append(max(1, int(round(lengths[e] * 10))))  # decimetres
        degree[a] += 1
        degree[b] += 1
    adj_off = array("i", bytes(4 * (nnodes + 1)))
    for i in range(nnodes):
        adj_off[i + 1] = adj_off[i] + degree[i]
    cursor = array("i", adj_off[:-1])
    nhalf = adj_off[nnodes]
    adj_nbr = array("i", bytes(4 * nhalf))
    adj_len = array("i", bytes(4 * nhalf))
    adj_geom = array("i", bytes(4 * nhalf))
    for k in range(nedges):
        a, b, L = eu[k], ev[k], elen[k]
        p = cursor[a]
        cursor[a] = p + 1
        adj_nbr[p] = b
        adj_len[p] = L
        adj_geom[p] = k << 1
        p = cursor[b]
        cursor[b] = p + 1
        adj_nbr[p] = a
        adj_len[p] = L
        adj_geom[p] = (k << 1) | 1
    return (lat, lon, adj_off, adj_nbr, adj_len, adj_geom,
            geom_off, glat, glon, nnodes, nhalf, nedges, len(glat))


def main():
    pbf = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PBF
    if not os.path.exists(pbf):
        sys.exit("input not found: %s\nDownload the Geofabrik greater-london "
                 "extract first (see module docstring)." % pbf)
    t0 = time.time()
    print("pass 1: scanning ways for intersections ...")
    scan = UsageScan()
    scan.apply_file(pbf)
    print("  walkable ways: %d, shared nodes: %d  (%.1fs)"
          % (scan.n_ways, len(scan.shared), time.time() - t0))

    t1 = time.time()
    print("pass 2: building segments with geometry ...")
    sb = SegmentBuilder(scan.shared)
    sb.apply_file(pbf, locations=True, idx="flex_mem")
    del scan
    print("  segments: %d, vertices: %d  (%.1fs)"
          % (len(sb.seg_u), len(sb.vlat), time.time() - t1))

    t2 = time.time()
    print("contracting degree-2 chains ...")
    u, v, lengths, geoms = contract(sb)
    vlat, vlon = sb.vlat, sb.vlon
    del sb
    print("  edges after contraction: %d  (%.1fs)" % (len(u), time.time() - t2))

    print("splitting long edges ...")
    u, v, lengths, geoms, vlat, vlon = length_split(u, v, lengths, geoms,
                                                    vlat, vlon)
    print("  edges after split: %d" % len(u))

    print("keeping largest connected component ...")
    keep, counts = largest_component(u, v, len(vlat))
    print("  components: %d, kept %d/%d edges"
          % (len(counts), len(keep), len(u)))

    print("serializing ...")
    (lat, lon, adj_off, adj_nbr, adj_len, adj_geom, geom_off, glat, glon,
     nnodes, nhalf, nedges, npts) = serialize(u, v, lengths, geoms,
                                              vlat, vlon, keep)

    os.makedirs(GRAPH_DIR, exist_ok=True)
    with open(OUT_GRAPH, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<4I", nnodes, nhalf, nedges, npts))
        for arr in (lat, lon, adj_off, adj_nbr, adj_len, adj_geom,
                    geom_off, glat, glon):
            arr.tofile(f)
    size_mb = os.path.getsize(OUT_GRAPH) / 1e6
    bbox = [min(lon) * 1e-6, min(lat) * 1e-6, max(lon) * 1e-6, max(lat) * 1e-6]
    meta = {
        "format": MAGIC.decode(),
        "source": os.path.basename(pbf),
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nodes": nnodes,
        "halfedges": nhalf,
        "edges": nedges,
        "geometry_points": npts,
        "bbox_lon_lat": bbox,
        "file_mb": round(size_mb, 1),
        "build_seconds": round(time.time() - t0, 1),
    }
    with open(OUT_META, "w") as f:
        json.dump(meta, f, indent=2)
    print("wrote %s (%.1f MB), %d nodes, %d edges, %d geom points in %.0fs"
          % (OUT_GRAPH, size_mb, nnodes, nedges, npts, time.time() - t0))
    print("bbox lon/lat: %s" % bbox)


if __name__ == "__main__":
    main()
