#!/usr/bin/env python3
"""Blackout Beacon — captive-portal server.

A MacBook broadcasts its own WiFi hotspot during a blackout; phones that join
get every DNS name resolved to this laptop (wildcard DNS on UDP :53) and are
funnelled by their OS connectivity probes onto the Beacon page (HTTP :80),
which serves an offline emergency AI assistant backed by local Ollama.

DNS + probe-redirect logic is lifted from the field-tested prototype
(beacontest/captive.py) that popped the captive sheet on real phones.

OFFLINE GUARANTEE: this process opens NO outbound connections except to
loopback (llm.py enforces that its Ollama URL is 127.0.0.1/localhost and is
the only module that dials out). BYTES_TO_INTERNET below is 0 by construction.

Stdlib only. Run via ./run.sh (ports 53/80 need root).
Dev mode:  BEACON_HTTP_PORT=8080 BEACON_DNS=0 python3 beacon_server.py
"""
import json
import math
import os
import socket
import struct
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

import llm
import postcode_lookup
import routes

# ---------------------------------------------------------------- config

BEACON_IP = os.environ.get("BEACON_IP", "192.168.2.1")
HTTP_PORT = int(os.environ.get("BEACON_HTTP_PORT", "80"))
DNS_ENABLED = os.environ.get("BEACON_DNS", "1") != "0"
DNS_PORT = int(os.environ.get("BEACON_DNS_PORT", "53"))  # override for non-root dev tests

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DATA_DIR = os.path.join(HERE, "data")  # offline map data (tiles, postcodes, POIs)
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
MAP_PATH = os.path.join(STATIC_DIR, "map.html")
CARDS_DIR = llm.CARDS_DIR
META_PATH = os.path.join(CARDS_DIR, "meta.json")

# Display truth: no code path in this process opens a non-loopback outbound
# socket, so this counter can never be anything but 0.
BYTES_TO_INTERNET = 0

# OS connectivity-probe paths -> always captive-redirect (proven on real phones).
PROBE_PATHS = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/ncsi.txt",
    "/connecttest.txt", "/success.txt", "/canonical.html",
}

_hostname = socket.gethostname().lower()
_hostbase = _hostname[:-len(".local")] if _hostname.endswith(".local") else _hostname
ALLOWED_HOSTS = {
    BEACON_IP, "beacon.help", "beacon",
    _hostname, _hostbase, _hostbase + ".local",
    "127.0.0.1",  # dev convenience (loopback, same spirit as "localhost")
}

_PORT_SUFFIX = "" if HTTP_PORT == 80 else f":{HTTP_PORT}"
PORTAL_URL = f"http://{BEACON_IP}{_PORT_SUFFIX}/"

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
    # offline map assets: vector tiles + glyph PBFs
    ".pmtiles": "application/octet-stream", ".pbf": "application/x-protobuf",
}

# Extensions safe to cache on phones (immutable briefing-phase assets).
# HTML/JSON stay no-store so UI + data iterations reach clients immediately.
CACHEABLE_EXTS = {".js", ".css", ".pmtiles", ".pbf", ".png", ".woff2",
                  ".jpg", ".jpeg", ".webp", ".svg"}

PLACEHOLDER_PAGE = """<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>BEACON</title>
<style>body{background:#052e16;color:#fff;font-family:-apple-system,Roboto,sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;
min-height:95vh;margin:0;text-align:center}
h1{color:#4ade80;font-size:34px;margin:12px 0 6px}p{color:#bbf7d0;font-size:18px;margin:4px 0}
.big{font-size:56px}</style></head><body>
<div class="big">&#128225;</div><h1>BEACON</h1>
<p>Offline emergency assistant is starting up.</p>
<p>The interface has not been deployed yet &mdash; this page retries every 5s.</p>
</body></html>"""

# ---------------------------------------------------------------- logging

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- client tracking

CLIENTS = {}  # ip -> last-seen monotonic ts
CLIENTS_LOCK = threading.Lock()
CLIENT_WINDOW_S = 300


def note_client(ip):
    with CLIENTS_LOCK:
        CLIENTS[ip] = time.monotonic()


def client_count():
    cutoff = time.monotonic() - CLIENT_WINDOW_S
    with CLIENTS_LOCK:
        for ip in [ip for ip, ts in CLIENTS.items() if ts < cutoff]:
            del CLIENTS[ip]
        return len(CLIENTS)


# ---------------------------------------------------------------- generation queue
# Only ONE Ollama generation runs at a time; everyone else holds a ticket and
# is told their live position (multi-phone fairness).

GEN_LOCK = threading.Lock()
ASK_QUEUE = []  # ticket objects, arrival order; index 0 = currently generating
QUEUE_LOCK = threading.Lock()


def queue_depth():
    with QUEUE_LOCK:
        return len(ASK_QUEUE)


# ---------------------------------------------------------------- DNS (from tested prototype)

def dns_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", DNS_PORT))
    log(f"[dns] wildcard responder on :{DNS_PORT} -> {BEACON_IP}")
    while True:
        try:
            data, addr = s.recvfrom(512)
            if len(data) < 12:
                continue
            labels, i = [], 12
            while i < len(data) and data[i] != 0:
                n = data[i]
                labels.append(data[i + 1:i + 1 + n].decode(errors="replace"))
                i += n + 1
            qname = ".".join(labels)
            qtype = data[i + 1:i + 3]  # 2 bytes after the name's null terminator
            i += 5  # null byte + qtype(2) + qclass(2)
            question = data[12:i]
            if qtype in (b"\x00\x01", b"\x00\xff"):  # A / ANY -> answer with us
                answer = (b"\xc0\x0c" + b"\x00\x01" + b"\x00\x01" +
                          struct.pack(">I", 10) + b"\x00\x04" + socket.inet_aton(BEACON_IP))
                resp = (data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x01" +
                        b"\x00\x00\x00\x00" + question + answer)
            else:
                # AAAA/HTTPS/etc: clean NOERROR with zero answers, so phones
                # fall through to their A query instead of choking on a
                # malformed record (protocol-correct per QA finding).
                resp = (data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x00" +
                        b"\x00\x00\x00\x00" + question)
            s.sendto(resp, addr)
            qt_label = "A" if qtype == b"\x00\x01" else "other"
            log(f"[dns] {addr[0]} {qname} ({qt_label}) -> {BEACON_IP}")
        except Exception as e:
            log(f"[dns] error: {e}")


# ---------------------------------------------------------------- HTTP

class Beacon(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BlackoutBeacon/1.0"

    def log_message(self, *a):  # we do our own compact logging
        pass

    # -- helpers ---------------------------------------------------------

    def _log(self, note):
        host = self.headers.get("Host", "-")
        log(f"[http] {self.client_address[0]} {self.command} {self.path} "
            f"host={host} -> {note}")

    def _send_bytes(self, body, status=200, ctype="text/html; charset=utf-8",
                    extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         status=status, ctype="application/json")

    def _redirect(self, note):
        self._log(note)
        self.send_response(302)
        self.send_header("Location", PORTAL_URL)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _host_allowed(self, host):
        return host in ALLOWED_HOSTS or "localhost" in host

    # -- routing ---------------------------------------------------------

    def _api_route(self, query):
        try:
            p = parse_qs(query)
            f_lat, f_lon = (float(x) for x in p["from"][0].split(","))
            t_lat, t_lon = (float(x) for x in p["to"][0].split(","))
            if not all(map(math.isfinite, (f_lat, f_lon, t_lat, t_lon))):
                raise ValueError("non-finite coordinate")  # nan/inf crash int()
        except (KeyError, IndexError, ValueError):
            return self._send_json({"error": "bad params"}, status=400)
        try:
            r = routes.route(f_lat, f_lon, t_lat, t_lon)
        except Exception:
            r = None  # graph missing/corrupt -> same contract as out-of-coverage
        self._log("route" if r is not None else "404 route")
        return (self._send_json(r) if r is not None
                else self._send_json({"error": "outside coverage"}, status=404))

    def _api_postcode(self, query):
        try:
            q = parse_qs(query)["q"][0].strip()
            if not q:
                raise ValueError("empty q")
        except (KeyError, IndexError, ValueError):
            return self._send_json({"error": "bad params"}, status=400)
        try:
            r = postcode_lookup.lookup(q)
        except Exception:
            r = None  # sqlite missing/corrupt -> same contract as unknown code
        self._log("postcode" if r is not None else "404 postcode")
        return (self._send_json(r) if r is not None
                else self._send_json({"error": "not found"}, status=404))

    def _route(self):
        note_client(self.client_address[0])
        path = urlsplit(self.path).path or "/"
        query = urlsplit(self.path).query
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()

        if path in PROBE_PATHS:
            return self._redirect("302 probe")
        if not self._host_allowed(host):
            return self._redirect("302 foreign-host")

        if path in ("/", "/index.html"):
            self._log("200 index")
            return self._serve_index()
        if path in ("/map", "/map.html"):
            self._log("200 map")
            return self._serve_page(MAP_PATH)
        if path == "/api/status":
            self._log("200 status")
            return self._api_status()
        if path == "/api/cards":
            self._log("200 cards")
            return self._api_cards()
        if path == "/api/ask":
            return self._api_ask(query)
        if path == "/api/route":
            return self._api_route(query)
        if path == "/api/postcode":
            return self._api_postcode(query)
        if path.startswith("/static/"):
            return self._serve_tree(STATIC_DIR, path[len("/static/"):])
        if path.startswith("/data/"):
            # map data (london.pmtiles is fetched by byte range — see _serve_file)
            return self._serve_tree(DATA_DIR, path[len("/data/"):])
        return self._redirect("302 unknown-path")

    do_GET = _route
    do_HEAD = _route
    do_POST = _route  # some captive stacks POST their probes

    # -- pages -----------------------------------------------------------

    def _serve_index(self):
        try:  # read fresh from disk every request — frontend agent iterates
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
        except OSError:
            body = PLACEHOLDER_PAGE.encode("utf-8")
        self._send_bytes(body)

    def _serve_page(self, full_path):
        """A whole static HTML page (e.g. /map) read fresh from disk."""
        try:
            with open(full_path, "rb") as f:
                body = f.read()
        except OSError:
            body = PLACEHOLDER_PAGE.encode("utf-8")
        self._send_bytes(body)

    def _serve_tree(self, base_dir, rel_path):
        """Serve a file under base_dir (path-traversal safe), with HTTP Range
        support — the pmtiles JS client reads data/london.pmtiles by byte
        range, so large files are never loaded into memory whole."""
        # decode %-escapes: glyph dirs contain spaces ("Noto Sans Regular")
        rel = os.path.normpath(unquote(rel_path))
        full = os.path.realpath(os.path.join(base_dir, rel))
        if not full.startswith(os.path.realpath(base_dir) + os.sep):
            return self._redirect("302 traversal")
        if not os.path.isfile(full):
            self._log("404 file")
            return self._send_bytes(b"not found", status=404,
                                    ctype="text/plain; charset=utf-8")
        self._serve_file(full)

    @staticmethod
    def _parse_range(header, size):
        """Parse a single-range 'bytes=' header -> (start, end) inclusive,
        None if absent/malformed (=> serve full 200), or 'unsatisfiable'."""
        if not header or not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):].strip()
        if "," in spec:  # multipart ranges not supported -> full response
            return None
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s == "":            # suffix form: bytes=-N (last N bytes)
                n = int(end_s)
                if n <= 0:
                    return "unsatisfiable"
                return (max(size - n, 0), size - 1)
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        except ValueError:
            return None
        if start >= size or start > end:
            return "unsatisfiable"
        return (start, min(end, size - 1))

    def _serve_file(self, full):
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        cache = ("public, max-age=86400" if ext in CACHEABLE_EXTS
                 else "no-store")
        try:
            size = os.path.getsize(full)
            rng = self._parse_range(self.headers.get("Range"), size)
            if rng == "unsatisfiable":
                self._log("416 range")
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with open(full, "rb") as f:
                if rng:
                    start, end = rng
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {start}-{end}/{size}")
                    f.seek(start)
                else:
                    start, length = 0, size
                    self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self._log(f"{206 if rng else 200} file[{start}+{length}]")
                if self.command == "HEAD":
                    return
                remaining = length  # stream in chunks — never whole-file RAM
                while remaining > 0:
                    chunk = f.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self._log("file client-gone")
        except OSError:
            self._log("404 file-read")
            self._send_bytes(b"not found", status=404,
                             ctype="text/plain; charset=utf-8")

    # -- APIs ------------------------------------------------------------

    def _api_status(self):
        meta = {}
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, ValueError):
            pass
        self._send_json({
            "model": llm.OLLAMA_MODEL,
            "cards": len(llm.load_cards()),
            "clients": client_count(),
            "queue": queue_depth(),
            "briefed_at": meta.get("briefed_at"),
            "location": meta.get("location"),
            "bytes_to_internet": BYTES_TO_INTERNET,  # always 0 — see module docstring
        })

    def _api_cards(self):
        self._send_json([
            {"id": c["id"], "title": c["title"], "category": c["category"],
             "summary": c["summary"]}
            for c in llm.load_cards()
        ])

    def _api_ask(self, query):
        params = parse_qs(query)
        q = (params.get("q") or [""])[0].strip()
        lang = ((params.get("lang") or ["en"])[0].strip() or "en").lower()
        self._log(f"SSE ask lang={lang} q={q[:80]!r}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return

        def sse(event, payload):  # flush per event — no buffering
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.flush()

        try:
            if not q:
                sse("error", {"message": "missing q parameter"})
                return

            ticket = object()
            with QUEUE_LOCK:
                ASK_QUEUE.append(ticket)
            acquired = False
            try:
                last_pos = None
                while True:
                    with QUEUE_LOCK:
                        pos = ASK_QUEUE.index(ticket)
                    if pos == 0 and GEN_LOCK.acquire(blocking=False):
                        acquired = True
                        if last_pos != 0:  # always land on position 0 before start
                            sse("queued", {"position": 0})
                        break
                    if pos != last_pos:  # re-emit live position on change
                        sse("queued", {"position": pos})
                        last_pos = pos
                    else:  # heartbeat: keeps proxies honest, detects gone clients
                        self.wfile.write(b": waiting\n\n")
                        self.wfile.flush()
                    time.sleep(1.0)

                for event, payload in llm.ask_stream(q, lang):
                    sse(event, payload)
            finally:
                with QUEUE_LOCK:
                    if ticket in ASK_QUEUE:
                        ASK_QUEUE.remove(ticket)
                if acquired:
                    GEN_LOCK.release()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._log("SSE client-gone")


# ---------------------------------------------------------------- main

def main():
    log(f"[beacon] ip={BEACON_IP} http_port={HTTP_PORT} dns={'on' if DNS_ENABLED else 'off'} "
        f"model={llm.OLLAMA_MODEL} ollama={llm.OLLAMA_URL}")
    log(f"[beacon] allowed hosts: {sorted(ALLOWED_HOSTS)} (+ anything containing 'localhost')")
    if DNS_ENABLED:
        threading.Thread(target=dns_server, daemon=True).start()
    try:
        routes.load_graph()  # 0.14s warm-up so the first map route has zero jitter
        log("[routes] walking graph preloaded")
    except Exception as exc:
        log(f"[routes] graph unavailable ({exc!r}) - /api/route will 404")

    def _prewarm():
        # Pull Gemma into Ollama's memory so the first phone's question
        # streams immediately instead of paying the model-load cost.
        try:
            for _ in llm.ask_stream("ok", "en", k=1):
                pass
            log("[llm] model pre-warmed")
        except Exception as exc:
            log(f"[llm] pre-warm failed ({exc!r}) - first request will load the model")
    threading.Thread(target=_prewarm, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Beacon)
    srv.daemon_threads = True
    log(f"[http] listening on :{HTTP_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("[beacon] shutting down")


if __name__ == "__main__":
    main()
