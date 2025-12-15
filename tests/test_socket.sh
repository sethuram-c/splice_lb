#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
output="$(./build/splice-lb)"
printf '%s\n' "$output"
grep -Eq '^TCP socket created successfully \(fd=[0-9]+\)$' <<< "$output"
grep -Fxq 'TCP socket closed successfully' <<< "$output"
printf 'PASS: TCP socket lifecycle smoke test\n'
