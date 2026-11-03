#!/usr/bin/env bash
# Start the backend and the bridge together, in the order they need.
#
# The backend must be up before the bridge: the bridge posts every incoming
# message to /webhook and drops it if nothing answers. This waits for /health
# before starting the bridge, and stops both on Ctrl-C.
#
#   ./scripts/dev.sh              backend + bridge
#   ./scripts/dev.sh backend      backend only (dashboard, Arianna, stored messages)
set -uo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
MODE="${1:-all}"

die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }

# ---- preflight: fail on what stops it booting, warn on what makes it unsafe ----

[ -x venv/bin/python ] || die "No venv. Run: python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt"
[ -f .env ] || die "No .env. Run: cp .env.example .env   then fill it in."

# Read .env without echoing any value.
set -a; . ./.env; set +a

[ "${ANTHROPIC_API_KEY:-}" = "sk-ant-replace-me" ] && die "ANTHROPIC_API_KEY in .env is still the placeholder."
[ -n "${ANTHROPIC_API_KEY:-}" ] || die "ANTHROPIC_API_KEY is not set in .env."
[ -n "${DASHBOARD_PASSWORD:-}" ] || die "DASHBOARD_PASSWORD is not set in .env — the dashboard would be unprotected."
[ -n "${BRIDGE_API_TOKEN:-}" ] || die "BRIDGE_API_TOKEN is not set in .env — /webhook rejects everything without it."

[ -f contacts.json ] || warn "No contacts.json: Arianna has no valid recipients."
[ -n "${ALLOWED_CONTACTS:-}" ] || warn "ALLOWED_CONTACTS is empty: EVERY sender reaches the assistant."
case ",$(printf %s "${ALLOWED_CONTACTS:-}" | tr 'A-Z' 'a-z' | tr -d ' ')," in
  *,anyone,*) warn "ALLOWED_CONTACTS=anyone: strangers reach the assistant, and what they get is the Contacts tab default." ;;
esac
grep -q '[^[:space:]]' <(sed -n '/^## Schedule/,/^## /p' data/kb/about_me.md 2>/dev/null | sed '1d;$d') \
  || warn "data/kb/about_me.md has no Schedule: 'where are you' questions will be declined."

# ---- backend ----

./venv/bin/uvicorn app.main:app --port "$PORT" &
BACKEND=$!
trap 'kill $BACKEND ${BRIDGE:-} 2>/dev/null' EXIT INT TERM

for _ in $(seq 1 90); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  kill -0 $BACKEND 2>/dev/null || die "Backend exited during startup — see the log above."
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 \
  || die "Backend did not answer /health within 90s (first run loads the Whisper and BLIP models)."

ok "Backend ready — dashboard: http://localhost:$PORT/dashboard"

if [ "$MODE" = "backend" ]; then
  ok "Bridge not started (backend only). WhatsApp auto-replies are off."
  wait $BACKEND
  exit $?
fi

# ---- bridge ----

[ -d whatsapp-bridge/node_modules ] || die "Bridge deps missing. Run: cd whatsapp-bridge && npm install"
ok "Starting the bridge. On a first run, scan the QR code with WhatsApp > Linked devices."
( cd whatsapp-bridge && npm start ) &
BRIDGE=$!

# macOS ships bash 3.2, which has no `wait -n`: poll both children instead.
while kill -0 $BACKEND 2>/dev/null && kill -0 $BRIDGE 2>/dev/null; do sleep 2; done
warn "One of the two processes exited; stopping the other."
