# Deploying the tracker bot

Two separate "servers" are involved and it's easy to conflate them: the **Discord
server** (where the bot lives socially) and the **host machine** (where the Python
process runs). You need both.

The bot makes an outbound websocket connection to Discord. It needs no open ports,
no domain, and no TLS certificate — nothing on the internet ever connects *to* it.

---

## 1. Discord side

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. Treat it like a password; anyone with
   it controls the bot. If it leaks, reset it — the old one dies immediately.
3. Leave all three **Privileged Gateway Intents** off. This bot doesn't need them.
4. **OAuth2 → URL Generator**:
   - Scopes: `bot` **and** `applications.commands`
   - Bot permissions: **Send Messages**, **Embed Links**, **Attach Files**,
     **Read Message History**
5. Open the generated URL and invite the bot to your server.

`Attach Files` is not optional — `/tree show` uploads a PNG and will fail
silently without it.

To get your server's ID for instant command sync: Discord **Settings → Advanced →
Developer Mode**, then right-click the server icon → **Copy Server ID**.

---

## 2. Host machine

### What to run it on

This bot is tiny — idle most of the time, a few MB of RAM, a SQLite file that
will take years to reach a megabyte.

| Option | Notes |
|---|---|
| **VPS** (Hetzner, DigitalOcean, Vultr, Linode) | $4–6/month, the default answer. Any 1 vCPU / 512 MB–1 GB tier is oversized for this. |
| **Raspberry Pi on your own network** | Genuinely fine. No monthly cost. Just needs to stay powered and online. |
| **Oracle Cloud always-free ARM** | Free permanently, but signup is fussy and accounts occasionally get reclaimed. |

**Avoid anything with an ephemeral filesystem** — Heroku, Railway's default
dynos, Replit, most "deploy from GitHub" platforms. They wipe the disk on every
restart or redeploy, which silently destroys `tracker.db`. If you use one anyway,
attach a persistent volume and point the database at it. A plain VPS with a real
disk avoids the whole class of problem.

### Setup (Ubuntu 24.04)

```bash
# as root, on a fresh box
adduser --system --group --home /opt/tracker tracker
apt update
apt install -y python3-venv python3-pip fonts-dejavu-core
```

`fonts-dejavu-core` matters: the tech tree renderer loads DejaVu from
`/usr/share/fonts/truetype/dejavu`. Minimal server images often ship without it,
and Pillow will quietly fall back to a tiny bitmap font — the tree renders, but
looks broken.

Copy the code up from your laptop:

```bash
scp bot.py db.py tree_render.py requirements.txt root@YOUR_SERVER:/opt/tracker/
```

Then back on the server:

```bash
cd /opt/tracker
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
chown -R tracker:tracker /opt/tracker
```

### Secrets

Never put the token in the code or in the repo.

```bash
cat > /etc/tracker.env <<'EOF'
DISCORD_TOKEN=paste-your-token-here
GUILD_ID=your-server-id
EOF
chmod 600 /etc/tracker.env
```

`GUILD_ID` is optional. With it, slash commands appear the instant the bot
starts. Without it, Discord registers them globally and they can take up to an
hour to show up — which is the single most common "why isn't it working" moment.
Set it while you're getting started; drop it if you ever run in several servers.

### Run it as a service

```bash
cp tracker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tracker
systemctl status tracker
```

`Restart=always` brings it back after a crash, and `enable` brings it back after
a reboot. Watch it start up:

```bash
journalctl -u tracker -f
```

You're looking for `Logged in as YourBot#1234 · 1 guild(s)`. Then type `/` in
your Discord server and the commands should be there.

---

## 3. Keeping it alive

### Backups

Don't `cp` a live SQLite file — you can capture a half-written transaction. Use
SQLite's own backup command, which is safe against a running process:

```bash
apt install -y sqlite3
mkdir -p /opt/tracker/backups

crontab -e
```

```cron
0 3 * * * sqlite3 /opt/tracker/tracker.db ".backup '/opt/tracker/backups/tracker-$(date +\%F).db'"
0 4 * * * find /opt/tracker/backups -name '*.db' -mtime +30 -delete
```

Pull a copy off the machine periodically too — a backup that only exists on the
server it's backing up isn't a backup.

### Updating the code

```bash
scp bot.py db.py tree_render.py root@YOUR_SERVER:/opt/tracker/
ssh root@YOUR_SERVER systemctl restart tracker
```

Schema changes are additive (`CREATE TABLE IF NOT EXISTS`), so restarting on new
code won't drop existing data. Take a backup first anyway.

### Common failures

| Symptom | Cause |
|---|---|
| Commands don't appear | Global sync still propagating — set `GUILD_ID`. Or the invite missed the `applications.commands` scope; re-invite with both scopes. |
| `/tree show` does nothing | Missing **Attach Files** permission in that channel. |
| Tech tree text looks tiny and wrong | `fonts-dejavu-core` not installed. |
| Bot online but silent | Check `journalctl -u tracker -n 50` — an unhandled exception in a command shows up there. |
| Everything vanished after a redeploy | Ephemeral filesystem. Move to a host with a real disk. |
