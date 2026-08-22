#!/usr/bin/env python3
"""Blackout Beacon — LLM layer.

Loads reference cards from cards/*.json, selects the relevant ones for a
question, and streams a grounded answer from a local Ollama (gemma4).

OFFLINE GUARANTEE: this module is the ONLY place in the project that opens an
outbound connection, and it refuses to talk to anything but loopback. The
"bytes to internet" counter shown in the UI is 0 by construction.

Thinking suppression (verified against Ollama 127.0.0.1:11434 with gemma4:e4b):
  - By default gemma4 streams thinking tokens in a separate `message.thinking`
    field (content stays empty while it thinks).
  - Passing top-level `"think": false` in the /api/chat body is accepted and
    fully suppresses thinking — content tokens start immediately.
  - Belt and braces: we only ever read `message.content` (never `.thinking`)
    and additionally strip any literal <think>...</think> blocks.

Stdlib only.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

_HERE = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.environ.get("BEACON_CARDS_DIR", os.path.join(_HERE, "cards"))

ALWAYS_INCLUDE_CARD_ID = "emergency-numbers"

LANG_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "zh": "Chinese (Simplified)",
    "pl": "Polish",
    "ro": "Romanian",
}

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _assert_loopback(url):
    """Refuse to configure a non-loopback Ollama. Keeps the offline story true."""
    host = urlsplit(url).hostname
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"OLLAMA_URL host {host!r} is not loopback; Beacon never talks to the internet"
        )


_assert_loopback(OLLAMA_URL)  # fail fast at import time


# ---------------------------------------------------------------- cards

def load_cards(cards_dir=None):
    """Read cards/*.json. Missing dir or broken files -> silently skipped."""
    cards_dir = cards_dir or CARDS_DIR
    cards = []
    try:
        names = sorted(os.listdir(cards_dir))
    except OSError:
        return cards
    for name in names:
        if not name.endswith(".json") or name == "meta.json":
            continue
        try:
            with open(os.path.join(cards_dir, name), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        card_id = str(data.get("id") or os.path.splitext(name)[0])
        keywords = data.get("keywords")
        if not isinstance(keywords, list):
            keywords = []
        cards.append({
            "id": card_id,
            "title": str(data.get("title") or card_id),
            "category": str(data.get("category") or ""),
            "summary": str(data.get("summary") or ""),
            "content": str(data.get("content") or ""),
            "keywords": [str(k) for k in keywords],
            "lang": str(data.get("lang") or "en"),
        })
    return cards


_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _tokens(text):
    return set(_WORD_RE.findall(text.lower()))


def select_cards(question, cards=None, k=4):
    """Keyword/substring scoring over title+keywords+summary+content.
    Always includes the 'emergency-numbers' card if it exists."""
    if cards is None:
        cards = load_cards()
    q_lower = question.lower()
    q_tok = _tokens(question)
    scored = []
    for card in cards:
        score = 0
        for kw in card["keywords"]:
            kw_l = kw.lower().strip()
            if kw_l and kw_l in q_lower:
                score += 5  # whole keyword phrase appears in the question
            score += 2 * len(_tokens(kw_l) & q_tok)
        score += 4 * len(_tokens(card["title"]) & q_tok)
        score += 2 * len(_tokens(card["summary"]) & q_tok)
        score += 1 * len(_tokens(card["content"]) & q_tok)
        scored.append((score, card))
    scored.sort(key=lambda pair: -pair[0])
    picked = [card for score, card in scored if score > 0][:k]
    if not any(c["id"] == ALWAYS_INCLUDE_CARD_ID for c in picked):
        for card in cards:
            if card["id"] == ALWAYS_INCLUDE_CARD_ID:
                if len(picked) >= k and picked:
                    picked.pop()  # drop weakest match to make room
                picked.append(card)
                break
    return picked


# ---------------------------------------------------------------- prompt

def _build_messages(question, lang, cards):
    lang_name = LANG_NAMES.get(lang)
    if lang_name:
        lang_rule = (f"ALWAYS answer in {lang_name} (language code '{lang}'), "
                     f"no matter what language the question or the cards are in.")
    else:
        lang_rule = (f"ALWAYS answer in the language with ISO code '{lang}', "
                     f"no matter what language the question or the cards are in.")

    if cards:
        blocks = []
        for c in cards:
            body = c["content"].strip() or c["summary"].strip()
            blocks.append(f"--- CARD: {c['title']} (id: {c['id']}"
                          f"{', category: ' + c['category'] if c['category'] else ''}) ---\n{body}")
        cards_text = "\n\n".join(blocks)
    else:
        cards_text = "(No reference cards are loaded yet.)"

    system = (
        "You are Beacon, an offline emergency assistant running locally on a laptop "
        "during a blackout. There is no internet connection. People on this WiFi may be "
        "stressed; stay calm, practical and brief.\n"
        "Rules:\n"
        "1. Answer ONLY from the REFERENCE CARDS below. Do not use outside knowledge for "
        "local facts.\n"
        "2. Be concise: give numbered steps first, then at most one or two short closing "
        "sentences. Calm, reassuring tone.\n"
        "3. If the cards do not cover the question, say so plainly, then give the universal "
        "safety default: if any phone nearby has signal, call 999 or 112 in an emergency.\n"
        f"4. {lang_rule}\n"
        "5. NEVER invent local facts — addresses, phone numbers, shelter locations, or names "
        "that are not in the cards.\n\n"
        f"REFERENCE CARDS:\n{cards_text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


# ---------------------------------------------------------------- think stripper

class _ThinkStripper:
    """Fallback removal of literal <think>...</think> blocks from a token stream.
    (Should never trigger: 'think': false is verified to suppress thinking, and we
    only read message.content — but stay safe across chunk-split tags.)"""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self.buf = ""

    def feed(self, text):
        self.buf += text
        out = []
        while True:
            if self.in_think:
                i = self.buf.find(self.CLOSE)
                if i == -1:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]  # keep possible partial tag
                    break
                self.buf = self.buf[i + len(self.CLOSE):]
                self.in_think = False
            else:
                i = self.buf.find(self.OPEN)
                if i == -1:
                    safe = len(self.buf)
                    for n in range(min(len(self.OPEN) - 1, len(self.buf)), 0, -1):
                        if self.buf.endswith(self.OPEN[:n]):
                            safe = len(self.buf) - n
                            break
                    out.append(self.buf[:safe])
                    self.buf = self.buf[safe:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.in_think = True
        return "".join(out)

    def flush(self):
        tail = "" if self.in_think else self.buf
        self.buf = ""
        return tail


# ---------------------------------------------------------------- streaming ask

def ask_stream(question, lang="en", k=4):
    """Generator yielding (event, payload) tuples the server maps 1:1 to SSE:
        ("start", {"cards": [{"id","title"}, ...]})
        ("token", {"text": "..."})
        ("done",  {"total_ms": N})
        ("error", {"message": "..."})
    """
    t0 = time.time()
    try:
        _assert_loopback(OLLAMA_URL)
        cards = load_cards()
        picked = select_cards(question, cards, k=k)
        yield ("start", {"cards": [{"id": c["id"], "title": c["title"]} for c in picked]})

        body = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": _build_messages(question, lang, picked),
            "stream": True,
            "think": False,  # gemma4 is a thinking model; verified this suppresses it
            "options": {"temperature": 0.3, "num_predict": 700},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        stripper = _ThinkStripper()
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue
                if chunk.get("error"):
                    yield ("error", {"message": str(chunk["error"])})
                    return
                msg = chunk.get("message") or {}
                # Deliberately ignore msg.get("thinking") — never forwarded.
                text = stripper.feed(msg.get("content") or "")
                if text:
                    yield ("token", {"text": text})
                if chunk.get("done"):
                    tail = stripper.flush()
                    if tail:
                        yield ("token", {"text": tail})
                    break
        yield ("done", {"total_ms": int((time.time() - t0) * 1000)})
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        yield ("error", {"message": f"Ollama HTTP {e.code}: {detail or e.reason}"})
    except urllib.error.URLError as e:
        yield ("error", {"message": f"Cannot reach local model at {OLLAMA_URL}: {e.reason}. "
                                    f"Is Ollama running?"})
    except Exception as e:  # never let a generation error kill the server thread
        yield ("error", {"message": f"{type(e).__name__}: {e}"})
