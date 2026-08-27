# Deployment

The runtime host was left as a deferred decision, so both options ship.
Pick one; they are equivalent in behaviour.

Each one comes in two shapes:

* **Headless.** The scheduler alone (`bot run`), decided on from the CLI.
* **Dashboard.** `bot serve --with-scheduler`: the same scheduler plus the web
  interface, in one process. Approve, reject, edit, run ingestion and analysis,
  and read the audit trail from a browser.

Run one or the other, not both -- two processes writing to one SQLite file
contend for its write lock.

Either way, one thing does not change: **nothing is submitted to MFL without a
per-action approval.** The scheduler polls, analyses, stores recommendations and
(optionally) pings you; the dashboard's action buttons run only read and
analysis commands. Approving is always a separate, deliberate act, and it
authorises exactly one submission of exactly one payload.

## systemd (VPS, home server, Raspberry Pi)

```bash
sudo useradd --system --home /opt/mflbot --shell /usr/sbin/nologin mflbot
sudo mkdir -p /opt/mflbot /etc/mflbot
sudo chown mflbot:mflbot /opt/mflbot

sudo -u mflbot git clone https://github.com/nbarr1/FantasyFootballBot /opt/mflbot
sudo -u mflbot python3 -m venv /opt/mflbot/.venv
sudo -u mflbot /opt/mflbot/.venv/bin/pip install '/opt/mflbot[solver]'

sudo -u mflbot /opt/mflbot/.venv/bin/bot --config /opt/mflbot/config.toml init
sudo -u mflbot "$EDITOR" /opt/mflbot/config.toml

# Credentials, mode 0600, root-owned (systemd reads it before dropping privs).
sudo install -m 600 /dev/null /etc/mflbot/secrets.env
sudo "$EDITOR" /etc/mflbot/secrets.env      # see .env.example for the variables

# Headless: the scheduler only.
sudo cp /opt/mflbot/deploy/mflbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mflbot
journalctl -u mflbot -f
```

For the dashboard instead, install the web extra and the other unit:

```bash
sudo -u mflbot /opt/mflbot/.venv/bin/pip install '/opt/mflbot[solver,web]'

# Add a dashboard password to the secrets file (mode 0600, root-owned):
#   MFLBOT_WEB_PASSWORD=...
sudo "$EDITOR" /etc/mflbot/secrets.env

sudo cp /opt/mflbot/deploy/mflbot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now mflbot          # do not run both
sudo systemctl enable --now mflbot-web
```

It binds 127.0.0.1 and speaks plain HTTP. Reach it from your laptop with an SSH
tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@host    # then open http://127.0.0.1:8765
```

With no `MFLBOT_WEB_PASSWORD` set, the server prints a login link at startup
(`journalctl -u mflbot-web`), valid until the service restarts, and refuses to
bind anything but loopback.

## Docker

```bash
mkdir -p deploy/state
docker run --rm -v "$PWD/deploy/state:/data" -w /data \
  "$(docker build -q .)" init
$EDITOR deploy/state/config.toml

# The container's working directory is the mounted volume above, not the repo
# checkout -- copy the repo's committed, hand-verified endpoints.lock.json in
# alongside config.toml, or every write capability starts UNVERIFIED again.
cp endpoints.lock.json deploy/state/

cat > deploy/.env <<'EOF'
MFLBOT_MFL_USERNAME=...
MFLBOT_MFL_PASSWORD=...
MFLBOT_APPROVAL_SECRET=...   # openssl rand -hex 32
MFLBOT_WEB_PASSWORD=...      # signs you in to the dashboard
MFLBOT_WEBHOOK_URL=...       # optional
EOF
chmod 600 deploy/.env

docker compose -f deploy/docker-compose.yml up -d --build
```

The container runs the dashboard and the scheduler together, published to
`127.0.0.1:8765` on the host -- open it and sign in with `MFLBOT_WEB_PASSWORD`.
For a headless container, change the compose `command` to `["run"]` and drop
the published port.

`deploy/state/` holds the database, response cache and pending recommendations.
Back it up; losing it loses your audit trail.

## Notice when it stops

Whichever shape you run, set a check-in URL before you stop thinking about it:

```
MFLBOT_HEARTBEAT_URL=https://hc-ping.com/your-uuid
```

The bot pings it on every watchdog cycle while its jobs are keeping up, and
deliberately stops while they are not. A missed check-in is then the alarm for
both failure modes — a stalled bot and a dead one — and it is the only one that
survives the machine losing power. `bot heartbeat` exits 2 when something is
stale, if you would rather drive it from a cron you already have.

## Before either will do anything

Run these once, in order:

```bash
bot verify-endpoints    # required: writes stay inert until this passes
bot sync-config         # pull scoring rules and league settings
bot whoami              # find your franchise id, then set it in config.toml
bot config-summary      # confirm the parsed settings match your league
```
