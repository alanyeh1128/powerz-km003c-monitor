#!/bin/bash
# Double-click to launch the POWER-Z KM003C monitor (opens in the default browser).
cd "$(dirname "$0")"
exec .venv/bin/python powerz_monitor.py "$@"
