# Deploying `binance-square-engine` on Hostinger / any Ubuntu VPS

This guide assumes you have a fresh Ubuntu 22.04 / 24.04 LTS VPS with sudo
access. Estimated time: 15–25 minutes.

## 0. Risk acknowledgement

Programmatic posting on Binance Square at scale **may** violate the platform's
Terms of Service. Operating this engine can lead to your Binance account being
flagged or permanently banned. Run with `PUBLISH_MODE=dry_run` for the first
2 weeks, then ramp up slowly (5–10 posts/day) before enabling the full schedule.

## 1. System prerequisites

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv python3-pip \
  git curl ca-certificates \
  fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-noto-extra \
  fontconfig tini \
  libnss3 libatk-bridge2.0-0 libdrm2 libgbm1 libasound2 libxshmfence1 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgtk-3-0 \
  build-essential
sudo fc-cache -f -v
```

## 2. Dedicated user

```bash
sudo useradd --system --create-home --shell /bin/bash bse
sudo install -d -o bse -g bse /opt/binance-square-engine
sudo -u bse bash <<'EOF'
cd /opt/binance-square-engine
git clone https://github.com/pinzo435-spec/binance-square-engine .
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
EOF
```

## 3. Configuration

```bash
sudo -u bse cp /opt/binance-square-engine/.env.example /opt/binance-square-engine/.env
sudo -u bse $EDITOR /opt/binance-square-engine/.env
```

Required keys (minimum):

```
GEMINI_API_KEY=...        # from https://aistudio.google.com/apikey
IMGBB_API_KEY=...         # from https://api.imgbb.com
ACCOUNT_HANDLE=KinzoTech
SQUARE_UID=<your_uid>     # from Binance Square profile URL
PUBLISH_MODE=dry_run      # KEEP THIS UNTIL YOU'RE READY
MAX_POSTS_PER_DAY=10      # start low, ramp slowly
SCHEDULER_TIMEZONE=Asia/Riyadh
```

## 4. Database initialisation

```bash
sudo -u bse /opt/binance-square-engine/.venv/bin/bse init
```

## 5. Optional — cookies for browser publishing

If you want native image upload (vs. `dry_run`/ImgBB-link mode), you need an
authenticated Binance Square session.

**On a desktop with a GUI** (NOT the headless VPS):

```bash
bse cookies-export
# Browser opens. Log in to Binance manually. Close the window when done.
# A binance_cookies.json file is written to data/runtime/.
# Copy it to the VPS:
scp data/runtime/binance_cookies.json bse@<vps-ip>:/opt/binance-square-engine/data/runtime/
ssh bse@<vps-ip> chmod 600 /opt/binance-square-engine/data/runtime/binance_cookies.json
```

Once cookies are in place, validate the selectors:

```bash
sudo -u bse /opt/binance-square-engine/.venv/bin/bse selectors-tune --headless
```

You should see `YES` for at least `editor_textarea`, `image_upload_input`,
and `publish_button`. If not, edit
`data/selectors/binance_square.yaml` and add the matching candidate(s).

## 6. Run as a systemd service

```bash
sudo cp /opt/binance-square-engine/deploy/systemd/binance-square-engine.service /etc/systemd/system/
sudo cp /opt/binance-square-engine/deploy/systemd/binance-square-engine-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-square-engine
sudo systemctl enable --now binance-square-engine-dashboard
```

Verify:

```bash
systemctl status binance-square-engine
journalctl -u binance-square-engine -f
curl -fsS http://localhost:8000/health
```

## 7. Nginx reverse proxy + Basic Auth (recommended)

```bash
sudo apt-get install -y nginx apache2-utils
sudo htpasswd -c /etc/nginx/.bse-htpasswd kinzo
sudo tee /etc/nginx/sites-available/bse <<'EOF'
server {
  listen 80;
  server_name bse.example.com;
  auth_basic "binance-square-engine";
  auth_basic_user_file /etc/nginx/.bse-htpasswd;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
  }
}
EOF
sudo ln -sf /etc/nginx/sites-available/bse /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bse.example.com    # optional HTTPS
```

## 8. Docker alternative

```bash
docker compose up -d --build
docker compose logs -f engine
```

`data/`, `prompts/`, and `playbooks/` are bind-mounted so config changes
persist across rebuilds.

## 9. Operational checklist

- [ ] `bse scan` returns ≥ 30 opportunities
- [ ] `bse hook --ticker BTC --trigger PUMP --tendency 1` returns a valid Arabic hook
- [ ] `bse render --ticker BTC` writes PNGs to `data/runtime/images/`
- [ ] `PUBLISH_MODE=dry_run bse run-slot power_hour` completes with `success=True`
- [ ] `systemctl status binance-square-engine` shows `active (running)`
- [ ] `curl http://localhost:8000/health` returns `{"ok":true}`
- [ ] Dashboard renders posts at `http://<vps>:8000` (or via Nginx)
- [ ] First 14 days run in `dry_run` mode for safety; then flip to `hybrid`

## 10. Day-2 operations

| Task                                | Command                                          |
|-------------------------------------|--------------------------------------------------|
| View tail of logs                   | `journalctl -u binance-square-engine -f`         |
| Pause posting for 2 hours           | `bse pause --hours 2 --reason "maintenance"`     |
| Resume                              | `bse resume`                                     |
| Refresh few-shot prompt examples    | `bse learn`                                      |
| Manual single-slot fire             | `bse run-slot prime_gcc`                         |
| Take an out-of-cycle DB snapshot    | `bse snapshot`                                   |
| Update code                         | `cd /opt/binance-square-engine && git pull && .venv/bin/pip install -e . && sudo systemctl restart binance-square-engine` |
| Rotate cookies                      | run `bse cookies-export` on desktop; scp; restart |

## 11. Troubleshooting

| Symptom                              | Likely cause / fix                              |
|--------------------------------------|-------------------------------------------------|
| `cookies_invalid_or_expired`         | Rotate cookies via §5                          |
| `editor_not_found`                   | Selector drift; `bse selectors-tune` + edit YAML |
| `gemini_quota_exhausted` chain       | Free tier rate limit; wait or upgrade plan      |
| `image_upload_failed`                | Check ImgBB key + outbound HTTPS                |
| Daemon crashes on memory             | Lower `MAX_POSTS_PER_HOUR` and ensure `MemoryHigh` |
| No opportunities                     | Run `bse scan` manually; check `data/runtime/engine.db` |
