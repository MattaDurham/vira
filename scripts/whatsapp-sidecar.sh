#!/bin/zsh
# Run the WhatsApp sidecar by hand against a given Vira checkout's data/.
# Normal operation uses server/whatsapp.py to share and supervise it.
# This command is for deliberate manual startup and debugging.
#
#   scripts/whatsapp-sidecar.sh            # this checkout's data/, default port
#   scripts/whatsapp-sidecar.sh 18391      # explicit port
set -eu

HERE=${0:A:h:h}                       # repo root (script lives in scripts/)
PRIMARY=$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)
HERE=${PRIMARY:h}                     # one linked device for all branches
OWNER_ID=primary
BRIDGE="$HERE/bridge/whatsapp"
DATA="$HERE/data/whatsapp"
PORT=${1:-$(python3 - "$HERE/data/config.json" <<'PYTHON'
import json
import sys
from pathlib import Path

try:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(cfg.get("whatsapp_bridge_port") or 18377)
except (OSError, ValueError):
    print(18377)
PYTHON
)}

[[ -d "$BRIDGE/node_modules" ]] || {
  echo "installing sidecar dependencies (one time)…"
  (cd "$BRIDGE" && npm install --no-fund --no-audit)
}
mkdir -p "$DATA"
echo "sidecar on 127.0.0.1:$PORT  (session + inbox under $DATA)"
echo "stop: curl -s -X POST -H 'X-Vira-Instance: $OWNER_ID' http://localhost:$PORT/stop"
exec node "$BRIDGE/sidecar.js" --port "$PORT" --owner-id "$OWNER_ID" \
  --session-dir "$DATA/session" \
  --inbox "$DATA/inbox.ndjson" \
  --pidfile "$DATA/sidecar.pid" \
  --log "$DATA/sidecar.log"
