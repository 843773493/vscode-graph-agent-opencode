#!/usr/bin/env bash
set -euo pipefail

TOOL_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
exec "$TOOL_ROOT/remote-windows-vm.sh" "$@"
