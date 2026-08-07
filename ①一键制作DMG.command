#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$ROOT/scripts/build_macos.command"
exec "$ROOT/scripts/build_macos.command"
