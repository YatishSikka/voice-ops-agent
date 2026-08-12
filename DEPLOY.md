# Deploying

The agent needs one thing: a machine that keeps a Python process alive. There
is **no inbound network requirement** — Telegram is reached by outbound long
polling, and n8n runs beside the agent on a private network. No public URL, no
tunnel, no port forwarding, no TLS certificate.

That is the whole reason this is easy. Getting here took two hosting dead ends
(see the README's limitations), and the fix was to stop needing a web server at
all.

## What has to run

| Process | Why | Memory |
|---|---|---|
| `bot.py` | polls Telegram, runs the agent, serves n8n's callbacks | ~150 MB |
| n8n | holds the workflows and the Google credential | ~400 MB |

Both are in `docker-compose.yml`. A host with **1 GB of RAM** is enough; 2 GB
is comfortable.

## Free hosts that actually work

The constraint is a process that runs continuously. That rules out most free
"web service" tiers, which sleep when no HTTP request arrives — a sleeping bot
stops polling and simply misses your messages.

| Host | Free tier | Notes |
|---|---|---|
| **Google Cloud Always Free** | 1 × `e2-micro`, us-west1/central1/east1 | **Recommended.** 1 GB RAM is enough with swap, availability is reliable, and idle instances are not reclaimed. |
| **Oracle Cloud Always Free** | 2 ARM cores / 12 GB, or 2 AMD micro VMs | Far more headroom, but reclaims instances idle for 7 days (CPU/network/memory each under 10% at p95) — which is precisely what a polling bot looks like. ARM capacity is also frequently unavailable, and the ARM allowance was halved in 2026. |
| **Any cheap VPS** | ~$4/month | Hetzner, Vultr, DigitalOcean. Worth it if free tiers frustrate you. |
| **Your own machine** | free | Fine, but the agent is only reachable while it is powered on. |

Render, Railway and Fly are poor fits here: their free offerings are
request-driven web services, and this is a long-lived worker.

## Google Cloud always-free, start to finish

GCP gives one `e2-micro` free indefinitely, but only in **us-west1,
us-central1 or us-east1** — any other region is billed. It has 1 GB of RAM,
which fits n8n and the agent **only with swap**; without it, `npm install` for
n8n gets OOM-killed and looks like a hang.

Create the VM:

```
Compute Engine → Create instance
  Region       us-central1  (or us-west1 / us-east1 — nothing else is free)
  Machine type e2-micro
  Boot disk    Ubuntu 24.04 LTS, 30 GB standard persistent disk
  Firewall     leave both HTTP boxes unchecked — nothing needs inbound
```

Then, on the instance:

```bash
curl -fsSL https://raw.githubusercontent.com/YatishSikka/voice-ops-agent/main/scripts/bootstrap_vm.sh | bash
```

That adds 2 GB of swap, sets the timezone, installs Python and Node 22,
clones the repo, and installs both services. It prints the three remaining
steps, all of which need the n8n UI.

Reach that UI by tunnelling — never by opening port 5678:

```bash
gcloud compute ssh voice-agent -- -L 5678:localhost:5678
# then browse to http://localhost:5678 on your own machine
```

The tunnel has a second benefit: n8n sees itself as `http://localhost:5678`,
which is the redirect URI already registered in your Google Cloud OAuth client,
so the Calendar credential authorises with no changes.

### What this costs

Nothing, if you stay inside the free tier, but note the edges:

- **Region matters.** An `e2-micro` outside those three regions is billed.
- **Egress is capped**, and sources disagree on the figure (1 GB/month under
  the long-standing terms, 200 GB under the Standard network tier in US
  regions). Check the current docs if it matters to you. It does not matter
  much here: the agent sends audio to Groq and voice notes to Telegram, a few
  hundred KB per exchange, so even the smaller figure allows hundreds of
  interactions a month.
- **A billing account with a card is required** even for the free tier. Set a
  budget alert at $1 so a mistake is caught early.
- **30 GB disk** is the free ceiling; the default 10 GB is plenty here.

## Deploying to a VM (Docker)

```bash
# on the server
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/YatishSikka/voice-ops-agent.git
cd voice-ops-agent

cp .env.example .env
nano .env            # Groq key, Telegram token, chat id, allowlist

docker compose up -d
docker compose logs -f bot     # expect: "Listening as @yourbot"
```

`N8N_BASE_URL` and `PUBLIC_BASE_URL` are set by compose to the service names
(`http://n8n:5678`, `http://bot:7860`). Do not point them at `localhost` in a
container — that is the container's own loopback, and the usual first failure.

## Without Docker

If you would rather not run containers -- or you are on a 1 GB VM where the
overhead matters -- this is the same setup as local development, plus systemd
so both survive a reboot.

```bash
sudo apt install -y python3-pip nodejs npm
git clone https://github.com/YatishSikka/voice-ops-agent.git
cd voice-ops-agent && pip install -r requirements.txt
cp .env.example .env && nano .env
```

Two unit files, `/etc/systemd/system/n8n.service`:

```ini
[Unit]
Description=n8n
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu
ExecStart=/usr/bin/npx n8n start
Restart=always
Environment=N8N_PUBLIC_API_DISABLED=false

[Install]
WantedBy=multi-user.target
```

and `/etc/systemd/system/voice-agent.service`:

```ini
[Unit]
Description=Voice-Ops Agent
After=network.target n8n.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/voice-ops-agent
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now n8n voice-agent
journalctl -u voice-agent -f
```

Here `N8N_BASE_URL=http://localhost:5678` and
`PUBLIC_BASE_URL=http://127.0.0.1:7860` are correct, since both processes share
a host. Use `127.0.0.1`, not `localhost`, for the callback — n8n is Node, and
Node resolves `localhost` to IPv6 first.

## Then set up n8n

n8n starts empty. Its editor is deliberately bound to localhost, because it
holds your Google credential and should not face the internet. Reach it through
an SSH tunnel:

```bash
ssh -L 5678:localhost:5678 user@your-server
# now open http://localhost:5678 in your own browser
```

Then:

1. Create the owner account.
2. **Settings → n8n API → Create an API key**, and put it in `.env` as
   `N8N_API_KEY`. Restart the bot: `docker compose restart bot`.
3. Add the **Google Calendar OAuth2** credential. The redirect URI must match
   what n8n shows; over a tunnel it is `http://localhost:5678/rest/oauth2-credential/callback`.
4. Import the workflows from `n8n/workflows/*.json`, re-attach the credential
   to each Google Calendar node, tag each one `agent-tool`, and activate them.

Message the bot `/tools` to confirm it can see them.

## Keeping it running

`restart: unless-stopped` brings both containers back after a crash or reboot,
provided Docker starts at boot (`sudo systemctl enable docker`).

Check on it with:

```bash
docker compose ps
docker compose logs --tail=50 bot
curl -s localhost:7860/healthz    # from on the host
```

## What breaks, and what it looks like

| Symptom | Cause |
|---|---|
| Bot answers, but says it has no tools | n8n unreachable, or workflows not tagged/activated |
| "This assistant is private" | your chat id is not in `TELEGRAM_ALLOWED_CHATS` |
| "I've used up my daily quota" | Groq's 100K tokens/day; resets on its own |
| Background jobs never report back | `PUBLIC_BASE_URL` wrong — must be `http://bot:7860` in compose |
| Calendar tools return nothing | OAuth credential expired; re-authorise in n8n |

## A note on secrets

`.env` holds a Groq key, an n8n API key and a Telegram bot token. It is
gitignored and must never be committed. Google OAuth tokens live inside the
`n8n_data` volume, not in the repo — which is also why losing that volume means
redoing the OAuth consent, not just re-importing workflows.
