# TG AdBot — Worker

This worker connects your Telegram accounts to the dashboard and performs all the actual sending.

## Setup (any Linux/macOS/Windows machine with Python 3.10+)

```bash
pip install -r requirements.txt
python worker.py
```

The included `.env` already has your dashboard URL and worker token filled in — keep it safe.

## What it does

- Polls the dashboard for jobs (login, join group, send ad)
- Performs MTProto operations via Telethon
- Honors Telegram's flood-wait responses
- Reports every send (success / flood / forbidden / banned / error) back to the dashboard

## VPS recommendation

Run under `systemd`, `pm2`, or in a `tmux`/`screen` session so it keeps running after you log out.

Example systemd unit (`/etc/systemd/system/tg-adbot-worker.service`):

```
[Unit]
Description=TG AdBot worker
After=network.target

[Service]
WorkingDirectory=/opt/tg-adbot
ExecStart=/usr/bin/python3 worker.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/tg-adbot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-adbot-worker
```

## Converting an existing .session file to a StringSession

If you already have a Telethon `.session` file you want to paste into the dashboard:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = 123456; api_hash = "yourhash"
with TelegramClient("existing", api_id, api_hash) as c:
    print(StringSession.save(c.session))
```

Paste the printed string into "Add account → Session string" in the dashboard.

## Warning

Mass-sending from user accounts violates Telegram's Terms of Service. Accounts can be permanently banned. Use conservative intervals and warm new accounts up before blasting.
