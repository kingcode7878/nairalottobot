"""
Naija Mini App Bot
- /start -> welcome message + Mini App button (WebApp)
- /help, /examples, /stats, /daystats
- /broadcast, /broadcastnobutton, /setbcbutton, /setwelcomebutton, /setwelcometext, /addadmin
- Admins + Master Admins, Neon (Postgres) / SQLite fallback
- Blocked users removed from DB on broadcast; cleared-chat users still receive broadcasts.

Env vars (see .env.example):
  BOT_TOKEN, MASTER_ADMINS, MINI_APP_URL, DATABASE_URL (Neon), optional defaults
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("naija-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MINI_APP_URL_DEFAULT = os.getenv("MINI_APP_URL", "").strip()
MASTER_ADMINS = {
    int(x.strip())
    for x in os.getenv("MASTER_ADMINS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DEFAULT_WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "Welcome {name} to Naija 🇳🇬\n\nTap below to Play & Win 10,000 naira 💰🍀",
)
DEFAULT_WELCOME_BUTTON = os.getenv("WELCOME_BUTTON_TEXT", "🎮 Play & Win 10,000 naira")
DEFAULT_BC_BUTTON = os.getenv("BC_BUTTON_TEXT", "🎮 Open Mini App 🍀")


# ---------------- Database layer (Postgres on Neon, SQLite fallback) ----------------

USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
_pg_pool = None
_sqlite_db_path = os.getenv("SQLITE_PATH", "bot.db")


def _pg_dsn():
    import urllib.parse as _up

    dsn = DATABASE_URL.strip()
    # asyncpg wants postgresql:// ; Neon gives postgresql:// already. Render ok.
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    # Neon appends channel_binding=require which asyncpg doesn't understand -> strip it
    try:
        parts = _up.urlsplit(dsn)
        q = _up.parse_qsl(parts.query, keep_blank_values=True)
        q = [(k, v) for k, v in q if k.lower() != "channel_binding"]
        dsn = _up.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, _up.urlencode(q), parts.fragment)
        )
    except Exception:
        pass
    return dsn


def _pg_pool_kwargs():
    kw = {}
    # Neon requires SSL
    if "sslmode=require" in DATABASE_URL.lower():
        kw["ssl"] = "require"
    # Neon pooled connections (port 6543 / -pooler host) go through pgbouncer:
    # asyncpg needs statement_cache_size=0 there
    if "-pooler" in DATABASE_URL or ":6543" in DATABASE_URL:
        kw["statement_cache_size"] = 0
    return kw


async def db_init():
    global _pg_pool
    if USE_POSTGRES:
        import asyncpg

        _pg_pool = await asyncpg.create_pool(
            _pg_dsn(), min_size=1, max_size=10, **_pg_pool_kwargs()
        )
        async with _pg_pool.acquire() as c:
            await c.execute(
                """CREATE TABLE IF NOT EXISTS users(
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT, username TEXT,
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_start TIMESTAMPTZ DEFAULT NOW())"""
            )
            await c.execute(
                """CREATE TABLE IF NOT EXISTS app_opens(
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    opened_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            await c.execute(
                """CREATE TABLE IF NOT EXISTS admins(
                    user_id BIGINT PRIMARY KEY,
                    added_by BIGINT,
                    added_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            await c.execute(
                """CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY, value TEXT)"""
            )
            for k, v in [
                ("welcome_text", DEFAULT_WELCOME_TEXT),
                ("welcome_button_text", DEFAULT_WELCOME_BUTTON),
                ("bc_button_text", DEFAULT_BC_BUTTON),
                ("mini_app_url", MINI_APP_URL_DEFAULT),
            ]:
                await c.execute(
                    "INSERT INTO settings(key,value) VALUES($1,$2) "
                    "ON CONFLICT (key) DO NOTHING",
                    k, v,
                )
        log.info("Postgres (Neon) connected.")
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
                    first_seen TEXT, last_start TEXT)"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS app_opens(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER, opened_at TEXT)"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS admins(
                    user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT)"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY, value TEXT)"""
            )
            for k, v in [
                ("welcome_text", DEFAULT_WELCOME_TEXT),
                ("welcome_button_text", DEFAULT_WELCOME_BUTTON),
                ("bc_button_text", DEFAULT_BC_BUTTON),
                ("mini_app_url", MINI_APP_URL_DEFAULT),
            ]:
                await db.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v)
                )
            await db.commit()
        log.info("SQLite connected (no DATABASE_URL).")


def _now():
    return datetime.now(timezone.utc)


async def db_get_setting(key: str, default: str = "") -> str:
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow("SELECT value FROM settings WHERE key=$1", key)
            return row["value"] if row and row["value"] else default
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row and row[0] else default


async def db_set_setting(key: str, value: str):
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            await c.execute(
                """INSERT INTO settings(key,value) VALUES($1,$2)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                key, value,
            )
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()


async def db_upsert_user(user_id: int, first_name: str, username: str):
    now = _now()
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            await c.execute(
                """INSERT INTO users(user_id,first_name,username,first_seen,last_start)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (user_id) DO UPDATE SET
                     first_name=EXCLUDED.first_name, username=EXCLUDED.username,
                     last_start=EXCLUDED.last_start""",
                user_id, first_name, username, now, now,
            )
    else:
        import aiosqlite

        iso = now.isoformat()
        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute(
                """INSERT INTO users(user_id,first_name,username,first_seen,last_start)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     first_name=excluded.first_name, username=excluded.username,
                     last_start=excluded.last_start""",
                (user_id, first_name, username, iso, iso),
            )
            await db.commit()


async def db_log_open(user_id: int):
    now = _now()
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO app_opens(user_id, opened_at) VALUES($1,$2)",
                user_id, now,
            )
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute(
                "INSERT INTO app_opens(user_id, opened_at) VALUES(?,?)",
                (user_id, now.isoformat()),
            )
            await db.commit()


async def db_delete_user(user_id: int):
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM users WHERE user_id=$1", user_id)
            await c.execute("DELETE FROM app_opens WHERE user_id=$1", user_id)
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM app_opens WHERE user_id=?", (user_id,))
            await db.commit()


async def db_all_user_ids():
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT user_id FROM users")
            return [r["user_id"] for r in rows]
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute("SELECT user_id FROM users") as cur:
                return [r[0] for r in await cur.fetchall()]


async def db_stats():
    """Returns dict: total, today_new, today_active, week_new, month_new."""
    now = _now()
    day = now - timedelta(days=1)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            total = await c.fetchval("SELECT COUNT(*) FROM users")
            today_new = await c.fetchval(
                "SELECT COUNT(*) FROM users WHERE first_seen >= $1", day
            )
            today_active = await c.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_start >= $1", day
            )
            week_new = await c.fetchval(
                "SELECT COUNT(*) FROM users WHERE first_seen >= $1", week
            )
            month_new = await c.fetchval(
                "SELECT COUNT(*) FROM users WHERE first_seen >= $1", month
            )
            return dict(total=total, today_new=today_new,
                        today_active=today_active, week_new=week_new,
                        month_new=month_new)
    else:
        import aiosqlite

        def _c(rows, idx, since):
            return sum(1 for r in rows if r[idx] and r[idx] >= since.isoformat())

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute("SELECT first_seen,last_start FROM users") as cur:
                rows = await cur.fetchall()
                return dict(
                    total=len(rows),
                    today_new=_c(rows, 0, day),
                    today_active=_c(rows, 1, day),
                    week_new=_c(rows, 0, week),
                    month_new=_c(rows, 0, month),
                )


async def db_daystats():
    """Mini-app opens in last 24h: total events + unique users."""
    day = _now() - timedelta(hours=24)
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            total = await c.fetchval(
                "SELECT COUNT(*) FROM app_opens WHERE opened_at >= $1", day
            )
            uniq = await c.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM app_opens WHERE opened_at >= $1",
                day,
            )
            return dict(total=total or 0, unique=uniq or 0)
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute(
                "SELECT user_id FROM app_opens WHERE opened_at >= ?",
                (day.isoformat(),),
            ) as cur:
                rows = await cur.fetchall()
                return dict(total=len(rows), unique=len({r[0] for r in rows}))


async def db_is_admin(user_id: int) -> bool:
    if user_id in MASTER_ADMINS:
        return True
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            return await c.fetchval(
                "SELECT 1 FROM admins WHERE user_id=$1", user_id
            ) is not None
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute(
                "SELECT 1 FROM admins WHERE user_id=?", (user_id,)
            ) as cur:
                return await cur.fetchone() is not None


async def db_add_admin(user_id: int, added_by: int):
    now = _now()
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            await c.execute(
                """INSERT INTO admins(user_id,added_by,added_at) VALUES($1,$2,$3)
                   ON CONFLICT (user_id) DO NOTHING""",
                user_id, added_by, now,
            )
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
                (user_id, added_by, now.isoformat()),
            )
            await db.commit()


async def db_list_admins():
    if USE_POSTGRES:
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT user_id FROM admins ORDER BY user_id")
            return [r["user_id"] for r in rows]
    else:
        import aiosqlite

        async with aiosqlite.connect(_sqlite_db_path) as db:
            async with db.execute("SELECT user_id FROM admins ORDER BY user_id") as cur:
                return [r[0] for r in await cur.fetchall()]


# ---------------- Helpers ----------------

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not await db_is_admin(user.id):
            await update.effective_message.reply_text(
                "⛔ Admins only. Ask a master admin to run /addadmin with your ID.\n"
                f"Your ID: <code>{user.id if user else '?'}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        return await func(update, context)

    return wrapper


def welcome_keyboard(welcome_btn_text: str, mini_app_url: str):
    if mini_app_url and mini_app_url.startswith("https://"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text=welcome_btn_text,
                                   web_app=WebAppInfo(url=mini_app_url))]]
        )
    # Fallback: plain URL button (or no button if no URL configured)
    if mini_app_url:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text=welcome_btn_text, url=mini_app_url)]]
        )
    return None


def broadcast_keyboard(bc_btn_text: str, mini_app_url: str):
    return welcome_keyboard(bc_btn_text, mini_app_url)


def parse_broadcast_args(raw: str):
    """
    Supports:
      <message>
      <message> || <button_text>
      <message> || <button_text> || <url>
    Returns (message, button_text_or_None, url_or_None).
    """
    parts = [p.strip() for p in raw.split("||")]
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], parts[1], None
    return parts[0], parts[1], parts[2]


# ---------------- Command handlers ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db_upsert_user(user.id, user.first_name or "", user.username or "")

    welcome_text = await db_get_setting("welcome_text", DEFAULT_WELCOME_TEXT)
    welcome_btn = await db_get_setting("welcome_button_text", DEFAULT_WELCOME_BUTTON)
    mini_app_url = (await db_get_setting("mini_app_url", MINI_APP_URL_DEFAULT)).strip()

    try:
        text = welcome_text.format(name=user.first_name or "friend")
    except Exception:
        text = welcome_text.replace("{name}", user.first_name or "friend")

    kb = welcome_keyboard(welcome_btn, mini_app_url)
    if kb is None:
        text += "\n\n⚠️ <i>Mini App URL not set yet. Admin: /setminiapp https://...</i>"
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )


async def webapp_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs a Mini App open sent via Telegram.WebApp.sendData(...)."""
    user = update.effective_user
    if user:
        await db_upsert_user(user.id, user.first_name or "", user.username or "")
        await db_log_open(user.id)
    await update.effective_message.reply_text(
        "✅ Open logged! Good luck 🍀\nSend /start anytime to play again."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "<b>🤖 Commands</b>\n\n"
        "<b>User</b>\n"
        "/start — welcome + Play button\n"
        "/help — this list\n"
        "/examples — many examples\n\n"
        "<b>Admin only</b>\n"
        "/broadcast <code>msg [|| btn || url]</code> — send to all <b>with</b> button\n"
        "  ↳ or reply to any message with /broadcast\n"
        "/broadcastnobutton <code>msg</code> — send to all, text only\n"
        "/setbcbutton <code>text</code> — edit broadcast button 🎨\n"
        "/setwelcomebutton <code>text</code> — edit welcome button 🎨\n"
        "/setwelcometext <code>text</code> — edit welcome msg, use {name} 👤\n"
        "/setminiapp <code>https://...</code> — set Mini App URL 🔗\n"
        "/addadmin <code>telegram_id</code> — add admin 👑\n"
        "/listadmins — show admins\n"
        "/stats — daily / weekly / monthly users 📊\n"
        "/daystats — Mini App opens in last 24h 📈\n\n"
        "Emoji supported everywhere ✨\n"
        "See /examples for copy-paste variations.",
        parse_mode=ParseMode.HTML,
    )


async def examples_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "<b>📚 Examples</b>\n\n"
        "<b>/start</b>\n<code>/start</code>\n\n"
        "<b>Welcome text</b>\n"
        "<code>/setwelcometext Welcome {name} to Naija 🇳🇬🔥 Tap Play &amp; Win 10,000 naira 💰</code>\n"
        "<code>/setwelcometext Hello {name} 👋 Ready to win ₦10,000 today? 🍀 Tap below! 🎮</code>\n\n"
        "<b>Buttons</b>\n"
        "<code>/setwelcomebutton 🎮 Play &amp; Win 10,000 naira 💰</code>\n"
        "<code>/setwelcomebutton ▶️ Start Game 🍀</code>\n"
        "<code>/setbcbutton 🎮 Open Mini App ✨</code>\n"
        "<code>/setbcbutton 🔥 Claim ₦10,000 Now 💸</code>\n"
        "<code>/setminiapp https://your-mini-app.onrender.com</code>\n\n"
        "<b>Broadcast WITH button</b>\n"
        "<code>/broadcast 🔥 Weekend special! Win ₦10,000 💰🍀</code>\n"
        "<code>/broadcast Big draw tonight 🎉 || 🔥 Play Now 💸</code>\n"
        "<code>/broadcast Free entry 🆓🍀 || 🎮 Play || https://t.me/yourbot/app</code>\n"
        "Reply to a photo/video with <code>/broadcast</code> to resend it to everyone with the button.\n\n"
        "<b>Broadcast WITHOUT button</b>\n"
        "<code>/broadcastnobutton ⚠️ Maintenance at 9pm, game pauses 10 mins 🛠️</code>\n"
        "<code>/broadcastnobutton Winners announced 🎉 congrats Adaeze 🏆💰</code>\n\n"
        "<b>Admins</b>\n"
        "<code>/addadmin 123456789</code>\n"
        "<code>/listadmins</code>\n\n"
        "<b>Stats</b>\n<code>/stats</code> → new today / active today / 7-day / 30-day / total\n"
        "<code>/daystats</code> → Mini App opens in last 24h",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def set_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.text.partition(" ")[2].strip()
    if not raw:
        cur = await db_get_setting("welcome_text", DEFAULT_WELCOME_TEXT)
        await update.effective_message.reply_text(
            f"Current welcome text:\n{cur}\n\nUsage:\n<code>/setwelcometext Welcome {{name}} to Naija 🇳🇬 ...</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await db_set_setting("welcome_text", raw)
    await update.effective_message.reply_text(f"✅ Welcome text saved:\n{raw}")


@admin_only
async def set_welcome_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.text.partition(" ")[2].strip()
    if not raw:
        cur = await db_get_setting("welcome_button_text", DEFAULT_WELCOME_BUTTON)
        await update.effective_message.reply_text(
            f"Current: {cur}\nUsage: <code>/setwelcomebutton 🎮 Play &amp; Win 10,000 naira</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await db_set_setting("welcome_button_text", raw)
    await update.effective_message.reply_text(f"✅ Welcome button saved: {raw}")


@admin_only
async def set_bc_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.text.partition(" ")[2].strip()
    if not raw:
        cur = await db_get_setting("bc_button_text", DEFAULT_BC_BUTTON)
        await update.effective_message.reply_text(
            f"Current: {cur}\nUsage: <code>/setbcbutton 🎮 Open Mini App ✨</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await db_set_setting("bc_button_text", raw)
    await update.effective_message.reply_text(f"✅ Broadcast button saved: {raw}")


@admin_only
async def set_miniapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.text.partition(" ")[2].strip()
    if not raw or not raw.startswith("https://"):
        await update.effective_message.reply_text(
            "Usage: <code>/setminiapp https://your-mini-app-url</code>\n"
            "Must start with https:// (Telegram WebApp requirement).",
            parse_mode=ParseMode.HTML,
        )
        return
    await db_set_setting("mini_app_url", raw)
    await update.effective_message.reply_text(f"✅ Mini App URL saved:\n{raw}")


@admin_only
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.effective_message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/addadmin 123456789</code>\n"
            "Tip: user can get their ID from @userinfobot.",
            parse_mode=ParseMode.HTML,
        )
        return
    new_id = int(parts[1])
    await db_add_admin(new_id, update.effective_user.id)
    await update.effective_message.reply_text(f"✅ Admin added: <code>{new_id}</code>",
                                              parse_mode=ParseMode.HTML)


@admin_only
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_admins = await db_list_admins()
    lines = ["<b>👑 Admins</b>"]
    lines.append("Masters: " + (", ".join(f"<code>{m}</code>" for m in sorted(MASTER_ADMINS)) or "—"))
    lines.append("Added: " + (", ".join(f"<code>{a}</code>" for a in db_admins) or "—"))
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await db_stats()
    await update.effective_message.reply_text(
        "📊 <b>Users</b>\n"
        f"🆕 New today (/start first time): <b>{s['today_new']}</b>\n"
        f"👆 Active today (pressed /start): <b>{s['today_active']}</b>\n"
        f"📅 New last 7 days: <b>{s['week_new']}</b>\n"
        f"🗓️ New last 30 days: <b>{s['month_new']}</b>\n"
        f"👥 Total: <b>{s['total']}</b>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def daystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = await db_daystats()
    await update.effective_message.reply_text(
        "📈 <b>Mini App opens — last 24h</b>\n"
        f"👥 Unique users who opened: <b>{d['unique']}</b>\n"
        f"🔁 Total opens: <b>{d['total']}</b>\n\n"
        "<i>Includes returning users. Frontend must call "
        "Telegram.WebApp.sendData() or POST /api/open so opens get logged.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------------- Broadcast engine ----------------

async def _send_to_user(bot, uid: int, with_button: bool,
                        text: str | None, reply_msg=None,
                        btn_text: str = "", btn_url: str = ""):
    """Returns 'ok' | 'blocked' | 'fail'. Cleared-chat users still get messages (normal send works)."""
    kb = broadcast_keyboard(btn_text, btn_url) if with_button and btn_url else None
    try:
        if reply_msg is not None:
            # copy preserves photo/video/caption; attach button if requested
            await bot.copy_message(
                chat_id=uid, from_chat_id=reply_msg.chat_id,
                message_id=reply_msg.message_id,
                caption=(text or reply_msg.caption_html or reply_msg.caption),
                parse_mode=ParseMode.HTML if (text or reply_msg.caption) else None,
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                chat_id=uid, text=text, parse_mode=ParseMode.HTML,
                reply_markup=kb, disable_web_page_preview=True,
            )
        return "ok"
    except Forbidden:
        # User blocked the bot -> remove from DB (per spec)
        await db_delete_user(uid)
        return "blocked"
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            if reply_msg is not None:
                await bot.copy_message(chat_id=uid, from_chat_id=reply_msg.chat_id,
                                       message_id=reply_msg.message_id, reply_markup=kb)
            else:
                await bot.send_message(chat_id=uid, text=text,
                                       parse_mode=ParseMode.HTML, reply_markup=kb)
            return "ok"
        except Forbidden:
            await db_delete_user(uid)
            return "blocked"
        except Exception:
            return "fail"
    except (TimedOut, NetworkError):
        await asyncio.sleep(1)
        try:
            await bot.send_message(chat_id=uid, text=text or "📢 Update",
                                   parse_mode=ParseMode.HTML, reply_markup=kb)
            return "ok"
        except Exception:
            return "fail"
    except Exception as e:
        log.warning("send to %s failed: %s", uid, e)
        return "fail"


async def _run_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         with_button: bool):
    msg = update.effective_message
    reply_src = msg.reply_to_message
    raw = msg.text.partition(" ")[2].strip() if msg.text else ""

    if not raw and reply_src is None:
        name = "/broadcast" if with_button else "/broadcastnobutton"
        await msg.reply_text(
            f"Usage:\n<code>{name} Your message here 🎉</code>\n"
            f"<code>{name} Text || Button 🔥 || https://...</code>\n"
            f"Or reply to any photo/video/text with <code>{name}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    custom_text, custom_btn, custom_url = parse_broadcast_args(raw) if raw else (None, None, None)

    saved_btn = await db_get_setting("bc_button_text", DEFAULT_BC_BUTTON)
    saved_url = (await db_get_setting("mini_app_url", MINI_APP_URL_DEFAULT)).strip()
    btn_text = (custom_btn or saved_btn).strip()
    btn_url = (custom_url or saved_url).strip()

    uids = await db_all_user_ids()
    if not uids:
        await msg.reply_text("No users yet. Ask someone to press /start first.")
        return

    status = await msg.reply_text(f"📤 Broadcasting to {len(uids)} users…")
    ok = blocked = fail = 0
    sem = asyncio.Semaphore(25)

    async def worker(uid):
        nonlocal ok, blocked, fail
        async with sem:
            r = await _send_to_user(context.bot, uid, with_button,
                                    custom_text, reply_src, btn_text, btn_url)
            if r == "ok":
                ok += 1
            elif r == "blocked":
                blocked += 1
            else:
                fail += 1

    await asyncio.gather(*(worker(u) for u in uids))
    await status.edit_text(
        f"✅ Done.\nSent: {ok}\nBlocked &amp; removed: {blocked}\nFailed: {fail}"
        + ("" if with_button else "\n(mode: no button)"),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_broadcast(update, context, with_button=True)


@admin_only
async def broadcast_nobutton_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_broadcast(update, context, with_button=False)


# ---------------- App bootstrap ----------------

async def post_init(app: Application):
    await db_init()
    log.info("Master admins: %s", sorted(MASTER_ADMINS) or "none set")


def _start_health_server():
    """Tiny stdlib HTTP server so Render Web Services see an open port.

    Render sets $PORT (defaults to 10000). Serves 200 OK on / and /health.
    Runs in a daemon thread; polling continues in the main thread.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.getenv("PORT", "10000").strip() or 10000)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            return  # keep Render logs clean

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        log.info("Health server listening on port %s (/health)", port)
    except Exception as e:
        log.warning("Health server failed to start on port %s: %s", port, e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN. Copy .env.example to .env and fill it.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("examples", examples_cmd))
    app.add_handler(CommandHandler("setwelcometext", set_welcome_text))
    app.add_handler(CommandHandler("setwelcomebutton", set_welcome_button))
    app.add_handler(CommandHandler("setbcbutton", set_bc_button))
    app.add_handler(CommandHandler("setminiapp", set_miniapp))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("listadmins", list_admins))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("daystats", daystats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("broadcastnobutton", broadcast_nobutton_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data_handler))

    log.info("Bot starting (polling)…")
    _start_health_server()  # no-op locally, required on Render Web Service
    # Python 3.14+ removed implicit event-loop creation, which PTB v21's
    # run_polling() still relies on (asyncio.get_event_loop). Create + set
    # one explicitly so polling works on 3.11 -> 3.14.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
