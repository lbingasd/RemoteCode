#!/usr/bin/env bash
# macOS/Linux home-machine entrypoint; run as the developer, never with sudo.
set -euo pipefail
RC_PACKAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$RC_PACKAGE/scripts/bootstrap-posix.sh"
rc_bootstrap local "$@"
exec "$RC_PYTHON" "$RC_PACKAGE/scripts/setup.py" local "$@"
