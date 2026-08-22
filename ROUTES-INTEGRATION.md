# ROUTES-INTEGRATION - wiring the offline walking-route engine into beacon_server.py

Owner of this contract: the routing agent (`routes.py`, `tools/build_graph.py`,
`data/graph/*`). The integrator adds the snippet below to `beacon_server.py`;
nothing else is required.

## Endpoint

`GET /api/route?from=<lat>,<lon>&to=<lat>,<lon>`

- 200: `{"distance_m": 1710, "duration_min": 20.5, "polyline": [[lat, lon], ...]}`
  - `distance_m`: int, metres along streets
  - `duration_min`: float, at 5 km/h walking speed
  - `polyline`: [lat, lon] pairs (6 dp) following real streets - feed straight
    into MapLibre after swapping to [lon, lat] per point
- 404: either endpoint outside coverage (beyond ~1 km of any Greater London
  street) -> body `{"error": "outside coverage"}`
- 400: malformed/missing params

Note for map.html: `polyline[0]` / `polyline[-1]` are the snapped street
points, typically <30 m from the query coords - draw a short dashed connector
from the raw pin to each end if you want it seamless.

## Integration (3 steps)

1. Import, top of `beacon_server.py`:

```python
import routes
```

2. Dispatch line inside `Beacon._route()`, next to the other `/api/*` branches:

```python
        if path == "/api/route":
            return self._api_route(query)
```

3. Handler method on `Beacon` (matches the existing `_api_*` style):

```python
    def _api_route(self, query):
        try:
            p = parse_qs(query)
            f_lat, f_lon = (float(x) for x in p["from"][0].split(","))
            t_lat, t_lon = (float(x) for x in p["to"][0].split(","))
        except (KeyError, IndexError, ValueError):
            return self._send_json({"error": "bad params"}, status=400)
        r = routes.route(f_lat, f_lon, t_lat, t_lon)
        return (self._send_json(r) if r is not None
                else self._send_json({"error": "outside coverage"}, status=404))
```

## Preload at boot vs first request

`routes.load_graph()` is lazy and cached; first call loads the graph. Cold
load measured at **0.14 s** on this machine, so first-request loading is
already fine - but for zero first-hit jitter add one line in `main()` before
`serve_forever()`:

```python
    routes.load_graph()   # optional eager preload, ~0.15 s
```

Loading is thread-safe (double-checked lock), and route computation is
read-only, so `ThreadingHTTPServer` concurrency is safe with no further work.

## Resource envelope (measured on this M5, Python 3.9.6)

| metric | measured | budget |
|---|---|---|
| disk (`data/graph/london.graph`) | 52.7 MB | <= 150 MB |
| cold load (read + snap index) | 0.14 s | <= 20 s |
| process peak RSS after load + routing | 88 MB | <= 500 MB |
| warm route latency (hospital-scale, <= 5 km) | 0.4-4.3 ms | < 300 ms |
| warm route latency (52 km cross-London stress) | ~10 ms | - |

## Coverage

Full Greater London: lon -0.518..0.337, lat 51.283..51.699 (Geofabrik
greater-london extract, built 2026-08-22). Walkable OSM classes only
(motorways excluded, `foot=no`/private excluded); largest connected component
kept, so snapped points are always mutually reachable. Endpoints snap to the
nearest graph node within 1 km via a grid-bucketed index; outside that ->
`route()` returns `None` -> 404. Known quirk: a pin dropped on the Heathrow
T2/3 apron itself is >1 km from a kept walkable way and 404s (T5, Hatton
Cross etc. all fine).

Routes use A* with a x1.2 inflated straight-line heuristic: paths are within a
few percent of true shortest - the right trade for guidance speed. Distances
are real street distances, not crow-fly.

## Test results (2026-08-22, cold load 0.14 s, peak RSS 88 MB)

10 real routes - `ratio` = street distance / crow-fly (1.1-1.6 is healthy for
city walking; Trafalgar->St Thomas' is high because the Thames forces the
Westminster Bridge crossing):

| route | crow km | route km | ratio | min | pts | ms |
|---|---|---|---|---|---|---|
| South Kensington stn -> Chelsea & Westminster Hosp | 1.21 | 1.71 | 1.42 | 20.5 | 108 | 0.7 |
| Shoreditch High St -> Royal London Hosp | 1.33 | 1.60 | 1.20 | 19.2 | 151 | 0.8 |
| Brixton stn -> King's College Hosp | 1.58 | 2.11 | 1.33 | 25.3 | 161 | 1.4 |
| Stratford stn -> Homerton Univ Hosp | 3.09 | 3.88 | 1.26 | 46.6 | 291 | 2.1 |
| Camden Town -> Royal Free Hosp | 2.18 | 2.48 | 1.14 | 29.8 | 264 | 1.0 |
| Trafalgar Sq -> St Thomas' Hosp | 1.20 | 1.95 | 1.62 | 23.4 | 171 | 2.6 |
| Paddington stn -> St Mary's Hosp | 0.26 | 0.41 | 1.55 | 4.9 | 25 | 0.4 |
| East Croydon stn -> Croydon Univ Hosp | 1.85 | 2.56 | 1.39 | 30.7 | 202 | 0.6 |
| Ealing Broadway -> Ealing Hosp (Southall) | 3.10 | 3.37 | 1.09 | 40.4 | 253 | 2.2 |
| Cutty Sark Greenwich -> QE Hosp Woolwich | 3.91 | 4.52 | 1.16 | 54.2 | 368 | 4.3 |

All ten are within +-25% of real-world walking distances (spot-checked against
known geography; the +30%-looking Trafalgar row is vs a low pre-estimate - the
actual walk via Westminster Bridge is ~1.9 km, matching).

Stress: Uxbridge->Stratford 36.4 km / 1889 pts / 10 ms; Heathrow T5->Romford
52.2 km / 3214 pts / 9 ms.

Out-of-coverage: Birmingham, Reading, North Sea -> `None` (404) in <0.3 ms.
Same-point route -> `{"distance_m": 0, "duration_min": 0.0, "polyline": [p, p]}`.
Polyline continuity: max inter-point gap 76 m (a straight road segment);
origin snap offset 8 m on the Stratford case.

## Regeneration

See `data/graph/README.md`. `routes.py` and serving are stdlib-only; osmium +
the 128 MB PBF download are build-time only.
