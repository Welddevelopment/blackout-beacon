# HOTSPOT.md — Broadcasting the Beacon network

How to make the Mac broadcast the `BEACON` WiFi network with **no internet behind it**.
These steps are battle-tested on macOS 26 (Apple Silicon). Total time: ~2 minutes.

## 1. The fake cable (macOS 26 requirement)

macOS 26 refuses to start Internet Sharing from a source interface with no link.
We give the Thunderbolt Bridge a virtual "cable" using macOS's built-in fake
ethernet devices (`feth`). No internet is involved — the link is a loop to nowhere,
which is exactly the point (the blackout has no upstream).

```bash
sudo sh -c 'ifconfig feth0 create; ifconfig feth1 create; ifconfig feth0 peer feth1; ifconfig feth0 up; ifconfig feth1 up; ifconfig bridge0 addm feth0; ifconfig bridge0 up'
```

Verify: `ifconfig bridge0 | grep status` → must say `status: active`.
(The feth devices vanish on reboot — re-run after every restart.)

## 2. Configure Internet Sharing (one-time)

System Settings → **General → Sharing** → **Internet Sharing** → click **ⓘ**:

- **Share your connection from:** `Thunderbolt Bridge`
- **To devices using:** tick `Wi-Fi` (this checkbox silently unticks itself when the
  source changes — always re-check it)
- **Wi-Fi Options…** → Network Name `BEACON` · Security `WPA2/WPA3 Personal` ·
  Password `beacon123` → OK → Done

## 3. Start broadcasting

Flip **Internet Sharing ON**. Watch for the admin prompt and/or **Start**
confirmation — if no dialog appears and the Mac stays online, sharing did NOT
really start (see Troubleshooting).

Success signs:
- Menu-bar WiFi icon becomes the hotspot glyph
- The Mac loses internet (that's correct — it IS the network now)
- `ifconfig bridge100 | grep inet` → shows `192.168.2.1`

Then start the Beacon server if it isn't running: `sudo ./run.sh`

## 4. Phones join

- Join `BEACON` / `beacon123`. On Android, when asked "this network has no
  internet — stay connected?" tap **Yes / Keep connection**.
- iPhones: the Beacon page usually pops up on its own (captive portal).
- Androids (some builds probe HTTPS-first and never pop): open any browser →
  **`http://beacon.help`** (any http:// address works — local DNS answers
  everything). Bedrock fallback: `http://192.168.2.1`.
- Best practice for demos: print/display a WiFi QR code so phones join with one scan.

## 5. Teardown

Internet Sharing **OFF** → WiFi menu → rejoin your normal network.
Optional cleanup: `sudo ifconfig feth0 destroy; sudo ifconfig feth1 destroy`

## Troubleshooting (all hit for real during development)

| Symptom | Cause | Fix |
|---|---|---|
| Toggle "On" but Mac still online, no dialogs | Wi-Fi checkbox unticked itself, or source invalid | Toggle OFF, redo step 2, toggle ON |
| Toggle refuses / flips back off | Source has no link | Re-run the feth one-liner (step 1); or pick `Ethernet Adapter (en4/en5/en6)` as source |
| Phone joins, page times out | Android left the network ("no internet" verdict) | Rejoin; tap **Keep connection**; retry |
| Phone joins, `beacon.help` fails | Browser forced https, or Private DNS pinned | Type `http://` explicitly; bedrock: `http://192.168.2.1`; optionally set Private DNS → Off |
| No captive popup on Android | Vendor probes HTTPS-first (seen on Samsung) | Expected — use `beacon.help`; iPhones pop reliably |
| Demoing over venue WiFi instead | University networks block device-to-device | Don't — client isolation kills it; the hotspot IS the demo path |
