#!/usr/bin/env bash
# Cloud gateway entrypoint; forwards argv to setup.py, never evaluated.
# Process: detect distro -> system deps (curl/CA/OpenSSL) -> private Python 3.12
#          -> Docker Engine + Compose -> setup.py cloud (six stages).
# Required input: --ip (public IPv4). Optional: --machine, --state, --cn, --skip-deps.
set -euo pipefail
RC_PACKAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$RC_PACKAGE/scripts/bootstrap-posix.sh"
rc_bootstrap cloud "$@"
export PYTHONUNBUFFERED=1
exec "$RC_PYTHON" -u "$RC_PACKAGE/scripts/setup.py" cloud "$@"
