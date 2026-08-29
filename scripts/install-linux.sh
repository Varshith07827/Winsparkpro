#!/usr/bin/env bash
# Install wadam and OpenWA on a Linux server. Run it ON the server.
#
#   bash scripts/install-linux.sh
#
# Idempotent: safe to run again. It never overwrites an existing .env, never
# touches nginx, and puts its MongoDB data in its own database, so an app
# already running on this machine is left alone.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/wadam}"
OPENWA_DIR="${OPENWA_DIR:-$HOME/openwa}"
WEBHOOK_PORT="${WEBHOOK_PORT:-8765}"
API_PORT="${API_PORT:-8766}"
DB_NAME="${DB_NAME:-wa_events}"

say()  { printf '\n\033[36m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mOK\033[0m   %s\n' "$1"; }
warn() { printf '   \033[33mWARN\033[0m %s\n' "$1"; }
die()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

# ── 1. what is already here ─────────────────────────────────────────────
say "Checking prerequisites"

command -v python3 >/dev/null || die "python3 not installed:  sudo apt install -y python3 python3-venv"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "Python $PYV is too old — 3.11+ needed"
python3 -c 'import venv' 2>/dev/null || die "python3-venv missing:  sudo apt install -y python3-venv"
ok "Python $PYV"

command -v docker >/dev/null || die "docker not installed:  curl -fsSL https://get.docker.com | sudo sh"

# "Cannot connect to the daemon" and "permission denied on the socket" are
# different problems with different fixes, and the fix for one does nothing
# for the other. Told apart rather than guessed at.
if ! docker info >/dev/null 2>&1; then
  DOCKER_ERR=$(docker info 2>&1 || true)
  if echo "$DOCKER_ERR" | grep -qi "permission denied"; then
    # Being in the group and having it in THIS shell are different things:
    # usermod takes effect at the next login, so telling someone who has
    # already run it to run it again is the least useful thing to say.
    if id -nG | grep -qw docker; then
      die "you are in the docker group, but this shell started before that
        happened. Either re-run it with the group applied:

          sg docker -c 'bash $0'

        or open a new session (reconnect, if this is VS Code Remote) and run
        it normally."
    else
      die "no permission on the docker socket. Add yourself to the group and
        re-run without logging out:

          sudo usermod -aG docker \$USER
          sg docker -c 'bash $0'"
    fi
  elif ! systemctl is-active --quiet docker 2>/dev/null; then
    die "the docker daemon is not running:  sudo systemctl enable --now docker"
  else
    die "cannot talk to docker: $(echo "$DOCKER_ERR" | head -2 | paste -sd' ' -)"
  fi
fi
ok "Docker"

# Compose is a convenience here, not a requirement: this installs exactly one
# container, and `docker run` expresses the same thing. Ubuntu's own docker.io
# package ships no compose plugin and `docker-compose-plugin` only exists in
# Docker's repo, so insisting on it would send people adding apt sources for
# a single container.
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null; then
  COMPOSE="docker-compose"
else
  COMPOSE=""
fi
ok "${COMPOSE:-docker run (no compose needed)}"

if command -v mongod >/dev/null || systemctl is-active --quiet mongod 2>/dev/null; then
  ok "MongoDB present — this install will use its own database '$DB_NAME'"
else
  warn "No local MongoDB. Either install one, or put an Atlas URI in .env later."
fi

# The gateway the OpenWA container will reach this host on. Detected rather
# than assumed: 172.17.0.1 is only the default bridge, and a compose project
# creates its own network with a different gateway.
BRIDGE_IP=$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)
BRIDGE_IP="${BRIDGE_IP:-172.17.0.1}"
ok "container reaches this host at $BRIDGE_IP"

# ── 2. OpenWA ───────────────────────────────────────────────────────────
say "OpenWA"
mkdir -p "$OPENWA_DIR/data"

# Every option below has to match between the two paths, so they are named once.
OPENWA_IMAGE="${OPENWA_IMAGE:-rmyndharis/openwa:latest}"
OPENWA_NAME="openwa-api"

if [ -n "$COMPOSE" ]; then
  cd "$OPENWA_DIR"
  if [ ! -f docker-compose.yml ]; then
    cat > docker-compose.yml <<COMPOSEEOF
services:
  openwa:
    image: $OPENWA_IMAGE
    container_name: $OPENWA_NAME
    restart: unless-stopped
    # Loopback only. Nothing outside this machine has any business reaching
    # OpenWA — its API key can do anything on the instance.
    ports:
      - "127.0.0.1:2785:2785"
    volumes:
      - ./data:/app/data
    environment:
      # wadam listens on the host; the container must be allowed to reach it.
      # OpenWA blocks private addresses by default and would refuse silently.
      - SSRF_ALLOWED_HOSTS=$BRIDGE_IP,host.docker.internal
    extra_hosts:
      # Docker Desktop provides this on Windows and macOS; the Linux daemon
      # does not, so it is added explicitly.
      - "host.docker.internal:host-gateway"
COMPOSEEOF
    ok "wrote $OPENWA_DIR/docker-compose.yml"
  else
    ok "docker-compose.yml already exists — left alone"
  fi
  $COMPOSE up -d
elif docker ps -a --format '{{.Names}}' | grep -qx "$OPENWA_NAME"; then
  docker start "$OPENWA_NAME" >/dev/null
  ok "started the existing container"
else
  # An array rather than backslash continuations: those collapse silently if
  # anything ever rewrites this file, and a 250-character docker run is not
  # something anyone should have to read.
  run=(docker run -d
    --name "$OPENWA_NAME"
    --restart unless-stopped
    -p 127.0.0.1:2785:2785
    -v "$OPENWA_DIR/data:/app/data"
    -e "SSRF_ALLOWED_HOSTS=$BRIDGE_IP,host.docker.internal"
    --add-host "host.docker.internal:host-gateway"
    "$OPENWA_IMAGE")
  "${run[@]}" >/dev/null
  ok "created the container with docker run"
fi
ok "container up"

printf '   waiting for OpenWA to answer'
for _ in $(seq 1 60); do
  if curl -sf -m 2 http://127.0.0.1:2785/api/health >/dev/null 2>&1; then break; fi
  printf '.'; sleep 2
done
printf '\n'
curl -sf -m 5 http://127.0.0.1:2785/api/health >/dev/null   || die "OpenWA did not come up — check:  docker logs --tail 40 $OPENWA_NAME"
ok "OpenWA healthy"

for _ in $(seq 1 30); do
  [ -f "$OPENWA_DIR/data/.api-key" ] && break
  sleep 2
done
[ -f "$OPENWA_DIR/data/.api-key" ] || die "OpenWA never wrote data/.api-key"
OPENWA_KEY=$(tr -d '\r\n' < "$OPENWA_DIR/data/.api-key")
ok "read the API key"

# ── 3. wadam ────────────────────────────────────────────────────────────
say "wadam"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch wadam https://github.com/Varshith07827/Winsparkpro.git "$APP_DIR"
  ok "cloned"
else
  git -C "$APP_DIR" pull --ff-only
  ok "updated"
fi
cd "$APP_DIR"

python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
# No Qt: nothing on the headless path imports PySide6, and a server has no
# reason to carry ~150MB of it.
./.venv/bin/pip install --quiet -r requirements-headless.txt
ok "dependencies installed (headless — no Qt)"

if [ -f .env ]; then
  ok ".env exists — left untouched"
else
  API_TOKEN=$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')
  cat > .env <<ENVEOF
# Written by scripts/install-linux.sh

MONGODB_URI=mongodb://localhost:27017
DATABASE_NAME=$DB_NAME
OPENWA_API_KEY=$OPENWA_KEY

# Bound to the docker bridge, NOT 0.0.0.0: the OpenWA container can reach it
# there and the public internet cannot. A loopback bind would not work — on
# Linux the container reaches the host at the bridge, not at 127.0.0.1.
WEBHOOK_HOST=$BRIDGE_IP
WEBHOOK_PORT=$WEBHOOK_PORT
WEBHOOK_PUBLIC_URL=http://$BRIDGE_IP:$WEBHOOK_PORT/hook

# Where incoming messages are POSTed, for chats with no URL of their own.
# Nothing is dispatched until this is set, or a per-chat URL is.
# DEFAULT_WEBHOOK=https://your.server/hook

# The send API, on loopback. Reach it with an SSH tunnel:
#   ssh -L $API_PORT:localhost:$API_PORT $USER@this-host
API_PORT=$API_PORT
API_HOST=127.0.0.1
API_TOKEN=$API_TOKEN
ENVEOF
  chmod 600 .env
  ok "wrote .env (API key filled in, token generated)"
fi

# ── 4. service ──────────────────────────────────────────────────────────
say "systemd unit"
UNIT=/etc/systemd/system/wadam.service
sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=WhatsApp Automation Manager
After=network-online.target docker.service mongod.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run_headless.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNITEOF
sudo systemctl daemon-reload
sudo systemctl enable wadam >/dev/null 2>&1
ok "installed $UNIT (starts at boot)"

# ── what is left ────────────────────────────────────────────────────────
say "Two things left, both need you"
cat <<NEXT
   1. LINK A WHATSAPP SESSION. From your own machine:

        ssh -L 2785:localhost:2785 $USER@$(hostname -I 2>/dev/null | awk '{print $1}')

      then open http://localhost:2785 in your browser, create a session and
      scan the QR with the phone. Only a phone can do this.

   2. START IT, once the session says ready:

        sudo systemctl start wadam
        journalctl -u wadam -f

   Then check it:

        curl -s http://$BRIDGE_IP:$WEBHOOK_PORT/health

   And send a message, through a tunnel from your own machine:

        ssh -L $API_PORT:localhost:$API_PORT $USER@this-host
        curl -s -X POST http://127.0.0.1:$API_PORT/wam/ \\
          -H 'Content-Type: application/json' \\
          -H "Authorization: Bearer \$(grep '^API_TOKEN=' $APP_DIR/.env | cut -d= -f2)" \\
          -d '{"id":"Some Contact","msg":"Hello"}'

   Nothing listens on a public interface: OpenWA is loopback, the send API is
   loopback, and wadam's webhook port is on the docker bridge.
NEXT
