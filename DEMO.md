# DEMO.md — Blackout Beacon

Two deliverables in this file: the 2-minute Loom script, and the live-demo runbook for judging.

---

## Part 1 — The 2-minute Loom

Film in landscape. The middle section (0:30-1:30) is the whole video; everything else is framing. Real phones, real hands, one continuous take for the physical act if at all possible.

### [0:00-0:15] Hook

Visual: quick montage of blackout headlines (Storm Éowyn outages, Iberian grid collapse, "thousands without power" stills). Cut to black.

Voiceover:
> "When the grid dies, the cloud dies with it. Every AI assistant on earth is on the wrong side of a broken cable. This is what we built for the moment after that."

Title card: **BLACKOUT BEACON** — *The cloud teaches the laptop before the storm; the laptop carries the neighbourhood through it.*

### [0:15-0:30] Blue-sky phase

Visual: screen recording of `python3 briefing.py` running. Show Gemini 3.7 Flash being called and JSON readiness cards landing in `cards/` (a terminal split with the cards directory filling up works well). Briefly flash one open card: UK emergency numbers or the hospital card.

Voiceover:
> "While the internet still exists, Gemini 3.7 Flash compiles everything a neighbourhood will need: emergency numbers, first aid, water safety, local hospitals, phrasebooks. Structured JSON. Frozen onto the laptop."

### [0:30-1:30] The physical act (the money shot, filmed on camera, not screen-recorded)

- **[0:30]** Hand reaches to the router. **Unplug it on camera.** Hold on the dead router lights for a beat.
- **[0:35]** Laptop screen: Internet Sharing on, `BEACON` broadcasting. Phone 1 (held in shot): WiFi settings, `BEACON` appears, join it. Show the "no internet" warning and tap **keep connection** — say out loud that this warning is correct, this network has no internet, that is the point.
- **[0:45]** Phone 1 browser: type `beacon.help`. The portal loads. Tap in the question: *"My insulin has been in a warm fridge for 6 hours. Is it still safe?"* Show the answer **streaming token by token**, with its citation of the medicine-storage readiness card visible on screen.
- **[1:05]** Phone 2 enters frame, already on BEACON: switch the UI to **Spanish**, ask a question, show the streamed answer. Quick cut to **Arabic**, show the interface flip to right-to-left.
- **[1:20]** Wide shot: dead router, one laptop, two or three phones, all mid-conversation.

Voiceover (sparse, let the screens talk):
> "No app. No account. No internet. Gemma 4 running locally answers every question, grounded in the cards Gemini wrote before the storm. One question at a time, fairly queued, for every phone in range."

### [1:30-1:50] Architecture and the guarantee

Visual: the architecture card (both phases, the cards as the handoff). Then cut to the phone UI's footer counter: **"Bytes sent to the internet: 0"** — zoom on it.

Voiceover:
> "Two models, one handoff. Gemini 3.7 Flash distils the world's knowledge into readiness cards while it can. Gemma 4 over Ollama serves it when nothing else can. The server is Python standard library only; the counter in the corner is the product guarantee."

### [1:50-2:00] Close

Visual: the wide shot again, slow push in on the laptop.

Voiceover:
> "One laptop. A whole street. Works when nothing else does."

End card: **Blackout Beacon** · beacon.help · MIT.

---

## Part 2 — Live-demo runbook (judging)

Goal: from a cold start, a judge's phone is chatting with the beacon in under 90 seconds. Practise the sequence twice before the first judge.

### Pre-flight (once, before judging starts)

1. Laptop charged, lid-close sleep disabled (or keep it plugged into a battery pack for effect).
2. `ollama pull gemma4:e4b` already done; run one throwaway query so the model is warm in memory.
3. Cards compiled: `cards/` has 12-15 JSON files from this morning's `briefing.py` run.
4. Server up: `sudo ./run.sh` (binds DNS on UDP 53 and HTTP on port 80; both need root).
5. Print or display the **QR code for http://beacon.help** next to the laptop. This bypasses every captive-portal quirk (see Failure fallbacks).
6. Mobile data OFF on any demo phones you own; venue WiFi auto-join OFF on them (they will otherwise flee back to eduroam mid-demo).

### Hotspot bring-up (exact steps)

macOS 26 will refuse to share from an interface with no link. Run the fake-cable one-liner first:

```bash
sudo ifconfig feth0 create && sudo ifconfig feth1 create && \
sudo ifconfig feth0 peer feth1 && sudo ifconfig feth0 up && sudo ifconfig feth1 up
```

Then:

1. System Settings → **General → Sharing** → **Internet Sharing** → click **ⓘ** (do not toggle yet).
2. **Share your connection from:** the dead interface (`Thunderbolt Bridge`, or the `feth` interface if Thunderbolt Bridge is refused).
3. **To devices using:** tick **Wi-Fi**.
4. **Wi-Fi Options…** → Name: `BEACON` · Security: `WPA2/WPA3 Personal` · Password: `beacon123` → OK → Done.
5. Toggle **Internet Sharing ON** → **Start**. Menu-bar WiFi icon switches to the hotspot glyph. The Mac is now offline; that is correct.

If the toggle flips itself off: switch the source to `Ethernet Adapter (en4)`, then en5, en6, retoggling each time. One of them takes.

### Per-judge script

1. "Join the WiFi network **BEACON**, password **beacon123**."
2. When their phone warns "no internet": "Tap keep connection — the warning is the point, there is no internet, and this still works."
3. If a captive-portal popup appears: great, they are already on the portal. If not: "Scan this QR", or "open your browser and type **http://beacon.help**" (with the http://; some browsers force https and then complain).
4. Suggest the insulin question or let them free-type. Point out: streaming tokens, the cited card, the language switcher (show Arabic RTL if they seem interested), and the **"Bytes sent to the internet: 0"** counter.
5. Second judge's phone can join mid-conversation; the queue serves them in turn. Say so: "one laptop, fair queue, whole street."

### Reset between judges

- Refresh state: tap New Chat in the UI (or reload the page) so the next judge starts clean.
- Leave the hotspot and server running; do not toggle Internet Sharing between judges, bring-up is the slowest step.
- Glance at the server log for errors; if Ollama went cold, fire one warm-up query.
- If your own demo phones drifted back to venue WiFi, rejoin BEACON before the next judge arrives.

### Failure fallbacks (in order)

| Symptom | Do this |
|---|---|
| `beacon.help` will not load | Type **http://192.168.2.1** directly. This always works and is a perfectly good demo; say "any name works via our DNS wildcard, and the raw IP is the bedrock". |
| Browser forces HTTPS and errors | Retype with the explicit `http://` prefix, or use the QR code. |
| No captive popup (Samsung especially) | Expected on some builds (HTTPS-first connectivity probe). QR code or typed URL; do not fight the popup. |
| Name resolution hangs on Android | Private DNS trying port 853 first; wait ~5s for fallback to plain DNS, or go straight to the IP. |
| BEACON never appears on the phone | Toggle Internet Sharing OFF then ON; confirm Wi-Fi Options kept the network name. |
| Internet Sharing will not start | Re-run the `feth` one-liner (interfaces vanish on reboot), then retoggle. Then try en4/en5/en6 as the source. |
| Phone silently leaves BEACON | Its auto-join to venue WiFi is on; disable it and rejoin. |
| Total loss | `sudo ./run.sh` again, rejoin BEACON, http://192.168.2.1. Cold recovery is under two minutes; practising it once is worth it. |

## Evidence kit (for the video edit)

- `script -p evidence/briefing-run.rec` — replays the full Gemini briefing (cards forming live, real timing). Screen-record the replay; it is real output and needs no network.
- `evidence/briefing-london-wave{1,2,3}.log` — the 153-card London expansion, one line per card.
- `QA-REPORT.md` — ~130-check test matrix; show it briefly for the "we tested this" beat.
- `git log --oneline` — timestamped build history on the public repo.
- Server log during the live demo (stdout of ./run.sh) — every phone join and question scrolling live is strong B-roll.
