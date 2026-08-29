#!/usr/bin/env bash
# Check the whole stack and say what to do about anything that is wrong.
#
#   bash scripts/check-linux.sh
#
# Read-only: it starts nothing, changes nothing, and sends no messages.
set -uo pipefail

APP_DIR="${APP_DIR:-$HOME/wadam}"
OPENWA_NAME="${OPENWA_NAME:-openwa-api}"
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

pass()  { printf '  \033[32m[ok]\033[0m %s\n' "$1"; }
fail()  { printf '  \033[31m[--]\033[0m %s\n' "$1"; PROBLEMS=$((PROBLEMS + 1)); }
note()  { printf '       \033[33m%s\033[0m\n' "$1"; }
head_() { printf '\n\033[36m%s\033[0m\n' "$1"; }
PROBLEMS=0

# .env holds the ports and the token; read it rather than assuming defaults.
if [ -f "$APP_DIR/.env" ]; then
  get() { grep "^$1=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- ; }
else
  get() { printf ''; }
  echo "  no $APP_DIR/.env — assuming defaults"
fi
WEBHOOK_HOST=$(get WEBHOOK_HOST); WEBHOOK_HOST="${WEBHOOK_HOST:-172.17.0.1}"
WEBHOOK_PORT=$(get WEBHOOK_PORT); WEBHOOK_PORT="${WEBHOOK_PORT:-8765}"
API_PORT=$(get API_PORT)
DB_NAME=$(get DATABASE_NAME);     DB_NAME="${DB_NAME:-wa_events}"

# --- OpenWA -------------------------------------------------------------
head_ "OpenWA"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$OPENWA_NAME"; then
  pass "container running"
else
  fail "container is not running"
  note "docker start $OPENWA_NAME   (or re-run scripts/install-linux.sh)"
fi

KEY=$(docker exec "$OPENWA_NAME" cat /app/data/.api-key 2>/dev/null | tr -d '[:space:]')
if [ -z "$KEY" ]; then
  fail "cannot read OpenWA's API key"
  note "docker logs --tail 40 $OPENWA_NAME"
else
  SESSIONS=$(curl -sf -m 10 -H "x-api-key: $KEY" http://127.0.0.1:2785/api/sessions 2>/dev/null)
  # The id is needed twice below, and the webhook list is per-session — there
  # is no instance-wide /api/webhooks to ask.
  SESSION_ID=$(printf '%s' "$SESSIONS" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
  STATE=$(printf '%s' "$SESSIONS" | grep -o '"status":"[A-Za-z_]*"' | head -1 | cut -d'"' -f4)

  if [ -z "$SESSION_ID" ]; then
    fail "no WhatsApp session on this instance"
    note "from your own machine:  ssh -L 2785:localhost:2785 $USER@$HOST_IP"
    note "then open http://localhost:2785, create a session, scan the QR"
  elif [ "$STATE" = "ready" ]; then
    pass "session ready ($SESSION_ID)"
  else
    fail "session is '$STATE', not ready"
    note "re-pair it through the tunnel above — only a phone can do that"
  fi

  # Inbound only works if OpenWA knows where to deliver.
  if [ -n "$SESSION_ID" ]; then
    HOOKS=$(curl -sf -m 10 -H "x-api-key: $KEY" \
      "http://127.0.0.1:2785/api/sessions/$SESSION_ID/webhooks" 2>/dev/null)
    if printf '%s' "$HOOKS" | grep -q "$WEBHOOK_HOST:$WEBHOOK_PORT"; then
      pass "webhook registered at $WEBHOOK_HOST:$WEBHOOK_PORT"
    else
      fail "OpenWA has no webhook pointing here — inbound messages will not arrive"
      note "registered at startup; the reason it failed is in:  journalctl -u wadam -n 40"
    fi
  fi
fi

# --- wadam --------------------------------------------------------------
head_ "wadam"
if systemctl is-active --quiet wadam; then
  pass "service running since $(systemctl show wadam -p ActiveEnterTimestamp --value | cut -d' ' -f2-3)"
else
  fail "service is not running"
  note "sudo systemctl start wadam && journalctl -u wadam -f"
fi

HEALTH=$(curl -sf -m 5 "http://$WEBHOOK_HOST:$WEBHOOK_PORT/health" 2>/dev/null)
if [ -n "$HEALTH" ]; then
  pass "webhook listener on $WEBHOOK_HOST:$WEBHOOK_PORT"
  printf '       %s\n' "$HEALTH"
  # The listener answering says nothing about the store behind it. The
  # repository catches MongoDB failures and keeps going on its in-memory
  # dicts and the JSON mirror, so a database that refuses every write still
  # looks perfectly healthy from out here. The payload is the only place it
  # shows, so it is checked rather than printed and left to the reader.
  if ! printf '%s' "$HEALTH" | grep -q '"mongo": *"connected"'; then
    fail "the app cannot write to MongoDB — see the mongo field above"
    note "sending still works, and resolution is in memory, but nothing"
    note "is persisted except to the capped JSON mirror in data/"
  fi
else
  fail "nothing answering on $WEBHOOK_HOST:$WEBHOOK_PORT"
fi

if [ -n "$API_PORT" ]; then
  if curl -sf -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
    pass "send API on 127.0.0.1:$API_PORT"
  else
    fail "send API not answering on 127.0.0.1:$API_PORT"
  fi
else
  note "send API is off — no API_PORT in .env"
fi

# --- data ---------------------------------------------------------------
head_ "Data"
if command -v mongosh >/dev/null; then MONGO=mongosh
elif command -v mongo >/dev/null; then MONGO=mongo
else MONGO=""; fi
if [ -n "$MONGO" ]; then
  # stderr is kept: "cannot query" covers a refused connection, a missing
  # database and an unauthenticated one, and only the first is about mongod
  # being down. Saying which saves an hour.
  MONGO_ERR=$($MONGO --quiet "$DB_NAME" --eval 'print(db.chat_configs.countDocuments({}) + " chats, " + db.chat_configs.countDocuments({automation_enabled:true}) + " switched on, " + db.contacts.countDocuments({}) + " contacts, " + db.messages.countDocuments({}) + " messages")' 2>&1)
  case "$MONGO_ERR" in
    *chats*)
      pass "$DB_NAME: $MONGO_ERR"
      case "$MONGO_ERR" in
        "0 chats"*) note "empty — the first sync lands a few seconds after the service starts" ;;
      esac ;;
    *"requires authentication"*|*Unauthorized*|*"not authorized"*)
      fail "MongoDB requires authentication and the URI has no credentials"
      note "MONGODB_URI in $APP_DIR/.env needs a user, then: sudo systemctl restart wadam" ;;
    *"ECONNREFUSED"*|*"connect failed"*|*"Could not connect"*)
      fail "MongoDB is not accepting connections — is mongod running?"
      note "sudo systemctl status mongod" ;;
    *)
      fail "cannot query MongoDB database '$DB_NAME'"
      note "$(printf '%s' "$MONGO_ERR" | head -1)" ;;
  esac
else
  note "no mongosh on PATH — skipping the database check"
fi

MEDIA_DIR=$(get MEDIA_FOLDER); MEDIA_DIR="${MEDIA_DIR:-data/media}"
case "$MEDIA_DIR" in /*) ;; *) MEDIA_DIR="$APP_DIR/$MEDIA_DIR" ;; esac
if [ -d "$MEDIA_DIR/outbox" ]; then
  pass "media: $(find "$MEDIA_DIR" -type f 2>/dev/null | wc -l) file(s), outbox at $MEDIA_DIR/outbox"
else
  fail "no media directory at $MEDIA_DIR"
  note "it is created at startup — the service may not have started yet"
fi

DEFAULT_HOOK=$(get DEFAULT_WEBHOOK)
if [ -n "$DEFAULT_HOOK" ]; then
  pass "DEFAULT_WEBHOOK is $DEFAULT_HOOK"
else
  note "DEFAULT_WEBHOOK unset — incoming messages are stored, nothing is dispatched"
fi

# --- exposure -----------------------------------------------------------
head_ "Exposure"
PUBLIC=$(ss -ltn 2>/dev/null | awk '{print $4}' \
  | grep -E "^(0[.]0[.]0[.]0|[[]::[]]|[*]):(${WEBHOOK_PORT}|${API_PORT:-0}|2785)$")
if [ -z "$PUBLIC" ]; then
  pass "nothing on a public interface"
else
  fail "listening on a public interface: $(printf '%s' "$PUBLIC" | paste -sd' ' -)"
  note "anyone who can reach that could send WhatsApp messages as you"
fi

# --- verdict ------------------------------------------------------------
if [ "$PROBLEMS" -eq 0 ]; then
  printf '\n\033[32mAll good.\033[0m Prove it end to end from your own machine:\n\n'
  printf '  ssh -L %s:localhost:%s %s@%s\n' "${API_PORT:-8766}" "${API_PORT:-8766}" "$USER" "$HOST_IP"
  printf '  curl -s -X POST http://127.0.0.1:%s/wam/ -H "Content-Type: application/json" \\n' "${API_PORT:-8766}"
  printf '    -H "Authorization: Bearer TOKEN" -d %s\n\n' "'"'{"id":"Some Contact","msg":"Hello"}'"'"
  printf '  TOKEN is the API_TOKEN line in %s/.env\n' "$APP_DIR"
else
  printf '\n\033[31m%d problem(s)\033[0m — each is annotated above with what to do.\n' "$PROBLEMS"
  printf 'Logs:  journalctl -u wadam -n 50 --no-pager\n'
fi
