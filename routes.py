"""Offline walking-route engine for Blackout Beacon.

Python 3.9 STDLIB ONLY - no third-party imports. Loads the compact binary
graph produced by tools/build_graph.py (data/graph/london.graph) and answers
point-to-point walking routes over the London street network with A*.

Public API:

    import routes
    routes.load_graph()          # optional eager preload (lazy + cached anyway)
    r = routes.route(51.4941, -0.1738, 51.4844, -0.1816)
    # r is None if either endpoint is outside coverage, else:
    # {"distance_m": 1421, "duration_min": 17.1,
    #  "polyline": [[51.494102, -0.173792], ...]}   # [lat, lon] pairs

Notes:
- duration assumes 5 km/h walking speed.
- Endpoints are snapped to the nearest graph node (max 1 km snap radius,
  grid-bucketed index - no linear scans). polyline[0]/polyline[-1] are the
  snapped street points, not the raw query coords.
- A* uses a slightly inflated (x1.2) straight-line heuristic: routes are
  within a few percent of shortest and typically 5-10x faster to compute.
  Fine for guidance; not a certified shortest path.
- Memory: graph arrays + snap index ~= 250-400 MB. Load: a few seconds.
"""
import math
import os
import struct
import threading
from array import array
from heapq import heappush, heappop

__all__ = ["load_graph", "route", "coverage_bbox"]

_GRAPH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "graph", "london.graph")
_MAGIC = b"BBGRAPH1"

WALK_M_PER_MIN = 5000.0 / 60.0   # 5 km/h
_HEURISTIC_W = 1.2               # inflated heuristic (see module docstring)
_SNAP_MAX_M = 1000.0             # give up snapping beyond this
_CELL_LAT = 2700                 # grid cell size in 1e-6 deg (~300 m)
_CELL_LON = 4300                 # ~300 m at London latitude

# metres per 1e-6 degree
_KLAT = 0.1111949
_KLON = _KLAT * math.cos(math.radians(51.5))

_lock = threading.Lock()
_graph = None


class _Graph(object):
    __slots__ = ("n", "lat", "lon", "adj_off", "adj_nbr", "adj_len",
                 "adj_geom", "geom_off", "glat", "glon", "grid",
                 "min_lat", "max_lat", "min_lon", "max_lon")


def load_graph(path=_GRAPH_PATH):
    """Load (once) and return the graph. Thread-safe, cached."""
    global _graph
    if _graph is not None:
        return _graph
    with _lock:
        if _graph is not None:
            return _graph
        _graph = _load(path)
        return _graph


def _load(path):
    g = _Graph()
    with open(path, "rb") as f:
        if f.read(8) != _MAGIC:
            raise ValueError("bad graph file: " + path)
        n, nhalf, nedges, npts = struct.unpack("<4I", f.read(16))
        g.n = n

        def arr(count):
            a = array("i")
            a.fromfile(f, count)
            return a

        g.lat = arr(n)
        g.lon = arr(n)
        g.adj_off = arr(n + 1)
        g.adj_nbr = arr(nhalf)
        g.adj_len = arr(nhalf)
        g.adj_geom = arr(nhalf)
        g.geom_off = arr(nedges + 1)
        g.glat = arr(npts)
        g.glon = arr(npts)

    g.min_lat = min(g.lat)
    g.max_lat = max(g.lat)
    g.min_lon = min(g.lon)
    g.max_lon = max(g.lon)

    # grid-bucketed snap index: cell key -> array of node indices
    grid = {}
    lat, lon = g.lat, g.lon
    cl, co = _CELL_LAT, _CELL_LON
    for i in range(n):
        k = (lat[i] // cl) * 1048576 + (lon[i] // co) + 524288
        b = grid.get(k)
        if b is None:
            grid[k] = array("i", (i,))
        else:
            b.append(i)
    g.grid = grid
    return g


def coverage_bbox():
    """[min_lon, min_lat, max_lon, max_lat] of the loaded graph (degrees)."""
    g = load_graph()
    return [g.min_lon * 1e-6, g.min_lat * 1e-6,
            g.max_lon * 1e-6, g.max_lat * 1e-6]


def _nearest(g, lat_deg, lon_deg):
    """Nearest graph node via expanding grid-ring search; None beyond 1 km."""
    la = int(round(lat_deg * 1e6))
    lo = int(round(lon_deg * 1e6))
    margin = 12000  # ~1.2 km in 1e-6 deg
    if not (g.min_lat - margin <= la <= g.max_lat + margin and
            g.min_lon - margin <= lo <= g.max_lon + margin):
        return None
    cl, co = _CELL_LAT, _CELL_LON
    ca, cb = la // cl, lo // co
    grid, glat, glon = g.grid, g.lat, g.lon
    klat, klon = _KLAT, _KLON
    best = -1
    best_d = _SNAP_MAX_M
    max_r = 4  # 4 rings x ~300 m > snap radius
    for r in range(max_r + 1):
        # nearest possible point in ring r is ~(r-1)*280 m away
        if best >= 0 and best_d <= (r - 1) * 280.0:
            break
        for da in range(-r, r + 1):
            for db in range(-r, r + 1):
                if max(abs(da), abs(db)) != r:
                    continue
                bucket = grid.get((ca + da) * 1048576 + (cb + db) + 524288)
                if bucket is None:
                    continue
                for i in bucket:
                    dx = (glat[i] - la) * klat
                    dy = (glon[i] - lo) * klon
                    d = math.sqrt(dx * dx + dy * dy)
                    if d < best_d:
                        best_d = d
                        best = i
    return best if best >= 0 else None


def route(from_lat, from_lon, to_lat, to_lon):
    """Walking route between two WGS84 points.

    Returns {"distance_m": int, "duration_min": float,
             "polyline": [[lat, lon], ...]} or None when either endpoint is
    outside coverage (beyond ~1 km of any London street).
    """
    g = load_graph()
    s = _nearest(g, from_lat, from_lon)
    if s is None:
        return None
    t = _nearest(g, to_lat, to_lon)
    if t is None:
        return None
    if s == t:
        p = [round(g.lat[s] * 1e-6, 6), round(g.lon[s] * 1e-6, 6)]
        return {"distance_m": 0, "duration_min": 0.0, "polyline": [p, p]}

    dist_dm, pred = _astar(g, s, t)
    if dist_dm is None:
        return None  # disconnected (should not happen: single component)

    polyline = _build_polyline(g, s, t, pred)
    distance_m = int(round(dist_dm / 10.0))
    return {
        "distance_m": distance_m,
        "duration_min": round(distance_m / WALK_M_PER_MIN, 1),
        "polyline": polyline,
    }


def _astar(g, s, t):
    lat, lon = g.lat, g.lon
    off, nbr, ln = g.adj_off, g.adj_nbr, g.adj_len
    tla, tlo = lat[t], lon[t]
    klat = _KLAT * 10.0 * _HEURISTIC_W   # decimetre-scaled, inflated
    klon = _KLON * 10.0 * _HEURISTIC_W
    sqrt = math.sqrt

    dist = {s: 0}
    pred = {}
    dx = (lat[s] - tla) * klat
    dy = (lon[s] - tlo) * klon
    heap = [(sqrt(dx * dx + dy * dy), s)]
    push, pop = heappush, heappop

    while heap:
        f, u = pop(heap)
        if u == t:
            return dist[t], pred
        du = dist[u]
        # lazy deletion: stale entry if a better g-cost was already settled
        h_dx = (lat[u] - tla) * klat
        h_dy = (lon[u] - tlo) * klon
        if f - sqrt(h_dx * h_dx + h_dy * h_dy) > du + 1e-6:
            continue
        for i in range(off[u], off[u + 1]):
            v = nbr[i]
            nd = du + ln[i]
            dv = dist.get(v)
            if dv is not None and dv <= nd:
                continue
            dist[v] = nd
            pred[v] = (u, i)
            dx = (lat[v] - tla) * klat
            dy = (lon[v] - tlo) * klon
            push(heap, (nd + sqrt(dx * dx + dy * dy), v))
    return None, None


def _build_polyline(g, s, t, pred):
    # collect halfedges t -> s, then emit forward
    chain = []
    u = t
    while u != s:
        p, he = pred[u]
        chain.append(he)
        u = p
    chain.reverse()

    geom_off, glat, glon = g.geom_off, g.glat, g.glon
    adj_geom = g.adj_geom
    out = []
    for he in chain:
        code = adj_geom[he]
        e = code >> 1
        a, b = geom_off[e], geom_off[e + 1]
        idx = range(b - 1, a - 1, -1) if (code & 1) else range(a, b)
        first = True
        for i in idx:
            if first and out:
                first = False
                continue  # joint point already emitted by previous edge
            first = False
            out.append([round(glat[i] * 1e-6, 6), round(glon[i] * 1e-6, 6)])
    return out
