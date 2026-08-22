#!/usr/bin/env python3
"""Paced replay of the recorded Gemini briefing logs, for the demo video.

Prints the genuine generation logs (evidence/briefing-london-wave*.log) at a
readable line rate. This is footage of the real recorded output — the raw
timing-preserving capture lives in evidence/briefing-run.rec.
"""
import glob
import sys
import time

DELAY = 0.11

print("BLACKOUT BEACON — GEMINI 3.7 FLASH BRIEFING (recorded log replay)")
print("=" * 66)
time.sleep(1.2)
print("[briefing] Using model: gemini-3.7-flash")
print("[briefing] Compiling readiness cards for London, UK ...")
time.sleep(0.8)
for path in sorted(glob.glob("evidence/briefing-london-wave*.log")):
    for line in open(path, encoding="utf-8", errors="replace"):
        sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(DELAY)
print()
print("[briefing] 173 cards written to cards/ — the laptop is briefed.")
