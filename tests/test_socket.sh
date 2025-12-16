#!/usr/bin/env bash
# Phase 1 compatibility entrypoint. The socket lifecycle now includes accepting
# one client, so the old no-client smoke test must not be used.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tests/test_listener.py
