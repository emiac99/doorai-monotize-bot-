# main.py
import os
import logging
import sqlite3
import requests
import hashlib
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)

# --------- CONFIG (env vars; defaults provided) ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")                     # <-- set this on Render (your bot token)
ADMIN_IDS = int(os.getenv("ADMIN_IDS", "8001485394"))    # you gave 8001485394
DOMAIN = os.getenv("DOMAIN", "https://doorai-monotize-bot.onrender.com")
SHRINK_LINK = os.getenv("SHRINK_LINK", "")       # set your ShrinkMe API key in Render
REFERRAL_CLICKS_PER_DAY = int(os.getenv("REFERRAL_CLICKS_PER_DAY", "20"))
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "5_UTC"))  # not strictly used; reset checks date change

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

DB_PATH = os.getenv("DB_PATH", "bot.db")
LAST_RESET_FILE = os.getenv("LAST_RESET_FILE", "last_reset.txt")

# --------- DATABASE helpers ----------
def db_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db_conn()
    c = conn.cursor()
    # users: main table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        referred_by INTEGER,
        clicks INTEGER DEFAULT 0,
        paid_link TEXT,
        qualified_referrals INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)
    # referrals: explicit mapping (referrer -> referee)
    c.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER,
        referee INTEGER UNIQUE,
        qualified INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)
    # clicks history (optional for audits)
    c.execute("""
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        click_date TEXT,
        ip_hash TEXT,
        ua_hash TEXT,
        created_at TEXT
    )
    """)
    # settings
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# --------- Utility functions ----------
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

# --------- ShrinkMe integration ----------
def shrinkme_shorten(long_url):
    """Return ShrinkMe monetized link or None."""
    if not SHRINK_API_KEY:
        return None
    try:
        api = f"https://shrinkme.io/api?api={SHRINK_API_KEY}&url={long_url}"
        r = requests.get(api, timeout=10)
        data = r.json()
        # ShrinkMe returns shortenedUrl in JSON for some API versions
        if isinstance(data, dict):
            return data.get("shortenedUrl") or data.get("shortUrl") or data.get("shortenedurl")
        # fallback: if API returns string
        if isinstance(data, str) and data.startswith("http"):
            return data
    except Exception as e:
        logging.exception("ShrinkMe shorten failed")
    return None

# --------- Users / Referrals / Clicks ----------
def ensure_user(user_id, username=None, referred_by=None):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users(user_id, username, referred_by, clicks, paid_link, qualified_referrals, created_at) VALUES(?,?,?,?,?,?,?)",
                  (user_id, username or "", referred_by, 0, None, 0, datetime.utcnow().isoformat()))
        # If referred_by provided, add referral mapping (if not exists)
        if referred_by:
            try:
                c.execute("INSERT INTO referrals(referrer, referee, qualified, created_at) VALUES(?,?,0,?)", (referred_by, user_id, datetime.utcnow().isoformat()))
            except:
                pass
        conn.commit()
    else:
        # update username if changed
        if username:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id)); conn.commit()
    conn.close()

def get_user(user_id):
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, referred_by, clicks, paid_link, qualified_referrals FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone(); conn.close()
    return r

def record_click_via_redirect(user_id, ip_hash, ua_hash):
    """
    Records click event (via redirect) and checks referral qualification.
    Returns True if recorded (unique), False if duplicate fingerprint same day.
    """
    conn = db_conn(); c = conn.cursor()
    # duplicate check: same user, same ip_hash AND same day => skip
    c.execute("SELECT 1 FROM clicks WHERE user_id=? AND click_date=? AND ip_hash=?", (user_id, today_str(), ip_hash))
    if c.fetchone():
        conn.close(); return False

    c.execute("INSERT INTO clicks(user_id, click_date, ip_hash, ua_hash, created_at) VALUES(?,?,?,?,?)",
              (user_id, today_str(), ip_hash, ua_hash, datetime.utcnow().isoformat()))
    # increment total clicks in users table
    c.execute("UPDATE users SET clicks = clicks + 1 WHERE user_id=?", (user_id,))
    conn.commit()

    # check referral qualification
    c.execute("SELECT referrer, qualified FROM referrals WHERE referee=?", (user_id,))
    row = c.fetchone()
    if row:
        referrer, qualified = row
        if not qualified:
            # compute total clicks for referee (ALL time)
            c.execute("SELECT COUNT(*) FROM clicks WHERE user_id=?", (user_id,))
            total = c.fetchone()[0]
            if total >= REFERRAL_CLICKS_PER_DAY:
                # mark referral qualified and increment referrer's qualified_referrals
                c.execute("UPDATE referrals SET qualified=1 WHERE referee=?", (user_id,))
                c.execute("UPDATE users SET qualified_referrals = qualified_referrals + 1 WHERE user_id=?", (referrer,))
                conn.commit()
    conn.close()
    return True

# --------- Daily reset system ----------
def daily_reset():
    conn = db_conn(); c = conn.cursor()
    # We keep click history (clicks table) for record; we only reset per-user aggregated clicks
    c.execute("UPDATE users SET clicks = 0")
    conn.commit(); conn.close()
    logging.info("Daily reset performed: all user clicks set to 0")

def check_and_do_daily_reset():
    today = today_str()
    if not os.path.exists(LAST_RESET_FILE):
        with open(LAST_RESET_FILE, "w") as f:
            f.write(today)
        return
    with open(LAST_RESET_FILE, "r") as f:
        last = f.read().strip()
    if last != today:
        # perform reset (this preserves clicks history in clicks table)
        daily_reset()
        with open(LAST_RESET_FILE, "w") as f:
            f.write(today)

# --------- Telegram command handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # detect start payload for referral like: /start ref_123456 or /start 123456
    args = context.args
    payload = args[0] if args else None
    referred_by = None
    if payload:
        # accept numeric or 'ref_<id>'
        try:
            if payload.startswith("ref_"):
                referred_by = int(payload.split("_",1)[1])
            else:
                referred_by = int(payload)
        except:
            referred_by = None

    user = update.effective_user
    ensure_user(user.id, user.username, referred_by)

    # Menu buttons
    kb = [
        [InlineKeyboardButton("🔗 Get Link", callback_data="getlink")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats"), InlineKeyboardButton("🧾 My Referrals", callback_data="myreferrals")],
        [InlineKeyboardButton("🏁 Progress (today)", callback_data="progress")]
    ]
    # admin quick button
    if user.id == ADMIN_ID:
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await update.message.reply_text("Welcome! Tap buttons below to get your monetized link, view progress, or see referrals.", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_setshrinkme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_IDS:
        return await update.message.reply_text("Only admin can set ShrinkMe link.")
    if not context.args:
        return await update.message.reply_text("Usage: /setshrinkme <shrinkme_url>")
    url = context.args[0].strip()
    set_setting("shrinkme", url)
    await update.message.reply_text("ShrinkMe link saved.")

async def cmd_qualified_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_IDS:
        return await update.message.reply_text("Only admin.")
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT user_id, COUNT(*) as clicks FROM clicks WHERE click_date=? GROUP BY user_id HAVING clicks>=?", (today_str(), REFERRAL_QUALIFY_CLICKS))
    rows = c.fetchall(); conn.close()
    if not rows:
        return await update.message.reply_text("No qualified users today.")
    lines = [f"{i+1}. {r[0]} — {r[1]} clicks" for i,r in enumerate(rows)]
    await update.message.reply_text("Qualified today:\n" + "\n".join(lines))

async def cmd_referral_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_IDS:
        return await update.message.reply_text("Only admin.")
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT user_id, qualified_referrals FROM users ORDER BY qualified_referrals DESC LIMIT 20")
    rows = c.fetchall(); conn.close()
    if not rows:
        return await update.message.reply_text("No referral data yet.")
    lines = [f"{i+1}. {r[0]} — {r[1]} qualified" for i,r in enumerate(rows)]
    await update.message.reply_text("Top referrers:\n" + "\n".join(lines))

# --------- Callback (button) handler ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # always check and possibly reset daily
    check_and_do_daily_reset()

    # GET LINK
    if q.data == "getlink":
        # return cached paid link or create new one
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT paid_link FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        paid = row[0] if row else None
        if not paid:
            # generate raw link that contains the referral (so new users who click will be marked as referred)
            raw = f"https://t.me/{os.getenv('BOT_USERNAME', '')}?start=ref_{uid}" if os.getenv('BOT_USERNAME') else f"https://t.me/?start=ref_{uid}"
            paid = shrinkme_shorten(raw) or None
            if paid:
                c.execute("UPDATE users SET paid_link=? WHERE user_id=?", (paid, uid))
                conn.commit()
        conn.close()
        if not paid:
            await q.edit_message_text("❌ Unable to create ShrinkMe link. Check SHRINK_API_KEY in environment.")
            return
        await q.edit_message_text(f"🔗 *Your Monetized Link:*\n{paid}\n\nShare it and earn clicks!\n(Referrals count when they reach {REFERRAL_QUALIFY_CLICKS} total clicks)", parse_mode="Markdown")

    # STATS
    elif q.data == "stats":
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT clicks, qualified_referrals FROM users WHERE user_id=?", (uid,))
        r = c.fetchone(); conn.close()
        clicks = r[0] if r else 0
        qref = r[1] if r else 0
        await q.edit_message_text(f"📊 *Your Stats:*\n\nClicks (today): {clicks}\nQualified referrals: {qref}\nTarget: {REFERRAL_QUALIFY_CLICKS}\n", parse_mode="Markdown")

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
        today_count = c.fetchone()[0]
        conn.close()
        remaining = max(0, REFERRAL_CLICKS_PER_DAY - today_count)
        await q.edit_message_text(f"Today's clicks: {today_count}/{REFERRAL_QUALIFY_CLICKS}. {remaining} to go today.")

    # ADMIN PANEL
    elif q.data == "admin_panel":
        if uid != ADMIN_ID:
            await q.edit_message_text("Only admin.")
            return
        kb = [
            [InlineKeyboardButton("📋 All Users", callback_data="admin_all_users")],
            [InlineKeyboardButton("🏆 Qualified Today", callback_data="admin_qualified_today")],
            [InlineKeyboardButton("🧾 Referral Details", callback_data="admin_ref_details")]
        ]
        await q.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(kb))

    # ADMIN: all users
    elif q.data == "admin_all_users":
        if uid != ADMIN_IDS:
            await q.edit_message_text("Only admin.")
            return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT user_id, clicks, qualified_referrals FROM users ORDER BY created_at DESC LIMIT 200")
        rows = c.fetchall(); conn.close()
        text = "All users (latest 200):\n"
        for r in rows:
            text += f"{r[0]} — clicks:{r[1]} — qref:{r[2]}\n"
        await q.edit_message_text(text)

    # ADMIN: qualified today
    elif q.data == "admin_qualified_today":
        if uid != ADMIN_IDS:
            await q.edit_message_text("Only admin.")
            return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT user_id, COUNT(*) as clicks FROM clicks WHERE click_date=? GROUP BY user_id HAVING clicks>=?", (today_str(), REFERRAL_QUALIFY_CLICKS))
        rows = c.fetchall(); conn.close()
        if not rows:
            await q.edit_message_text("No qualified users today.")
            return
        text = "Qualified today:\n"
        for r in rows:
            text += f"{r[0]} — {r[1]} clicks\n"
        await q.edit_message_text(text)

    # ADMIN: referral details
    elif q.data == "admin_ref_details":
        if uid != ADMIN_IDS:
            await q.edit_message_text("Only admin.")
            return
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT referrer, referee, qualified, created_at FROM referrals ORDER BY created_at DESC LIMIT 500")
        rows = c.fetchall(); conn.close()
        text = "Referrals (latest):\n"
        for r in rows[:200]:
            text += f"{r[0]} -> {r[1]} — {'Q' if r[2] else 'P'} — {r[3][:16]}\n"
        await q.edit_message_text(text)

# --------- Redirect endpoint to capture real clicks and forward to ShrinkMe URL ----------
@app.route("/r")
def redirect_endpoint():
    # expects query param: u=<tg_id>
    user_param = request.args.get("u")
    if not user_param or not user_param.isdigit():
        abort(400, "missing user")
    uid = int(user_param)
    check_and_do_daily_reset()  # ensure daily reset if needed

    # fingerprint
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "0.0.0.0"
    ua = (request.headers.get("User-Agent") or "")[:500]
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    ua_hash = hashlib.sha256(ua.encode()).hexdigest()

    ensure_user(uid)  # create user row if missing
    recorded = record_click_via_redirect(uid, ip_hash, ua_hash)

    # redirect to admin-set ShrinkMe link if present (for consistency)
    shrinkme = get_setting("shrinkme")
    if shrinkme:
        return ("", 302, {"Location": shrinkme})
    # if no admin-specified shrinkme, but user's own paid_link exists, use it
    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT paid_link FROM users WHERE user_id=?", (uid,))
    r = c.fetchone(); conn.close()
    if r and r[0]:
        return ("", 302, {"Location": r[0]})
    # fallback
    abort(500, "no target link set")

# --------- Webhook endpoint (Telegram) ----------
@app.post("/webhook")
def webhook():
    # Check reset on each incoming update
    check_and_do_daily_reset()

    data = request.get_json(force=True)
    application.update_queue.put_nowait(Update.de_json(data, application.bot))
    return "OK", 200

# --------- Register handlers and start ----------
def register_handlers():
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("setshrinkme", cmd_setshrinkme))
    application.add_handler(CommandHandler("qualified_today", cmd_qualified_today))
    application.add_handler(CommandHandler("referral_leaderboard", cmd_referral_leaderboard))

    application.add_handler(CallbackQueryHandler(callback_handler))

register_handlers()

if __name__ == "__main__":
    # Flask will be run by Render; for local testing:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
