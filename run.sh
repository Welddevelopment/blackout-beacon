#!/bin/bash
# Blackout Beacon launcher — ports 53 (DNS) and 80 (HTTP) need root.
# Dev mode without sudo: BEACON_HTTP_PORT=8080 BEACON_DNS=0 python3 beacon_server.py
cd "$(dirname "$0")" && sudo python3 beacon_server.py
