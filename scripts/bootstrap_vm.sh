#!/usr/bin/env bash
# Set up the agent on a fresh Debian/Ubuntu VM.
#
# Written for Google Cloud's always-free e2-micro, which has 1 GB of RAM --
# enough for n8n and the agent, but only with swap. Node will be OOM-killed
# mid-npm-install without it, which looks like a hang rather than an error.
#
#   curl -fsSL https://raw.githubusercontent.com/YatishSikka/voice-ops-agent/main/scripts/bootstrap_vm.sh | bash
#
# or, having cloned already:
#   bash scripts/bootstrap_vm.sh
#
# Idempotent: safe to run again after a failure.
set -euo pipefail

REPO_URL="https://github.com/YatishSikka/voice-ops-agent.git"
APP_DIR="${APP_DIR:-$HOME/voice-ops-agent}"
SERVICE_USER="$(whoami)"
TIMEZONE="${TIMEZONE:-America/New_York}"

say() { printf '\n=== %s\n' "$1"; }

say "Swap (1 GB VMs cannot install n8n without it)"
if ! swapon --show | grep -q .; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "  2 GB swap added"
else
  echo "  swap already present"
fi

say "Timezone"
# The agent resolves "tomorrow" against local time, so this is not cosmetic.
sudo timedatectl set-timezone "$TIMEZONE"
echo "  $(timedatectl show -p Timezone --value)"

say "Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv git curl ca-certificates

if ! command -v node >/dev/null; then
  # Distro Node is usually too old for current n8n.
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs
fi
echo "  python $(python3 --version 2>&1 | cut -d' ' -f2), node $(node --version)"

say "Repository"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --quiet "$REPO_URL" "$APP_DIR"
fi

say "Python dependencies"
# gradio is a dev convenience and pulls a large tree; the bot does not import it.
grep -v '^gradio' "$APP_DIR/requirements.txt" > /tmp/req-bot.txt
pip3 install --quiet --break-system-packages -r /tmp/req-bot.txt 2>/dev/null \
  || pip3 install --quiet -r /tmp/req-bot.txt
echo "  installed"

say "Environment file"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "  created $APP_DIR/.env -- you must fill it in"
else
  echo "  $APP_DIR/.env already exists, left alone"
fi

say "Services"
sudo tee /etc/systemd/system/n8n.service >/dev/null <<UNIT
[Unit]
Description=n8n
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
WorkingDirectory=$HOME
ExecStart=/usr/bin/npx --yes n8n start
Restart=always
RestartSec=10
Environment=N8N_PUBLIC_API_DISABLED=false
Environment=N8N_DIAGNOSTICS_ENABLED=false
Environment=GENERIC_TIMEZONE=$TIMEZONE
Environment=TZ=$TIMEZONE
# n8n peaks well above its steady state during startup on a small VM.
MemoryMax=700M

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/voice-agent.service >/dev/null <<UNIT
[Unit]
Description=Voice-Ops Agent (Telegram)
After=network-online.target n8n.service
Wants=network-online.target

[Service]
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=TZ=$TIMEZONE

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now n8n
echo "  n8n enabled (first start downloads n8n; give it a few minutes)"

cat <<NEXT

===========================================================================
Base setup is done. Three things left, in order:

1. Wait for n8n, then create its API key.
     journalctl -u n8n -f          # wait for "Editor is now accessible"
   From your own laptop, tunnel in -- do not open 5678 to the internet:
     gcloud compute ssh $(hostname) -- -L 5678:localhost:5678
   Then open http://localhost:5678, create the owner account, and go to
   Settings -> n8n API -> Create an API key.

2. Fill in $APP_DIR/.env
     GROQ_API_KEY, N8N_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
     N8N_BASE_URL=http://localhost:5678
     PUBLIC_BASE_URL=http://127.0.0.1:7860     # not "localhost" -- see README

3. Add the Google Calendar credential and import the workflows
   (in the tunnelled n8n UI), tag each one 'agent-tool', activate them.

Then start the agent:
     sudo systemctl start voice-agent
     journalctl -u voice-agent -f     # expect "Listening as @yourbot"
===========================================================================
NEXT
