#!/usr/bin/env bash
# macOS/Linux home-machine entrypoint; run as the developer, never with sudo.
# Process: system deps -> Node/Git -> setup.py local (IDE/frpc -> runtime ->
#          ACP adapters + prerequisite Codex/Claude detection -> settings/extension
#          -> service -> doctor).
# Required input: --bundle <dir>, --workspace <dir>, --accept-license. Optional: --cn.
set -euo pipefail
RC_PACKAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$RC_PACKAGE/scripts/bootstrap-posix.sh"
rc_bootstrap local "$@"
export PYTHONUNBUFFERED=1
exec "$RC_PYTHON" -u "$RC_PACKAGE/scripts/setup.py" local "$@"
