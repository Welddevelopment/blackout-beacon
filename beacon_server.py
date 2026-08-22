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
import os
import socket
import struct
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import llm

# ---------------------------------------------------------------- config

BEACON_IP = os.environ.get("BEACON_IP", "192.168.2.1")
HTTP_PORT = int(os.environ.get("BEACON_HTTP_PORT", "80"))
DNS_ENABLED = os.environ.get("BEACON_DNS", "1") != "0"
DNS_PORT = int(os.environ.get("BEACON_DNS_PORT", "53"))  # override for non-root dev tests

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
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
}

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
            i += 5  # null byte + qtype(2) + qclass(2)
            question = data[12:i]
            answer = (b"\xc0\x0c" + b"\x00\x01" + b"\x00\x01" +
                      struct.pack(">I", 10) + b"\x00\x04" + socket.inet_aton(BEACON_IP))
            resp = (data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x01" +
                    b"\x00\x00\x00\x00" + question + answer)
            s.sendto(resp, addr)
            log(f"[dns] {addr[0]} {qname} -> {BEACON_IP}")
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
        if path == "/api/status":
            self._log("200 status")
            return self._api_status()
        if path == "/api/cards":
            self._log("200 cards")
            return self._api_cards()
        if path == "/api/ask":
            return self._api_ask(query)
        if path.startswith("/static/"):
            return self._serve_static(path)
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

    def _serve_static(self, path):
        rel = os.path.normpath(path[len("/static/"):])
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            return self._redirect("302 traversal")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._log("404 static")
            return self._send_bytes(b"not found", status=404,
                                    ctype="text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        self._log("200 static")
        self._send_bytes(body, ctype=MIME.get(ext, "application/octet-stream"))

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
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Beacon)
    srv.daemon_threads = True
    log(f"[http] listening on :{HTTP_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("[beacon] shutting down")


if __name__ == "__main__":
    main()
