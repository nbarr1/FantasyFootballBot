# Deployment

The runtime host was left as a deferred decision, so both options ship.
Pick one; they are equivalent in behaviour.

Either way, one thing does not change: **the scheduler never submits anything to
MFL.** It polls, analyses, stores recommendations and (optionally) pings you.
Approval is always a separate, interactive command.

## systemd (VPS, home server, Raspberry Pi)

```bash
sudo useradd --system --home /opt/mflbot --shell /usr/sbin/nologin mflbot
sudo mkdir -p /opt/mflbot /etc/mflbot
sudo chown mflbot:mflbot /opt/mflbot

sudo -u mflbot git clone https://github.com/nbarr1/FantasyFootballBot /opt/mflbot
sudo -u mflbot python3 -m venv /opt/mflbot/.venv
sudo -u mflbot /opt/mflbot/.venv/bin/pip install '/opt/mflbot[solver]'

sudo -u mflbot cp /opt/mflbot/config.example.toml /opt/mflbot/config.toml
sudo -u mflbot "$EDITOR" /opt/mflbot/config.toml

# Credentials, mode 0600, root-owned (systemd reads it before dropping privs).
sudo install -m 600 /dev/null /etc/mflbot/secrets.env
sudo "$EDITOR" /etc/mflbot/secrets.env      # see .env.example for the variables

sudo cp /opt/mflbot/deploy/mflbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mflbot
journalctl -u mflbot -f
```

## Docker

```bash
mkdir -p deploy/state
cp config.example.toml deploy/state/config.toml
$EDITOR deploy/state/config.toml

cat > deploy/.env <<'EOF'
MFLBOT_MFL_USERNAME=...
MFLBOT_MFL_PASSWORD=...
MFLBOT_APPROVAL_SECRET=...   # openssl rand -hex 32
MFLBOT_WEBHOOK_URL=...       # optional
EOF
chmod 600 deploy/.env

docker compose -f deploy/docker-compose.yml up -d --build
```

`deploy/state/` holds the database, response cache and pending recommendations.
Back it up; losing it loses your audit trail.

## Before either will do anything

Run these once, in order:

```bash
bot verify-endpoints    # required: writes stay inert until this passes
bot sync-config         # pull scoring rules and league settings
bot whoami              # find your franchise id, then set it in config.toml
bot config-summary      # confirm the parsed settings match your league
```
