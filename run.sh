#!/usr/bin/env bash
# Web51TNC Launcher Script
set -e
cd "$(dirname "$0")"

exec python3 server.py "$@"

