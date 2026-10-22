#!/usr/bin/env bash
# Add-on deployment/configuration for an existing home runtime; no cloud changes.
# Process: bootstrap Python -> qq_bot.py install/configure (interactive AppID/
#          AppSecret/sandbox -> verify auth -> write config -> start -> pairing code).
# Required input: QQ AppID/AppSecret (interactive, hidden). Commands: install|configure|status|...
set -euo pipefail
RC_PACKAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$RC_PACKAGE/scripts/bootstrap-posix.sh"
rc_python || rc_die 'Python 3.12+ required; set REMOTECODE_PYTHON to its path.'
if [ "$#" -eq 0 ]; then set -- install; fi
exec "$RC_PYTHON" -u "$RC_PACKAGE/scripts/qq_bot.py" "$@"
