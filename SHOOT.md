# SHOOT.md — solo video shoot, click-by-click

(Claude is offline during the blackout takes — this file is the self-serve script.
VO lines are recorded LATER over the edit, not during takes.)

## Setup
- Phone: charged, DND on, mobile data OFF, forget BEACON, screen recorder: sound none + show taps
- Mac: plugged in, DND, only Terminal + browser + System Settings open, Terminal font ~20pt
- `open docs/join-card.html` -> fullscreen in its own Space

## Take 1 — briefing replay (ONLINE, Mac)
cd "/Users/joeljeon/gemini hack" && clear
Cmd-Shift-5 record ->  script -p evidence/briefing-run.rec  -> ~60-90s -> stop, Ctrl+C
VO: "So before the storm, Gemini 3.7 Flash briefs this laptop — compiling local
knowledge for every borough and district of London into structured cards:
hospitals, emergency numbers, first aid, eleven languages."

## Go offline
sudo ./run.sh   (confirm: dns / http / routes preloaded / model pre-warmed)
System Settings > General > Sharing > Internet Sharing ON

## Take 2 — dead internet + live log (Mac; keep rolling through Take 3)
Cmd-Shift-5 record -> google.com error page 4s -> switch to server-log Terminal -> leave rolling
VO: "Then the blackout hits. No internet. And this MacBook becomes the network."

## Take 3 — phone journey (ONE take; rehearse once first)
Start phone recorder ->
  WiFi > BEACON > beacon123 > Keep connection
  Camera > scan QR #2 on join card > open link
  Medical emergency > "Someone collapsed" > let it stream (~20s)
  Bengali pill > Medical > first sub-chip > streams in Bengali
  Map > type SW7 2AZ > Find A&E > "exact location" + routes > tap first hospital > hold 3s
Stop phone recorder. Stop the Take-2 Mac recording.
VO: "Any phone joins — no app, no account. Ask anything: Gemma 4 answers entirely
on-device, grounded in the briefing." / "It speaks your language." / "Type your
postcode — it knows exactly where you are, and walks you to the nearest A&E,
street by street, computed offline."

## Take 4 — queue (optional)
Mac browser http://localhost + phone: both type questions, send Mac first, phone 2s later
-> phone shows "position 1". Record on phone.

## Take 5 — physical shot
QuickTime > New Movie Recording > hold phone beside laptop 8s.
VO: "One laptop. A whole street. Bytes sent to the internet: zero."

## Teardown
Internet Sharing OFF -> rejoin WiFi -> mobile data ON -> AirDrop clips to Mac.

## Assembly (iMovie)
Order: title card / T1 / T2 / T3 / T4 / T5 / closing card
Trims: 8 / 17 / 15 / 65 / 12 / 8 / 8 s  (~2:00)
Title: "When the grid dies, the cloud dies with it."
Close: "BLACKOUT BEACON — works when nothing else does." + repo URL
One continuous VO pass (mic button), final line: "Blackout Beacon — it works when
nothing else does."
Export 1080p -> upload YouTube unlisted or Loom -> link into submission.

If a take flakes: finish it, re-shoot just that take, keep every clip.
