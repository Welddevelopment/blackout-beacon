# Phase 2 — Hotspot Test Runbook (Claude is OFFLINE during this)

Server is already running and logging. Follow in order. Restore steps at the bottom.

## A. Phone prep (do FIRST)
1. Mobile data **OFF** (forces the browser onto WiFi; mirrors the real blackout).
   - iPhone: Settings → Mobile Data. Android: quick-settings tile or Settings → Network & internet → SIMs.
2. Stop the phone fleeing back to venue WiFi:
   - iPhone: Settings → Wi-Fi → ⓘ on venue network → **Auto-Join OFF**.
   - Android: Settings → Wi-Fi → venue network → **Auto-connect / Auto reconnect OFF**. Also disable "Switch to mobile data automatically" if present (Wi-Fi → Network preferences).
   (Remember to restore all of this at the end.)

## B. Mac — create the hotspot
3. System Settings → **General → Sharing**.
4. Find **Internet Sharing** → click the **ⓘ** button (do NOT toggle yet).
5. **Share your connection from:** `Thunderbolt Bridge`  (a dead interface — intentional: the blackout has no upstream).
6. **To devices using:** tick `Wi-Fi`.
7. **Wi-Fi Options…** → Network Name: `BEACON` · Channel: leave default · Security: `WPA2/WPA3 Personal` · Password: `beacon123` → OK → Done.
8. Flip **Internet Sharing ON** → enter Mac password if asked → click **Start**.
9. Menu-bar WiFi icon should change to the hotspot glyph (arrow). **Mac is now offline. Claude is paused. Keep going solo.**

## C. Phone — the actual test
10. Settings → Wi-Fi → wait for **BEACON** (can take ~30s) → join with `beacon123`.
11. "No internet" warnings are CORRECT — do NOT let the phone leave:
    - iPhone: dismiss any "Use Other Network" popup; choose "Use Without Internet" if offered.
    - Android: when it asks "BEACON has no internet access — stay connected?" tap **YES / Keep Wi-Fi connection** (tick "don't ask again" if offered). THIS TAP IS THE CRITICAL MOMENT.
12. Open the browser and type EXACTLY (with the http://):
    - **Android: go straight to** `http://192.168.2.1:8000`  (.local names don't resolve on Android)
    - iPhone: `http://joels-macbook-pro.local:8000`, and if that fails ~15s, `http://192.168.2.1:8000`
    - If the browser auto-upgrades to https and errors, retype with the http:// prefix.
14. SUCCESS = green "BEACON TEST OK" page.

## D. Stress it (only if C succeeded)
15. Reload the page 3–4 times over ~2 minutes.
16. Lock the phone, wait 30s, unlock, reload again (tests WiFi sleep-drop).
17. If a second phone is available: join BEACON on it too, load the page.
18. Note: which URL(s) worked, any popups, whether the phone ever silently left BEACON.

## E. If it fails
- Toggle won't start / flips itself off → change "share from" to `Ethernet Adapter (en4)`, retoggle. Then try en5, en6.
- BEACON never appears on the phone → toggle Internet Sharing OFF then ON once more; recheck Wi-Fi Options saved the name.
- Page loads on 192.168.2.1 but not .local → that's a PASS, just note it.
- Nothing works at all → revert (below) and report everything you saw.

## F. Revert (brings Claude back)
19. Internet Sharing toggle **OFF**.
20. WiFi menu → rejoin the venue network → open any website to confirm internet is back.
21. iPhone: Mobile Data **ON**; venue network Auto-Join **ON**.
22. Tell Claude what happened (which URLs, how many reloads, popups, second phone). The server logged every request, so results can be verified forensically.
