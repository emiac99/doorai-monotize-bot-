# main.py  (webhook / Render)
import os
import logging
import sqlite3
import requests
import hashlib
import asyncio
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------- CONFIG (env vars) ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")            # required
ADMIN_ID = int(os.getenv("ADMIN_ID", "8001485394"))
DOMAIN = os.getenv("DOMAIN", "")                        # set to your render domain
SHRINK_API_KEY = os.getenv("SHRINK_API_KEY", "")        # optional
REFERRAL_QUALIFY_CLICKS = int(os.getenv("REFERRAL_QUALIFY_CLICKS", "20"))
REQUIRED_CLICKS_PER_DAY = int(os.getenv("REQUIRED_CLICKS_PER_DAY", "20"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")            # e.g. DooraiZapBot
DB_PATH = os.getenv("DB_PATH", "bot.db")
LAST_RESET_FILE = os.getenv("LAST_RESET_FILE", "last_reset.txt")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is required in environment")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# ---------- DB helpers ----------
def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = db_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        referred_by INTEGER,
        clicks INTEGER DEFAULT 0,
        paid_link TEXT,
        qualified_referrals INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER,
        referee INTEGER UNIQUE,
        qualified INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        click_date TEXT,
        ip_hash TEXT,
        ua_hash TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)""")
    conn.commit(); conn.close()

init_db()

# ---------- Utilities ----------
def today_str():
    return date.today().isoformat()

def fingerprint(ip, ua):
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()

def get_setting(key):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT v FROM settings WHERE k=?", (key,))
    r = c.fetchone(); conn.close()
    return r[0] if r else None

def set_setting(key, val):
    conn = db_conn(); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, val))
    conn.commit(); conn.close()

# ---------- ShrinkMe integration ----------
def shrinkme_shorten(long_url):
    if not SHRINK_API_KEY:
        return None
    try:
        api = f"https://shrinkme.io/api?api={SHRINK_API_KEY}&url={long_url}"
        r = requests.get(api, timeout=10)
        data = r.json()
        if isinstance(data, dict):
            return data.get("shortenedUrl") or data.get("shortUrl") or data.get("shortenedurl")
        if isinstance(data, str) and data.startswith("http"):
            return data
    except Exception:
        logger.exception("ShrinkMe shorten failed")
    return None

# ---------- Users / referrals / clicks ----------
def ensure_user(user_id, username=None, referred_by=None):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users(user_id, username, referred_by, clicks, paid_link, qualified_referrals, created_at) VALUES(?,?,?,?,?,?,?)",
                  (user_id, username or "", referred_by, 0, None, 0, datetime.utcnow().isoformat()))
        if referred_by:
            try:
                c.execute("INSERT INTO referrals(referrer, referee, qualified, created_at) VALUES(?,?,0,?)",
                          (referred_by, user_id, datetime.utcnow().isoformat()))
            except:
                pass
        conn.commit()
    else:
        if username:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id)); conn.commit()
    conn.close()

def count_user_total(user_id):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM clicks WHERE user_id=?", (user_id,))
    r = c.fetchone()[0]; conn.close(); return r

def count_user_today(user_id):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM clicks WHERE user_id=? AND click_date=?", (user_id, today_str()))
    r = c.fetchone()[0]; conn.close(); return r

def record_click_via_redirect(user_id, ip_hash, ua_hash):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM clicks WHERE user_id=? AND click_date=? AND ip_hash=?", (user_id, today_str(), ip_hash))
    if c.fetchone():
        conn.close(); return False
    c.execute("INSERT INTO clicks(user_id, click_date, ip_hash, ua_hash, created_at) VALUES(?,?,?,?,?)",
              (user_id, today_str(), ip_hash, ua_hash, datetime.utcnow().isoformat()))
    c.execute("UPDATE users SET clicks = clicks + 1 WHERE user_id=?", (user_id,))
    conn.commit()

    c.execute("SELECT referrer, qualified FROM referrals WHERE referee=?", (user_id,))
    row = c.fetchone()
    if row:
        referrer, qualified = row
        if not qualified:
            c.execute("SELECT COUNT(*) FROM clicks WHERE user_id=?", (user_id,))
            total = c.fetchone()[0]
            if total >= REFERRAL_QUALIFY_CLICKS:
                c.execute("UPDATE referrals SET qualified=1 WHERE referee=?", (user_id,))
                c.execute("UPDATE users SET qualified_referrals = qualified_referrals + 1 WHERE user_id=?", (referrer,))
                conn.commit()
    conn.close()
    return True

# ---------- Daily reset ----------
def daily_reset():
    conn = db_conn(); c = conn.cursor()
    c.execute("UPDATE users SET clicks = 0")
    conn.commit(); conn.close()
    logger.info("Daily reset done")

def check_and_do_daily_reset():
    today = today_str()
    if not os.path.exists(LAST_RESET_FILE):
        with open(LAST_RESET_FILE, "w") as f: f.write(today); return
    with open(LAST_RESET_FILE, "r") as f: last = f.read().strip()
    if last != today:
        daily_reset()
        with open(LAST_RESET_FILE, "w") as f: f.write(today)

# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args; payload = args[0] if args else None
    referred_by = None
    if payload:
        try:
            if payload.startswith("ref_"):
                referred_by = int(payload.split("_",1)[1])
            else:
                referred_by = int(payload)
        except:
            referred_by = None
    user = update.effective_user
    ensure_user(user.id, user.username, referred_by)
    kb = [
        [InlineKeyboardButton("🔗 Get Link", callback_data="getlink")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats"), InlineKeyboardButton("🧾 My Referrals", callback_data="myreferrals")],
        [InlineKeyboardButton("🏁 Progress (today)", callback_data="progress")]
    ]
    if user.id == ADMIN_ID:
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    await update.message.reply_text("Welcome — use buttons below.", reply_markup=InlineKeyboardMarkup(kb))

async def setshrink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("Only admin.")
    if not context.args:
        return await update.message.reply_text("Usage: /setshrinkme <url>")
    set_setting("shrinkme", context.args[0].strip())
    await update.message.reply_text("Saved.")

# full callback logic (getlink, stats, myreferrals, progress, admin panel, etc.)
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    check_and_do_daily_reset()

    # GET LINK
    if q.data == "getlink":
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT paid_link FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        paid = row[0] if row else None
        if not paid:
            raw = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else f"https://t.me/?start=ref_{uid}"
            paid = shrinkme_shorten(raw) or None
            if paid:
                c.execute("UPDATE users SET paid_link=? WHERE user_id=?", (paid, uid)); conn.commit()
        conn.close()
        if not paid:
            await q.edit_message_text("❌ Unable to create ShrinkMe link. Check SHRINK_API_KEY in environment.")
            return
        await q.edit_message_text(f"🔗 Your monetized link:\n{paid}\nShare it to earn clicks.", parse_mode="Markdown")

    # STATS
    elif q.data == "stats":
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT clicks, qualified_referrals FROM users WHERE user_id=?", (uid,))
        r = c.fetchone(); conn.close()
        clicks = r[0] if r else 0; qref = r[1] if r else 0
        await q.edit_message_text(f"📊 Your Stats:\nClicks (today): {clicks}\nQualified referrals: {qref}\nTarget: {REFERRAL_QUALIFY_CLICKS}", parse_mode="Markdown")

    # MY REFERRALS
    elif q.data == "myreferrals":
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT referee, qualified, created_at FROM referrals WHERE referrer=? ORDER BY created_at DESC", (uid,))
        rows = c.fetchall(); conn.close()
        if not rows:
            await q.edit_message_text("You have no referrals yet.")
            return
        lines = []
        for r in rows[:200]:
            referee, qualified, created = r
            lines.append(f"{referee} — {'Qualified' if qualified else 'Pending'} — {created[:19]}")
        await q.edit_message_text("Your referrals:\n" + "\n".join(lines))

    # PROGRESS (today)
    elif q.data == "progress":
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM clicks WHERE user_id=? AND click_date=?", (uid, today_str()))
        today_count = c.fetchone()[0]; conn.close()
        remaining = max(0, REFERRAL_QUALIFY_CLICKS - today_count)
        await q.edit_message_text(f"Today's clicks: {today_count}/{REFERRAL_QUALIFY_CLICKS}. {remaining} to go today.")

    # ADMIN PANEL
    elif q.data == "admin_panel":
        if uid != ADMIN_ID:
            await q.edit_message_text("Only admin."); return
        kb = [
            [InlineKeyboardButton("📋 All Users", callback_data="admin_all_users")],
            [InlineKeyboardButton("🏆 Qualified Today", callback_data="admin_qualified_today")],
            [InlineKeyboardButton("🧾 Referral Details", callback_data="admin_ref_details")]
        ]
        await q.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data == "admin_all_users":
        if uid != ADMIN_ID: await q.edit_message_text("Only admin."); return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT user_id, clicks, qualified_referrals FROM users ORDER BY created_at DESC LIMIT 200")
        rows = c.fetchall(); conn.close()
        text = "All users (latest 200):\n"
        for r in rows: text += f"{r[0]} — clicks:{r[1]} — qref:{r[2]}\n"
        await q.edit_message_text(text)

    elif q.data == "admin_qualified_today":
        if uid != ADMIN_ID: await q.edit_message_text("Only admin."); return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT user_id, COUNT(*) as clicks FROM clicks WHERE click_date=? GROUP BY user_id HAVING clicks>=?", (today_str(), REFERRAL_QUALIFY_CLICKS))
        rows = c.fetchall(); conn.close()
        if not rows: await q.edit_message_text("No qualified users today."); return
        text = "Qualified today:\n"
        for r in rows: text += f"{r[0]} — {r[1]} clicks\n"
        await q.edit_message_text(text)

    elif q.data == "admin_ref_details":
        if uid != ADMIN_ID: await q.edit_message_text("Only admin."); return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT referrer, referee, qualified, created_at FROM referrals ORDER BY created_at DESC LIMIT 500")
        rows = c.fetchall(); conn.close()
        text = "Referrals (latest):\n"
        for r in rows[:200]: text += f"{r[0]} -> {r[1]} — {'Q' if r[2] else 'P'} — {r[3][:16]}\n"
        await q.edit_message_text(text)

# ---------- Redirect endpoint ----------
@app.route("/r")
def redirect_endpoint():
    user_param = request.args.get("u")
    if not user_param or not user_param.isdigit(): abort(400, "missing user")
    uid = int(user_param)
    check_and_do_daily_reset()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "0.0.0.0"
    ua = (request.headers.get("User-Agent") or "")[:500]
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    ua_hash = hashlib.sha256(ua.encode()).hexdigest()
    ensure_user(uid)
    record_click_via_redirect(uid, ip_hash, ua_hash)
    shrinkme = get_setting("shrinkme")
    if shrinkme:
        return ("", 302, {"Location": shrinkme})
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT paid_link FROM users WHERE user_id=?", (uid,)); r = c.fetchone(); conn.close()
    if r and r[0]:
        return ("", 302, {"Location": r[0]})
    abort(500, "no target")

# ---------- Webhook endpoint ----------
@app.post("/webhook")
def webhook():
    check_and_do_daily_reset()
    data = request.get_json(force=True)
    application.update_queue.put_nowait(Update.de_json(data, application.bot))
    return "OK", 200

# ---------- Register handlers ----------
def register_handlers():
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("setshrinkme", setshrink_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))

register_handlers()

# ---------- Startup for gunicorn environment ----------
async def _startup():
    await application.initialize()
    await application.start()
    logger.info("Telegram Application started")

# schedule startup task when module is imported by gunicorn
asyncio.get_event_loop().create_task(_startup())
