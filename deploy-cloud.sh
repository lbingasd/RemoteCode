#!/usr/bin/env bash
# Linux gateway entrypoint. Arguments are forwarded as argv, never evaluated.
set -euo pipefail
RC_PACKAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$RC_PACKAGE/scripts/bootstrap-posix.sh"
rc_bootstrap cloud "$@"
export PYTHONUNBUFFERED=1
exec "$RC_PYTHON" -u "$RC_PACKAGE/scripts/setup.py" cloud "$@"
