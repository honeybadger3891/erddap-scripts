#!/usr/bin/env bash
# Log in using your normal SSH client, then run this on the ERDDAP host.
# All entry points use the same scan/review/confirmed-apply implementation.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/erddap_admin_rename.py" "$@"
