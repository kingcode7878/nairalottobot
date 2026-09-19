# NairaLottoBot 🇳🇬 — Telegram Mini App Bot

`/start` shows a welcome message + Mini App button (e.g. `Welcome Ada to Naija` + `🎮 Play & Win 10,000 naira`).

## Commands

| Command | Who | What |
|---|---|---|
| `/start` | all | Welcome + Play button, registers user |
| `/help` | all | Command list |
| `/examples` | all | Copy-paste variations |
| `/broadcast msg [|| button || url]` | admin | Send to all **with** button. Reply to photo/video with `/broadcast` to resend it. Emoji supported |
| `/broadcastnobutton msg` | admin | Send to all, text only |
| `/setbcbutton text` | admin | Edit broadcast button |
| `/setwelcomebutton text` | admin | Edit welcome button |
| `/setwelcometext text` | admin | Edit welcome msg, use `{name}` |
| `/setminiapp https://...` | admin | Set Mini App URL (must be `https://`) |
| `/addadmin <telegram_id>` | admin | Add admin (masters in env can add; admins can add too) |
| `/listadmins` | admin | Show masters + added admins |
| `/stats` | admin | New today / active today (`/start`) / 7-day / 30-day / total |
| `/daystats` | admin | Mini App opens last 24h (unique + total, includes returning users) |

Quick examples:
```
/setwelcometext Welcome {name} to Naija 🇳🇬🔥 Tap Play & Win 10,000 naira 💰
/setwelcomebutton 🎮 Play & Win 10,000 naira 💰
/broadcast 🔥 Weekend special! Win ₦10,000 💰🍀
/broadcast Big draw tonight 🎉 || 🔥 Play Now 💸
/broadcastnobutton ⚠️ Maintenance at 9pm 🛠️
/addadmin 123456789
/stats
```

## How it works

- Users: `first_seen` (first `/start`) + `last_start` (last `/start`). Cleared-chat users stay in DB so broadcasts still deliver.
- Mini App opens: logged to `app_opens` when frontend calls `Telegram.WebApp.sendData('open')` (handled as `WEB_APP_DATA`). Powers `/daystats`.
- Blocked users: `Forbidden` on send → deleted from DB automatically.
- DB: Neon Postgres if `DATABASE_URL` set, else local SQLite `bot.db`. Tables auto-create on startup.

## Env vars

Copy `.env.example` → `.env`:

```
BOT_TOKEN=123456:ABC-your-botfather-token
MASTER_ADMINS=111111111,222222222
MINI_APP_URL=https://your-mini-app.onrender.com
DATABASE_URL=postgresql://user:pass@ep-xxx.neon.tech/dbname?sslmode=require
WELCOME_TEXT=Welcome {name} to Naija 🇳🇬
WELCOME_BUTTON_TEXT=🎮 Play & Win 10,000 naira
BC_BUTTON_TEXT=🎮 Open Mini App 🍀
```

## Run locally

```powershell
pip install -r requirements.txt
copy .env.example .env
# edit .env, then:
python bot.py
```

## Mini App frontend snippet (for /daystats)

```js
// call once on app open
if (window.Telegram?.WebApp) {
  Telegram.WebApp.ready();
  try { Telegram.WebApp.sendData("open"); } catch (e) {}
}
```

## Deploy: Neon + Render + GitHub

**1. Neon (`neon.tech`):** New Project → Connection Details → copy Direct URI (port 5432):
`postgresql://USER:PASS@ep-xxx.neon.tech/dbname?sslmode=require` → use as `DATABASE_URL`. No manual SQL needed.

**2. GitHub with PAT:** Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate, scope `repo`:
```powershell
git init; git add .; git commit -m "Naija bot"
git branch -M main
git remote add origin https://github.com/YOURUSER/nairalottobot.git
$env:GITHUB_PAT="paste-token"
git push "https://x-access-token:$env:GITHUB_PAT@github.com/YOURUSER/nairalottobot.git" main:main
Remove-Item Env:\GITHUB_PAT
```
Never commit tokens (`.env` is gitignored).

**3. Render:** dashboard.render.com → New → **Background Worker** → connect repo → Build `pip install -r requirements.txt`, Start `python bot.py` (see `render.yaml`) → add env vars `BOT_TOKEN, MASTER_ADMINS, MINI_APP_URL, DATABASE_URL` → Deploy. Logs should show `Postgres (Neon) connected` + `Bot starting (polling)`.

Then in Telegram (as master admin): `/setminiapp https://...`, `/setwelcometext ...`, `/setwelcomebutton ...`, `/setbcbutton ...`.
