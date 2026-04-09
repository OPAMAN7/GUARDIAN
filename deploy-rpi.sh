#!/bin/bash
# Wrapper for compatibility with top-level deploy command.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/pi-agent/scripts/deploy-rpi.sh"
