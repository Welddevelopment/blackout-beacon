"""Offline UK-postcode -> coordinates lookup for Blackout Beacon.

Python 3.9 STDLIB ONLY - no third-party imports. Answers postcode queries
from the read-only sqlite bundle produced by the data pipeline
(data/postcodes.sqlite):

    pc(pc TEXT PRIMARY KEY, lat REAL, lon REAL, outcode TEXT)   -- pc unspaced
    outcode(oc TEXT PRIMARY KEY, lat REAL, lon REAL, n INTEGER)
    meta(k TEXT PRIMARY KEY, v TEXT)

Public API:

    import postcode_lookup
    r = postcode_lookup.lookup("n20 9aq")
    # r is None if unknown (or the sqlite has not been deployed yet), else:
    # {"postcode": "N20 9AQ", "lat": 51.6169, "lon": -0.1755,
    #  "precision": "unit", "outcode": "N20"}
    # Outward-only input ("n20"), or a unit that misses but whose outward
    # district is known, resolve at district precision:
    # {"postcode": "N20", "lat": ..., "lon": ..., "precision": "outcode",
    #  "outcode": "N20"}

Notes:
- Input is normalised (uppercase, strip every non-alphanumeric) before any
  query, so "n20 9aq", "N209AQ" and " n20-9aq. " are equivalent. Lookups use
  parameterised queries only - normalised input never reaches SQL text.
- The sqlite is generated after deploy and may not exist yet: every lookup
  lazily retries the open until the file appears, then caches the connection.
  Missing/half-written file -> None (same contract as an unknown postcode).
- Thread safety: beacon_server's ThreadingHTTPServer runs each request on its
  own thread, and a sqlite3 connection must not be shared across threads - so
  each thread opens (and caches) its own read-only connection via
  threading.local() rather than serialising every lookup behind one
  shared-connection lock. mode=ro opens cost microseconds and read-only
  readers never contend, so lock-free per-thread connections win.
"""
import os
import re
import sqlite3
import threading
from urllib.parse import quote

__all__ = ["lookup"]

DB_PATH = os.environ.get(
    "BEACON_POSTCODES_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "data", "postcodes.sqlite"))

_INWARD_LEN = 3     # UK inward code is always 3 chars (the "9AQ" of N20 9AQ)
_MAX_NORM_LEN = 7   # longest valid UK unit postcode once normalised

_local = threading.local()


def _connect(path):
    """Per-thread cached read-only connection; None while the file is absent."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(path)
    if conn is None:
        try:  # mode=ro never creates the file; %-quote (dir name has a space)
            conn = sqlite3.connect("file:" + quote(path) + "?mode=ro",
                                   uri=True)
        except sqlite3.Error:
            return None  # not deployed yet - retried on the next lookup
        conns[path] = conn
    return conn


def _drop(path):
    """Forget this thread's connection (file replaced/corrupt) -> reopen."""
    conns = getattr(_local, "conns", None)
    conn = conns.pop(path, None) if conns else None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _normalize(raw):
    """Uppercase and strip everything non-alphanumeric ("n20 9aq" -> "N209AQ")."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def lookup(raw, path=None):
    """Resolve raw user postcode input to coordinates.

    Returns {"postcode": display, "lat": float, "lon": float,
             "precision": "unit"|"outcode", "outcode": str} or None when the
    input matches nothing (or the sqlite is not available). Unit hits render
    with the display space before the final 3 chars ("N20 9AQ")."""
    norm = _normalize(raw)
    if not norm or len(norm) > _MAX_NORM_LEN:
        return None
    if path is None:
        path = DB_PATH
    conn = _connect(path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT lat, lon, outcode FROM pc WHERE pc = ?",
                           (norm,)).fetchone()
        if row is not None:
            return {"postcode": norm[:-_INWARD_LEN] + " " + norm[-_INWARD_LEN:],
                    "lat": row[0], "lon": row[1],
                    "precision": "unit", "outcode": row[2]}
        # outward-only input (<= 4 chars can only be a district), or a full
        # unit that missed -> fall back to its outward district centroid
        oc = norm if len(norm) <= 4 else norm[:-_INWARD_LEN]
        row = conn.execute("SELECT lat, lon FROM outcode WHERE oc = ?",
                           (oc,)).fetchone()
        if row is not None:
            return {"postcode": oc, "lat": row[0], "lon": row[1],
                    "precision": "outcode", "outcode": oc}
        return None
    except sqlite3.Error:
        _drop(path)  # half-written/replaced file - fresh open next lookup
        return None
