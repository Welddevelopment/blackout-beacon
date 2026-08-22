# QA Report — Blackout Beacon

QA run 2026-08-22 (afternoon, pre-demo), against a dedicated dev instance
(`BEACON_HTTP_PORT=8092 BEACON_DNS=0`), Ollama `gemma4:e4b` warm.
~130 checks across 8 areas. Backend fix committed; verdict at the bottom.

## Test matrix results

| Area | Checks | Result | Notes |
|---|---|---|---|
| 1. API contract (`/api/status`, `/api/cards`, `/api/ask` SSE) | 12 | PASS | Status shape + `bytes_to_internet: 0`; cards list = 173, no `content` leak; SSE named events exact (`queued`→`start`→`token`…→`done`); missing `q` → `error` event; HEAD on `/api/ask` returns headers only |
| 1b. Ask edge cases | 6 | PASS | 2000-char question, `<script>` injection (clean JSON tokens, no crash), emoji-only, empty `lang` (defaults en), unknown `lang=xx` — all complete with proper `done` |
| 2. Multilingual SSE (all 11 langs, question written in that language) | 11 | PASS | Answer in correct language/script (ar/zh/bn/ur script-verified); grounding cards sensible in every case (see below); zero thinking-token leaks in 20+ generations |
| 3. Area routing (`llm.select_cards`) | 20 | PASS (1 known limitation) | Boroughs, districts, postcodes (E8→dalston, N16→hackney, SE15→southwark, SW7→kensington-chelsea per card keywords); exactly one area card per answer; `emergency-numbers` always included. Misspelling "Hackny" NOT matched — see Open bugs |
| 4. Queue fairness (3 overlapping SSE) | 5 | PASS | client2 positions [1,0], client3 [2,1,0], strictly decreasing, all complete; heartbeat comments seen while waiting |
| 5. Portal behavior | 16 | PASS | 4 probe paths → 302 portal (GET and POST); foreign Host → 302; beacon.help / with-port / 127.0.0.1 / localhost → 200; 5 traversal payloads (incl. `%2e%2e`, `..%2f`) blocked, nothing leaked; unknown path → 302; no-Host HTTP/1.0 handled |
| 6. Map stack | 30 | PASS | `/map` 200; pmtiles Range: `bytes=0-16383` → 206 with exactly 16384 B + correct `Content-Range`; open-ended, suffix, past-EOF→416, inverted→416, multipart→200-full, HEAD+Range CL-only; 5 POI files parse, `count`==len(items), fields present; glyphs with spaces in path (`Noto%20Sans%20Regular`) and sprites serve; `routes.py`: 5 routes incl. cross-London 36 km in 8 ms, out-of-coverage (Birmingham/Reading/North Sea) → None; `/api/route` end-to-end: valid 200 (matches contract shape), bad params → 400, out-of-coverage → 404 |
| 7. Resilience | 8 | PASS | 50 rapid status polls all 200; mid-stream SSE disconnect → next ask completes, queue drains to 0; fd count stable (35→35) across burst; RSS 38 MB after cross-London routing (budget 500 MB); no tracebacks in log after fix |
| 8. Data integrity | 7 | PASS | All 173 cards parse; required fields present; no duplicate ids; id==filename; meta.json `card_count` 173 and `area_cards` 154 both match filesystem |
| DNS (extra, high-port instance) | 8 | PASS | Wildcard answers every name → 192.168.2.1, txn-id echoed; short/garbage packet ignored without crash, responder keeps serving |

Multilingual grounding spot checks (start-event card ids): Arabic "ستراتفورد" →
`local-help-points-stratford`; Chinese Stratford → `local-help-points-stratford`
(answer cited The Broadway / Stratford High Street from the card); Bengali
Whitechapel → `local-help-points-whitechapel`; Urdu Camden →
`local-help-points-camden-town`; Turkish Wood Green, Portuguese Peckham,
Romanian Croydon, Polish Ealing, Spanish Brixton, French Croydon all correct.
The translation-for-retrieval layer is working as designed.

## Bugs found

### Fixed (committed)

1. **`/api/route` with `nan`/`inf` coordinates killed the request thread** —
   `float("nan")` parses, then `int(round(nan*1e6))` in `routes._nearest`
   raises ValueError/OverflowError; client got an empty reply (curl exit 52)
   and a traceback hit the log. Severity: medium (trivially triggerable,
   server survives but request dies silently). Fixed with an explicit
   finiteness check → 400 `bad params`. Commit `374335b`.

2. **`/api/route` not wired** (map.html already calls it) — found during this
   run; the routing agent landed the integration concurrently (commit
   `7dc22bd`, includes boot preload + graceful degradation). I verified it
   end-to-end afterwards: valid / 400 / 404 / nan-400 all correct.

### Open (not fixed, with repro)

3. **Misspelled place names fall back silently to the default area card.**
   Repro: `llm.select_cards("I'm in Hackny, where do I go?")` →
   `local-help-points-kensington-chelsea` (BEACON_AREA default), not hackney.
   Severity: low — graceful (national numbers still correct, answer just
   cites the wrong borough's help points). Deliberately NOT fixed: edit-
   distance fuzzy matching creates worse false positives ("dealing"→Ealing,
   "parking"→Barking, "action"→Acton) which would misroute well-spelled
   questions. Mitigation: demo with correctly spelled place names.

4. **POST bodies are never drained.** `do_POST = _route` never reads the
   request body; on an HTTP/1.1 keep-alive connection the unread body bytes
   are parsed as the next request line (worst case: one spurious 400, then
   the connection closes; server unaffected). Real captive probes POST with
   empty bodies, and the UI uses GET, so exposure is minimal. Severity: low.
   Repro: pipeline two requests on one socket where the first is a POST with
   a body.

5. **DNS answers AAAA (and any qtype) with an A record.** Protocol-sloppy but
   field-tested on real phones per README; phones fall back fine. Severity:
   info. No change recommended before the demo.

6. **Cosmetic**: occasional grammar slips in low-resource languages (e.g. pt
   "O insulina não aberto") — model quality, not a pipeline bug; language and
   facts were correct in all 11 tests.

## Demo-readiness verdict

**GO.** Backend, DNS, portal logic, queue, map stack, routing engine and all
11 languages pass. Server survived every malformed-input, disconnect and
concurrency test without a crash; fds and memory are flat.

Top 3 risks for a live demo:

1. **Live edits to `static/index.html`.** The design agent was still
   restyling it during this run, and the server serves it fresh from disk on
   every request — a bad save is instantly user-visible. Freeze the frontend
   and do one full phone walk-through (ask + map + language switch) before
   going on stage.
2. **Inference latency under queue.** Warm e4b answered in 4–12 s here; a
   cold model, or the 12b variant, or 3+ queued phones stretches waits to
   30 s+. Pre-warm Ollama (one throwaway ask) minutes before the demo and
   keep audience-driven asks to one or two at a time.
3. **Wrong-borough answers on odd place input.** Misspelled or uncovered
   place names silently get the default Kensington & Chelsea help points
   (bug 3). Script demo questions with clean place names, or set
   `BEACON_AREA` to the venue's actual area so even the fallback is right.

Not testable in this run: real hotspot behavior (Internet Sharing / feth
workaround, port 53/80 as root, phone captive-portal popups) — HOTSPOT.md
covers it; rehearse once on real hardware.
