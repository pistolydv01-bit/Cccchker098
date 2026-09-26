import os, sqlite3, threading, logging, time, hmac, hashlib, json, secrets
from datetime import datetime, timezone, timedelta
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import telebot
from telebot import types
import qrcode
from flask import Flask, request, redirect, url_for, render_template_string, session

# ============================================================
# CONFIG
# Only BOT_TOKEN is read from Render Environment Variables.
# Other basic settings stay in this file as requested.
# ============================================================

DB = "bot.db"

# Render ENV: BOT_TOKEN only
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Admin Telegram IDs
ADMIN_IDS = [7771292960, 6874667015]
ADMIN_ID = ADMIN_IDS[0]

# Basic settings (edit here if you want to change them)
DEFAULT_UPI = "flipkshop@axl"
ADMIN_SECRET = "change-this-admin-password"
FLASK_SECRET = "change-this-flask-secret"
PUBLIC_BASE_URL = ""
ADMIN_USERNAME = "PrashantYadav980"

# Razorpay credentials can be managed from the Telegram Admin Panel.
# Environment variables remain supported as a fallback for compatibility.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()

# Render service port
PORT = 10000
HEALTH_PORT = PORT

PAYMENT_TIMEOUT_MINUTES = 9

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# ============================================================
# SERVICE DOCUMENTS / PRICES
# ============================================================

DOCS = {
    "Jati / Aawas / Niwas": [
        "Aadhar Card", "Photo", "Mobile Number", "Gmail ID"
    ],
    "NCL": [
        "Aadhar Card", "Photo", "Mobile Number", "Gmail ID"
    ],
    "PMS": [
        "Jati Certificate", "Aay Certificate", "Aadhar Card",
        "10th Certificate", "Bonafide Certificate", "Fee Receipt", "Photo"
    ],
}

DEFAULTS = {
    "upi_id": DEFAULT_UPI,
    "jati_48h": "300",
    "jati_3d": "200",
    "jati_online": "120",
    "ncl": "50",
    "pms": "20",
    "payment_mode": "razorpay",  # legacy/preference compatibility
    "payment_expiry": str(PAYMENT_TIMEOUT_MINUTES),
    # Payment switches are controlled from the Telegram Admin Panel.
    "upi_enabled": "1",
    "razorpay_enabled": "1",
    "razorpay_key_id": "",
    "razorpay_key_secret": "",
    "razorpay_webhook_secret": "",
    "public_base_url": PUBLIC_BASE_URL,
}

sessions = {}
payment_sessions = {}
admin_input_sessions = {}
state_lock = threading.Lock()

# ============================================================
# DATABASE
# ============================================================

def db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        banned INTEGER DEFAULT 0,
        fancoin INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS applications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        category TEXT,
        plan TEXT,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        payment_method TEXT DEFAULT '',
        payment_ref TEXT DEFAULT '',
        payment_link TEXT DEFAULT '',
        created_at TEXT,
        paid_at TEXT
    );

    CREATE TABLE IF NOT EXISTS fancoin_tx(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        delta INTEGER,
        reason TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS broadcasts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        provider TEXT,
        provider_id TEXT,
        amount INTEGER,
        status TEXT,
        expires_at TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """)
    for k, v in DEFAULTS.items():
        c.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (k, v)
        )
    c.commit()
    c.close()

def setting(key):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else ""

def set_setting(key, value):
    c = db()
    c.execute("""
        INSERT INTO settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    c.commit()
    c.close()

def user_upsert(m):
    c = db()
    c.execute("""
        INSERT INTO users(user_id,username,created_at)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
    """, (m.from_user.id, m.from_user.username or "", now()))
    c.commit()
    c.close()

def get_fancoin(uid):
    c = db()
    r = c.execute("SELECT fancoin FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return int(r["fancoin"]) if r else 0

def change_fancoin(uid, delta, reason="Admin adjustment"):
    c = db()
    c.execute("""
        INSERT INTO users(user_id,fancoin,created_at)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET fancoin=fancoin+excluded.fancoin
    """, (uid, delta, now()))
    c.execute("""
        INSERT INTO fancoin_tx(user_id,delta,reason,created_at)
        VALUES(?,?,?,?)
    """, (uid, delta, reason, now()))
    c.commit()
    r = c.execute("SELECT fancoin FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return int(r["fancoin"])

def is_admin(uid):
    return int(uid) in ADMIN_IDS

def banned(uid):
    c = db()
    r = c.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return bool(r and r["banned"])

def price(category, key=None):
    if category == "NCL":
        return int(setting("ncl"))
    if category == "PMS":
        return int(setting("pms"))
    mapping = {"48h": "jati_48h", "3d": "jati_3d", "online": "jati_online"}
    return int(setting(mapping[key]))

# ============================================================
# UI HELPERS
# ============================================================

def main_menu():
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("📄 Jati / Aawas / Niwas", callback_data="cat|Jati / Aawas / Niwas"),
        types.InlineKeyboardButton("📄 NCL", callback_data="cat|NCL"),
        types.InlineKeyboardButton("🎓 PMS", callback_data="cat|PMS"),
        types.InlineKeyboardButton("🪙 Fancoin Balance", callback_data="coin|balance"),
    )
    return mk

def admin_menu():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add("📊 Bot Stats", "💰 Pricing / UPI")
    mk.add("💳 Payment Settings", "🪙 Fancoin")
    mk.add("📢 Broadcast", "📋 Applications")
    mk.add("🔐 Razorpay Settings", "❌ Close Admin")
    return mk

def admin_notify(text):
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text)
        except Exception as e:
            logging.warning("Admin notify failed for %s: %s", aid, e)

def admin_contact_markup():
    mk = types.InlineKeyboardMarkup()
    if ADMIN_USERNAME:
        mk.add(types.InlineKeyboardButton("💬 Admin से बात करें", url=f"https://t.me/{ADMIN_USERNAME}"))
    else:
        mk.add(types.InlineKeyboardButton("💬 Admin Chat", callback_data="admin|contact"))
    return mk

# ============================================================
# QR / DIRECT UPI FALLBACK
# ============================================================

def direct_upi_qr(amount, reference):
    upi_id = setting("upi_id")
    if not upi_id:
        raise RuntimeError("UPI_ID is not configured in Admin Settings.")
    upi = (
        f"upi://pay?pa={upi_id}"
        f"&pn=DocumentService"
        f"&am={int(amount)}"
        f"&cu=INR"
        f"&tn=Application-{reference}"
    )
    q = qrcode.QRCode(version=1, box_size=8, border=4)
    q.add_data(upi)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "upi_payment_qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio, upi

# ============================================================
# RAZORPAY PAYMENT LINK
# ============================================================

def razorpay_credentials():
    # Telegram Admin Panel settings take priority; ENV remains a fallback.
    key_id = setting("razorpay_key_id") or RAZORPAY_KEY_ID
    key_secret = setting("razorpay_key_secret") or RAZORPAY_KEY_SECRET
    webhook_secret = setting("razorpay_webhook_secret") or RAZORPAY_WEBHOOK_SECRET
    return key_id.strip(), key_secret.strip(), webhook_secret.strip()

def razorpay_configured():
    key_id, key_secret, _ = razorpay_credentials()
    return bool(key_id and key_secret)

def create_razorpay_payment_link(amount, application_id, user):
    key_id, key_secret, _ = razorpay_credentials()
    if not (key_id and key_secret):
        raise RuntimeError("Razorpay keys are not configured. Use Admin > Razorpay Settings.")

    minutes = int(setting("payment_expiry") or PAYMENT_TIMEOUT_MINUTES)
    expire_ts = int(time.time()) + max(1, minutes) * 60

    reference_id = f"APP{application_id}-{secrets.token_hex(3)}"
    payload = {
        "amount": int(amount) * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": reference_id[:40],
        "description": f"Document Service Application #{application_id}",
        "expire_by": expire_ts,
        "reminder_enable": False,
        "upi_link": True,
        "notify": {"sms": False, "email": False},
        "notes": {
            "application_id": str(application_id),
            "telegram_user_id": str(user.id),
        }
    }

    r = requests.post(
        "https://api.razorpay.com/v1/payment_links",
        auth=(key_id, key_secret),
        json=payload,
        timeout=20
    )
    if not r.ok:
        raise RuntimeError(f"Razorpay API error: {r.text[:500]}")

    data = r.json()
    return data["id"], data["short_url"], expire_ts

# ============================================================
# APPLICATION FLOW
# ============================================================

@bot.message_handler(commands=["start"])
def start(m):
    user_upsert(m)
    if banned(m.from_user.id):
        bot.reply_to(m, "⛔ आपका account restricted है.")
        return

    sessions[m.chat.id] = {"docs": {}, "idx": 0}
    text = (
        f"👋 <b>नमस्कार {m.from_user.first_name}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Service चुनें</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "नीचे से अपनी service select करें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=main_menu())

@bot.callback_query_handler(func=lambda c: c.data == "coin|balance")
def coin_balance(c):
    bot.answer_callback_query(c.id)
    bal = get_fancoin(c.from_user.id)
    bot.send_message(
        c.message.chat.id,
        f"🪙 <b>Fancoin Balance</b>\n\nआपके पास <b>{bal}</b> Fancoin हैं।"
    )

@bot.message_handler(commands=["fancoin"])
def fancoin_cmd(m):
    user_upsert(m)
    bot.reply_to(m, f"🪙 <b>Fancoin Balance:</b> {get_fancoin(m.from_user.id)}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def category(c):
    bot.answer_callback_query(c.id)
    category_name = c.data.split("|", 1)[1]
    s = sessions.setdefault(c.message.chat.id, {"docs": {}, "idx": 0})
    s.update(category=category_name, docs={}, idx=0)

    mk = types.InlineKeyboardMarkup(row_width=1)
    if category_name == "Jati / Aawas / Niwas":
        for key, label in [
            ("48h", "⚡ 48 घंटे में"),
            ("3d", "📅 3 दिन में"),
            ("online", "💻 सिर्फ Online"),
        ]:
            mk.add(types.InlineKeyboardButton(
                f"{label} • ₹{price(category_name, key)}",
                callback_data=f"plan|{key}"
            ))
        text = (
            f"📄 <b>{category_name}</b>\n\n"
            "अपना delivery plan चुनें:"
        )
    else:
        amount = price(category_name)
        mk.add(types.InlineKeyboardButton(
            f"➡️ Continue • ₹{amount}", callback_data="fixed|go"
        ))
        text = (
            f"📄 <b>{category_name}</b>\n\n"
            f"Service Fee: <b>₹{amount}</b>\n\n"
            "Continue दबाकर documents भरें."
        )

    mk.add(types.InlineKeyboardButton("⬅️ Main Menu", callback_data="back|main"))
    bot.edit_message_text(
        text, c.message.chat.id, c.message.message_id, reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan|") or c.data == "fixed|go")
def plan(c):
    bot.answer_callback_query(c.id)
    s = sessions.get(c.message.chat.id)
    if not s:
        bot.send_message(c.message.chat.id, "Session expire हो गया. /start करें.")
        return

    category_name = s["category"]
    key = c.data.split("|", 1)[1]
    amount = price(category_name, key if category_name == "Jati / Aawas / Niwas" else None)
    label = {
        "48h": "48 घंटे में",
        "3d": "3 दिन में",
        "online": "सिर्फ Online"
    }.get(key, "Standard")

    s.update(plan=label, amount=amount)

    text = (
        "✅ <b>Plan Selected</b>\n\n"
        f"📌 Service: <b>{category_name}</b>\n"
        f"🚚 Plan: <b>{label}</b>\n"
        f"💰 Fee: <b>₹{amount}</b>\n\n"
        "अब documents step-by-step लिए जाएंगे."
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id)
    ask(c.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "back|main")
def back_main(c):
    bot.answer_callback_query(c.id)
    bot.edit_message_text(
        "📋 <b>Main Menu</b>\n\nService चुनें 👇",
        c.message.chat.id,
        c.message.message_id,
        reply_markup=main_menu()
    )

def ask(cid):
    s = sessions.get(cid)
    if not s:
        return
    idx = s["idx"]
    req = DOCS[s["category"]]

    if idx >= len(req):
        payment(cid)
        return

    doc_name = req[idx]
    msg = bot.send_message(
        cid,
        f"📌 <b>Step {idx+1}/{len(req)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📎 <b>{doc_name}</b> भेजें.\n\n"
        "Photo, PDF/document या text भेज सकते हैं."
    )
    bot.register_next_step_handler(msg, doc_input)

def doc_input(m):
    cid = m.chat.id
    if cid not in sessions:
        bot.reply_to(m, "Session expire हो गया. /start करें.")
        return

    s = sessions[cid]
    name = DOCS[s["category"]][s["idx"]]

    if m.content_type == "photo":
        value = ("photo", m.photo[-1].file_id)
    elif m.content_type == "document":
        value = ("document", m.document.file_id)
    elif m.content_type == "text":
        value = ("text", m.text)
    else:
        bot.reply_to(m, "❌ सिर्फ photo/document/text भेजें.")
        ask(cid)
        return

    s["docs"][name] = value
    s["idx"] += 1

    bot.send_message(m.chat.id, f"✅ <b>{name}</b> प्राप्त हो गया.")
    ask(cid)

# ============================================================
# PAYMENT
# ============================================================

def create_application(cid):
    s = sessions[cid]
    c = db()
    cur = c.execute("""
        INSERT INTO applications(
            user_id,username,category,plan,amount,status,created_at
        ) VALUES(?,?,?,?,?,'awaiting_payment',?)
    """, (
        cid,
        s.get("username", ""),
        s["category"],
        s["plan"],
        int(s["amount"]),
        now()
    ))
    appid = cur.lastrowid
    c.commit()
    c.close()
    return appid

def payment(cid):
    s = sessions[cid]
    appid = create_application(cid)
    s["application_id"] = appid

    amount = int(s["amount"])
    upi_enabled = setting("upi_enabled") == "1"
    razorpay_enabled = setting("razorpay_enabled") == "1"

    # The new ON/OFF switches are the final authority. Razorpay is preferred
    # when both methods are ON, with UPI as a safe fallback if Razorpay fails.
    if razorpay_enabled and upi_enabled:
        effective_mode = "both"
    elif razorpay_enabled:
        effective_mode = "razorpay"
    elif upi_enabled:
        effective_mode = "direct_upi"
    else:
        effective_mode = "disabled"

    mk = types.InlineKeyboardMarkup(row_width=1)

    # Razorpay: automatic verification path.
    if effective_mode in ("razorpay", "both") and razorpay_configured():
        try:
            plink_id, short_url, expire_ts = create_razorpay_payment_link(
                amount, appid, type("U", (), {"id": cid})()
            )
            payment_sessions[plink_id] = {
                "application_id": appid,
                "chat_id": cid,
                "amount": amount,
                "expires_at": expire_ts,
                "payment_ref": plink_id,
            }

            c = db()
            c.execute("""
                UPDATE applications
                SET payment_method='razorpay', payment_ref=?, payment_link=?
                WHERE id=?
            """, (plink_id, short_url, appid))
            c.execute("""
                INSERT INTO payments(application_id,provider,provider_id,amount,status,expires_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                appid, "razorpay", plink_id, amount, "created",
                datetime.fromtimestamp(expire_ts, timezone.utc).isoformat(),
                now(), now()
            ))
            c.commit()
            c.close()

            mk.add(types.InlineKeyboardButton(
                f"💳 Pay ₹{amount} • Razorpay UPI",
                url=short_url
            ))
            mk.add(types.InlineKeyboardButton(
                "🔄 Payment Status Check",
                callback_data=f"paycheck|{appid}"
            ))

            text = (
                f"💳 <b>Payment Required</b>\n\n"
                f"🧾 Application: <b>#{appid}</b>\n"
                f"💰 Amount: <b>₹{amount}</b>\n\n"
                f"⏳ <b>Payment window: {setting('payment_expiry') or PAYMENT_TIMEOUT_MINUTES} minutes</b>\n"
                "Razorpay payment automatic verification के लिए enabled है.\n"
                "Payment complete होने पर bot खुद status check करेगा."
            )
            bot.send_message(cid, text, reply_markup=mk)

            q = qrcode.QRCode(version=1, box_size=8, border=4)
            q.add_data(short_url)
            q.make(fit=True)
            img = q.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            bio.name = "razorpay_payment_qr.png"
            img.save(bio, "PNG")
            bio.seek(0)
            bot.send_photo(
                cid,
                bio,
                caption=(
                    f"📲 <b>Scan & Pay</b>\n\n"
                    f"Amount: <b>₹{amount}</b>\n"
                    f"⏳ Valid for <b>{setting('payment_expiry') or PAYMENT_TIMEOUT_MINUTES} minutes</b>."
                ),
                reply_markup=mk
            )
            return
        except Exception as e:
            logging.exception("Razorpay payment link failed: %s", e)
            # If UPI is also ON, transparently fall back to UPI instead of
            # showing the old "no payment method configured" error.
            if not upi_enabled:
                bot.send_message(
                    cid,
                    "⚠️ Razorpay payment अभी उपलब्ध नहीं है.\n"
                    "Admin Panel → Payment Settings में Razorpay credentials check करें."
                )
                return

    # Direct UPI: manual verification path.
    if effective_mode in ("direct_upi", "both") and upi_enabled:
        try:
            bio, upi_url = direct_upi_qr(amount, appid)
            mk2 = types.InlineKeyboardMarkup(row_width=1)
            mk2.add(types.InlineKeyboardButton(
                f"📲 Pay ₹{amount} via UPI",
                url=upi_url
            ))
            mk2.add(types.InlineKeyboardButton(
                "📸 Payment Screenshot भेजें",
                callback_data=f"manualproof|{appid}"
            ))
            bot.send_photo(
                cid,
                bio,
                caption=(
                    f"💳 <b>Direct UPI Payment</b>\n\n"
                    f"🧾 Application: <b>#{appid}</b>\n"
                    f"💰 Amount: <b>₹{amount}</b>\n"
                    f"⏳ QR window: <b>{setting('payment_expiry') or PAYMENT_TIMEOUT_MINUTES} minutes</b>\n\n"
                    "Payment के बाद screenshot/UTR भेजें; admin verification करेगा."
                ),
                reply_markup=mk2
            )
            return
        except Exception as e:
            logging.exception("Direct UPI failed: %s", e)

    # This is reached only when both switches are OFF, or the enabled method
    # has no usable configuration.
    if not upi_enabled and not razorpay_enabled:
        bot.send_message(
            cid,
            "⚠️ <b>सभी payment methods OFF हैं.</b>\n\n"
            "Admin → 💳 Payment Settings खोलकर UPI या Razorpay को ON करें."
        )
    elif razorpay_enabled and not razorpay_configured() and not upi_enabled:
        bot.send_message(
            cid,
            "⚠️ <b>Razorpay ON है लेकिन configured नहीं है.</b>\n\n"
            "Admin → 🔐 Razorpay Settings से Key ID और Key Secret set करें."
        )
    else:
        bot.send_message(
            cid,
            "⚠️ Payment अभी उपलब्ध नहीं है.\n"
            "Admin → 💳 Payment Settings में payment method check करें."
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("manualproof|"))
def manualproof(c):
    bot.answer_callback_query(c.id)
    appid = int(c.data.split("|", 1)[1])
    msg = bot.send_message(
        c.message.chat.id,
        f"📸 <b>Application #{appid}</b>\n\n"
        "Payment screenshot भेजें. चाहें तो साथ में UTR भी text में भेज सकते हैं."
    )
    payment_sessions[f"manual-{c.message.chat.id}"] = {
        "application_id": appid,
        "chat_id": c.message.chat.id,
        "manual": True
    }
    bot.register_next_step_handler(msg, manual_payment_proof)

def manual_payment_proof(m):
    key = f"manual-{m.chat.id}"
    info = payment_sessions.pop(key, None)
    if not info:
        bot.reply_to(m, "Payment session expire हो गया. /start करें.")
        return

    appid = info["application_id"]
    c = db()
    c.execute("""
        UPDATE applications SET status='payment_proof_received',
        payment_method='direct_upi', payment_ref=?
        WHERE id=?
    """, (str(m.message_id), appid))
    c.commit()
    c.close()

    notify_application(appid, m.chat.id, "Manual UPI proof received")
    bot.send_message(
        m.chat.id,
        f"📨 <b>Proof received</b>\nApplication #{appid}\n\n"
        "Admin verification के बाद आगे process होगा.",
        reply_markup=admin_contact_markup()
    )

def mark_razorpay_application_paid(appid, payment_link_id=None):
    cdb = db()
    row = cdb.execute("SELECT * FROM applications WHERE id=?", (appid,)).fetchone()
    cdb.close()
    if not row:
        return False, "Application नहीं मिली."

    key_id, key_secret, _ = razorpay_credentials()
    if not (key_id and key_secret):
        return False, "Razorpay credentials configured नहीं हैं."

    plink_id = payment_link_id or row["payment_ref"]
    if not plink_id:
        return False, "Payment Link ID नहीं मिला."

    try:
        r = requests.get(
            f"https://api.razorpay.com/v1/payment_links/{plink_id}",
            auth=(key_id, key_secret),
            timeout=20
        )
        if not r.ok:
            return False, f"Razorpay status error: {r.text[:250]}"
        data = r.json()
        status = (data.get("status") or "").lower()
        if status != "paid":
            return False, f"Payment अभी paid नहीं है. Razorpay status: {status or 'unknown'}"

        amount_paid = int(data.get("amount_paid", 0)) // 100
        if amount_paid != int(row["amount"]):
            return False, "Payment amount mismatch मिला."

        cdb = db()
        cdb.execute(
            "UPDATE applications SET status='paid', paid_at=?, payment_method='razorpay' WHERE id=?",
            (now(), appid)
        )
        cdb.execute(
            "UPDATE payments SET status='paid', updated_at=? WHERE application_id=? AND provider='razorpay'",
            (now(), appid)
        )
        cdb.commit()
        cdb.close()
        notify_application(appid, int(row["user_id"]), "Razorpay status check: payment_link is paid")
        return True, "paid"
    except Exception as e:
        logging.exception("Razorpay status check failed")
        return False, str(e)

@bot.callback_query_handler(func=lambda c: c.data.startswith("paycheck|"))
def payment_check(c):
    bot.answer_callback_query(c.id)
    appid = int(c.data.split("|", 1)[1])
    cdb = db()
    row = cdb.execute(
        "SELECT status,payment_method,payment_ref,amount FROM applications WHERE id=?",
        (appid,)
    ).fetchone()
    cdb.close()

    if not row:
        bot.send_message(c.message.chat.id, "Application नहीं मिली.")
        return

    if row["status"] != "paid" and row["payment_method"] == "razorpay":
        ok, detail = mark_razorpay_application_paid(appid, row["payment_ref"])
        if ok:
            bot.send_message(
                c.message.chat.id,
                f"✅ <b>Payment Successful</b>\nApplication #{appid}\n\n"
                "Razorpay payment automatically verify हो गई है.\n"
                "अब नीचे screenshot button से proof भेज सकते हैं.",
                reply_markup=types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton("📸 Payment Screenshot भेजें", callback_data=f"sendproof|{appid}")
                )
            )
            return

        bot.send_message(
            c.message.chat.id,
            f"⏳ <b>Payment अभी verify नहीं हुई.</b>\nApplication #{appid}\n\n{detail}"
        )
        return

    if row["status"] == "paid":
        bot.send_message(
            c.message.chat.id,
            f"✅ <b>Payment Successful</b>\nApplication #{appid}\n\n"
            "अब नीचे वाला button दबाकर screenshot भेज दें.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("📸 Payment Screenshot भेजें", callback_data=f"sendproof|{appid}")
            )
        )
    else:
        bot.send_message(
            c.message.chat.id,
            f"⏳ <b>Payment अभी verify नहीं हुई.</b>\n"
            f"Application #{appid}\nStatus: {row['status']}"
        )

def razorpay_payment_monitor():
    """Background auto-check for active Razorpay payment links.
    Webhook remains supported; this is a second safe verification path so the
    admin/user does not have to manually press Payment Status Check.
    """
    while True:
        try:
            candidates = []
            for key, info in list(payment_sessions.items()):
                if not isinstance(info, dict):
                    continue
                if info.get("manual") or not info.get("expires_at"):
                    continue
                candidates.append((key, info))
            for key, info in candidates:
                if time.time() > float(info.get("expires_at", 0)):
                    payment_sessions.pop(key, None)
                    continue
                appid = int(info["application_id"])
                ok, _ = mark_razorpay_application_paid(appid, info.get("payment_ref"))
                if ok:
                    payment_sessions.pop(key, None)
                    try:
                        mk = types.InlineKeyboardMarkup(row_width=1)
                        mk.add(types.InlineKeyboardButton("📸 Payment Screenshot भेजें", callback_data=f"sendproof|{appid}"))
                        bot.send_message(
                            int(info["chat_id"]),
                            f"🎉 <b>Payment Automatically Verified!</b>\n\nApplication #{appid}\n\nRazorpay payment successfully verify हो गई है.",
                            reply_markup=mk
                        )
                    except Exception:
                        pass
        except Exception:
            logging.exception("Razorpay monitor error")
        time.sleep(20)

def notify_application(appid, cid, extra=""):
    c = db()
    row = c.execute("SELECT * FROM applications WHERE id=?", (appid,)).fetchone()
    c.close()
    if not row:
        return

    admin_notify(
        "🔔 <b>NEW APPLICATION UPDATE</b>\n\n"
        f"🧾 Application: <b>#{appid}</b>\n"
        f"👤 User: <code>{row['user_id']}</code>\n"
        f"📂 Category: <b>{row['category']}</b>\n"
        f"🚚 Plan: <b>{row['plan']}</b>\n"
        f"💰 Amount: <b>₹{row['amount']}</b>\n"
        f"💳 Payment: <b>{row['payment_method']}</b>\n"
        f"📌 Status: <b>{row['status']}</b>\n"
        f"📝 {extra}"
    )

# ============================================================
# RAZORPAY WEBHOOK
# ============================================================

def verify_webhook_signature(raw_body, signature):
    _, _, webhook_secret = razorpay_credentials()
    if not webhook_secret:
        return False
    expected = hmac.new(
        webhook_secret.encode(),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")

@app.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(raw, signature):
        return "invalid signature", 400

    try:
        payload = json.loads(raw.decode("utf-8"))
        event = payload.get("event", "")
        if event != "payment_link.paid":
            return "ignored", 200

        entity = (
            payload.get("payload", {})
            .get("payment_link", {})
            .get("entity", {})
        )
        plink_id = entity.get("id", "")
        reference_id = entity.get("reference_id", "")
        amount_paid = int(entity.get("amount_paid", 0)) // 100

        c = db()
        row = c.execute(
            "SELECT * FROM applications WHERE payment_ref=? OR payment_link=?",
            (plink_id, entity.get("short_url", ""))
        ).fetchone()

        if not row and reference_id.startswith("APP"):
            row = c.execute(
                "SELECT * FROM applications WHERE id=?",
                (int(reference_id.split("-")[0][3:]),)
            ).fetchone()

        if row:
            if int(row["amount"]) != amount_paid:
                c.close()
                return "amount mismatch", 400

            c.execute("""
                UPDATE applications
                SET status='paid', paid_at=?, payment_method='razorpay'
                WHERE id=?
            """, (now(), row["id"]))
            c.execute("""
                UPDATE payments SET status='paid', updated_at=?
                WHERE application_id=? AND provider='razorpay'
            """, (now(), row["id"]))
            c.commit()
            cid = int(row["user_id"])
            appid = int(row["id"])
            c.close()

            notify_application(appid, cid, "Razorpay webhook: payment_link.paid")
            payment_sessions.pop(plink_id, None)

            try:
                mk_success = types.InlineKeyboardMarkup(row_width=1)
                mk_success.add(types.InlineKeyboardButton(
                    "📸 Payment Screenshot भेजें",
                    callback_data=f"sendproof|{appid}"
                ))
                if ADMIN_USERNAME:
                    mk_success.add(types.InlineKeyboardButton(
                        "💬 Admin से बात करें",
                        url=f"https://t.me/{ADMIN_USERNAME}"
                    ))
                bot.send_message(
                    cid,
                    f"🎉 <b>Payment Successful!</b>\n\n"
                    f"🧾 Application: <b>#{appid}</b>\n"
                    f"💰 Amount: <b>₹{amount_paid}</b>\n\n"
                    "Payment Razorpay से verify हो गई है.\n"
                    "अब नीचे वाला button दबाकर screenshot भेज दें.",
                    reply_markup=mk_success
                )
                payment_sessions[f"proof-{cid}"] = {
                    "application_id": appid,
                    "chat_id": cid
                }
                # Next user message will be treated as proof only if they use /sendproof.
            except Exception:
                pass

        else:
            c.close()

        return "ok", 200

    except Exception:
        logging.exception("Webhook error")
        return "bad payload", 400

@bot.callback_query_handler(func=lambda c: c.data.startswith("sendproof|"))
def sendproof_button(c):
    bot.answer_callback_query(c.id)
    appid = int(c.data.split("|", 1)[1])
    info = payment_sessions.get(f"proof-{c.message.chat.id}")
    if not info or int(info["application_id"]) != appid:
        info = {"application_id": appid, "chat_id": c.message.chat.id}
        payment_sessions[f"proof-{c.message.chat.id}"] = info
    msg = bot.send_message(
        c.message.chat.id,
        f"📸 <b>Application #{appid}</b> का payment screenshot भेजें."
    )
    bot.register_next_step_handler(msg, paid_payment_proof)

@bot.message_handler(commands=["sendproof"])
def sendproof(m):
    info = payment_sessions.get(f"proof-{m.chat.id}")
    if not info:
        bot.reply_to(m, "कोई pending paid application नहीं मिली.")
        return
    msg = bot.send_message(
        m.chat.id,
        f"📸 Application #{info['application_id']} का screenshot भेजें."
    )
    bot.register_next_step_handler(msg, paid_payment_proof)

def paid_payment_proof(m):
    key = f"proof-{m.chat.id}"
    info = payment_sessions.pop(key, None)
    if not info:
        bot.reply_to(m, "Proof session expire हो गया.")
        return
    appid = info["application_id"]

    c = db()
    c.execute(
        "UPDATE applications SET status='paid_proof_received' WHERE id=?",
        (appid,)
    )
    c.commit()
    c.close()

    # Send collected document references + proof to all admins.
    s = sessions.get(m.chat.id)
    admin_notify(
        f"📸 <b>Payment Screenshot Received</b>\n"
        f"Application: <b>#{appid}</b>\n"
        f"User: <code>{m.chat.id}</code>"
    )
    try:
        bot.forward_message(ADMIN_ID, m.chat.id, m.message_id)
    except Exception:
        pass

    if s:
        for name, (typ, val) in s.get("docs", {}).items():
            for aid in ADMIN_IDS:
                try:
                    if typ == "photo":
                        bot.send_photo(aid, val, caption=f"#{appid} • {name}")
                    elif typ == "document":
                        bot.send_document(aid, val, caption=f"#{appid} • {name}")
                    else:
                        bot.send_message(aid, f"#{appid} • {name}: {val}")
                except Exception:
                    pass

    bot.send_message(
        m.chat.id,
        f"✅ <b>Screenshot received</b>\nApplication #{appid}\n\n"
        "Admin अब आपकी application process करेगा.",
        reply_markup=admin_contact_markup()
    )

# ============================================================
# ADMIN TELEGRAM PANEL
# ============================================================

@bot.message_handler(commands=["admin"])
def admin_command(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "⛔ Admin access नहीं है.")
        return
    bot.send_message(
        m.chat.id,
        "👑 <b>Admin Control Center</b>\n\n"
        "नीचे से settings और management करें.",
        reply_markup=admin_menu()
    )

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "❌ Close Admin")
def admin_close(m):
    bot.send_message(m.chat.id, "Admin panel बंद.", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📊 Bot Stats")
def admin_stats(m):
    c = db()
    users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    apps = c.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]
    paid = c.execute("SELECT COUNT(*) n FROM applications WHERE status LIKE 'paid%'").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) n FROM applications WHERE status='awaiting_payment'").fetchone()["n"]
    c.close()
    bot.send_message(
        m.chat.id,
        f"📊 <b>Bot Stats</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"📋 Applications: <b>{apps}</b>\n"
        f"✅ Paid/Proof: <b>{paid}</b>\n"
        f"⏳ Awaiting Payment: <b>{pending}</b>"
    )

def admin_setting_buttons():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("✏️ UPI ID", callback_data="cfg|upi_id"),
        types.InlineKeyboardButton("💰 Jati 48h", callback_data="cfg|jati_48h"),
        types.InlineKeyboardButton("💰 Jati 3d", callback_data="cfg|jati_3d"),
        types.InlineKeyboardButton("💰 Jati Online", callback_data="cfg|jati_online"),
        types.InlineKeyboardButton("💰 NCL", callback_data="cfg|ncl"),
        types.InlineKeyboardButton("💰 PMS", callback_data="cfg|pms"),
        types.InlineKeyboardButton("⚙️ Payment Mode", callback_data="cfg|payment_mode"),
        types.InlineKeyboardButton("⏱️ Expiry", callback_data="cfg|payment_expiry"),
    )
    return mk

def admin_razorpay_buttons():
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("🔑 Set Key ID", callback_data="rp|key_id"),
        types.InlineKeyboardButton("🔐 Set Key Secret", callback_data="rp|key_secret"),
        types.InlineKeyboardButton("🪝 Set Webhook Secret", callback_data="rp|webhook_secret"),
        types.InlineKeyboardButton("🌐 Set Public Base URL", callback_data="rp|public_base_url"),
        types.InlineKeyboardButton("🗑️ Clear Razorpay Keys", callback_data="rp|clear"),
    )
    return mk

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "💰 Pricing / UPI")
def admin_pricing(m):
    text = (
        "💰 <b>Current Pricing</b>\n\n"
        f"Jati 48h: ₹{setting('jati_48h')}\n"
        f"Jati 3d: ₹{setting('jati_3d')}\n"
        f"Jati Online: ₹{setting('jati_online')}\n"
        f"NCL: ₹{setting('ncl')}\n"
        f"PMS: ₹{setting('pms')}\n\n"
        f"UPI: <code>{setting('upi_id') or 'Not set'}</code>\n\n"
        "नीचे से सीधे Telegram में setting बदलें. ENV में जाने की जरूरत नहीं है."
    )
    bot.send_message(m.chat.id, text, reply_markup=admin_setting_buttons())

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "💳 Payment Settings")
def admin_payment_settings(m):
    key_id, key_secret, webhook_secret = razorpay_credentials()
    text = (
        "💳 <b>Payment Settings</b>\n\n"
        f"UPI ID: <code>{setting('upi_id') or 'Not set'}</code>\n"
        f"UPI Status: <b>{'ON 🟢' if setting('upi_enabled') == '1' else 'OFF 🔴'}</b>\n"
        f"Razorpay Status: <b>{'ON 🟢' if setting('razorpay_enabled') == '1' else 'OFF 🔴'}</b>\n"
        f"Razorpay Keys: <b>{'Configured' if (key_id and key_secret) else 'Not configured'}</b>\n"
        f"Expiry: <b>{setting('payment_expiry')} min</b>\n\n"
        "UPI/Razorpay को नीचे से अलग-अलग ON/OFF करें.\n"
        "UPI ID और Razorpay credentials Telegram Admin Panel से ही set होंगे."
    )
    mk = types.InlineKeyboardMarkup(row_width=2)
    upi_state = "🟢 ON" if setting("upi_enabled") == "1" else "🔴 OFF"
    rp_state = "🟢 ON" if setting("razorpay_enabled") == "1" else "🔴 OFF"
    mk.add(
        types.InlineKeyboardButton(f"📲 UPI {upi_state}", callback_data="paytoggle|upi"),
        types.InlineKeyboardButton(f"💳 Razorpay {rp_state}", callback_data="paytoggle|razorpay"),
    )
    mk.add(
        types.InlineKeyboardButton("⚙️ Mode", callback_data="cfg|payment_mode"),
        types.InlineKeyboardButton("⏱️ Expiry", callback_data="cfg|payment_expiry"),
        types.InlineKeyboardButton("✏️ UPI ID", callback_data="cfg|upi_id"),
        types.InlineKeyboardButton("🔐 Razorpay Setup", callback_data="open|razorpay"),
    )
    bot.send_message(m.chat.id, text, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("paytoggle|"))
def payment_toggle_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return

    target = c.data.split("|", 1)[1]
    if target == "upi":
        key = "upi_enabled"
        label = "UPI"
    elif target == "razorpay":
        key = "razorpay_enabled"
        label = "Razorpay"
    else:
        bot.answer_callback_query(c.id, "Invalid payment method", show_alert=True)
        return

    new_value = "0" if setting(key) == "1" else "1"
    set_setting(key, new_value)

    # Keep the old payment_mode setting synchronized for compatibility.
    upi_on = setting("upi_enabled") == "1"
    rp_on = setting("razorpay_enabled") == "1"
    if upi_on and rp_on:
        set_setting("payment_mode", "both")
    elif rp_on:
        set_setting("payment_mode", "razorpay")
    elif upi_on:
        set_setting("payment_mode", "direct_upi")
    else:
        set_setting("payment_mode", "disabled")

    state = "ON 🟢" if new_value == "1" else "OFF 🔴"
    bot.answer_callback_query(c.id, f"{label}: {state}")
    admin_payment_settings(c.message)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cfg|"))
def admin_cfg_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    key = c.data.split("|", 1)[1]
    prompts = {
        "upi_id": "UPI ID भेजें (example: flipkshop@axl):",
        "jati_48h": "Jati 48h की नई कीमत सिर्फ number में भेजें:",
        "jati_3d": "Jati 3d की नई कीमत सिर्फ number में भेजें:",
        "jati_online": "Jati Online की नई कीमत सिर्फ number में भेजें:",
        "ncl": "NCL की नई कीमत सिर्फ number में भेजें:",
        "pms": "PMS की नई कीमत सिर्फ number में भेजें:",
        "payment_mode": "Payment mode भेजें: razorpay / direct_upi / both",
        "payment_expiry": "Payment expiry minutes भेजें (1-60):",
    }
    if key not in prompts:
        bot.answer_callback_query(c.id, "Invalid setting", show_alert=True)
        return
    admin_input_sessions[c.message.chat.id] = {"kind": "setting", "key": key}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✏️ " + prompts[key])

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.chat.id in admin_input_sessions and admin_input_sessions[m.chat.id].get("kind") == "setting")
def admin_cfg_input(m):
    state = admin_input_sessions.pop(m.chat.id, None)
    if not state:
        return
    kind = state["kind"]
    key = state["key"]
    value = (m.text or "").strip()
    if kind == "setting":
        try:
            if key == "upi_id":
                if "@" not in value or len(value) < 5:
                    raise ValueError("Invalid UPI ID")
            elif key in {"jati_48h", "jati_3d", "jati_online", "ncl", "pms"}:
                value = str(max(0, int(value)))
            elif key == "payment_expiry":
                value = str(min(60, max(1, int(value))))
            elif key == "payment_mode" and value not in {"razorpay", "direct_upi", "both"}:
                raise ValueError("Mode must be razorpay, direct_upi or both")
            set_setting(key, value)
            bot.send_message(m.chat.id, f"✅ <b>{key}</b> updated successfully.\nNew value: <code>{value}</code>")
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Setting update failed: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "open|razorpay")
def open_razorpay_admin(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    bot.answer_callback_query(c.id)
    admin_razorpay_settings(c.message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rp|"))
def razorpay_cfg_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    action = c.data.split("|", 1)[1]
    if action == "clear":
        for key in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"):
            set_setting(key, "")
        bot.answer_callback_query(c.id, "Razorpay keys cleared")
        bot.send_message(c.message.chat.id, "🗑️ Razorpay credentials cleared from bot settings.", reply_markup=admin_razorpay_buttons())
        return
    prompts = {
        "key_id": "Razorpay Key ID भेजें:",
        "key_secret": "Razorpay Key Secret भेजें:\n\n⚠️ यह message private admin chat में ही भेजें.",
        "webhook_secret": "Razorpay Webhook Secret भेजें (optional, webhook verification के लिए):",
        "public_base_url": "Public Base URL भेजें, example: https://your-service.onrender.com",
    }
    if action not in prompts:
        bot.answer_callback_query(c.id, "Invalid option", show_alert=True)
        return
    admin_input_sessions[c.message.chat.id] = {"kind": "razorpay", "key": action}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✏️ " + prompts[action])

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.chat.id in admin_input_sessions and admin_input_sessions[m.chat.id].get("kind") == "razorpay")
def razorpay_cfg_input(m):
    state = admin_input_sessions.pop(m.chat.id, None)
    if not state:
        return
    key = state["key"]
    value = (m.text or "").strip()
    if key == "public_base_url":
        value = value.rstrip("/")
        if not value.startswith("http://") and not value.startswith("https://"):
            bot.send_message(m.chat.id, "❌ URL http:// या https:// से शुरू होना चाहिए.")
            return
        set_setting("public_base_url", value)
        bot.send_message(m.chat.id, "✅ Public Base URL updated.")
        return
    db_key = {
        "key_id": "razorpay_key_id",
        "key_secret": "razorpay_key_secret",
        "webhook_secret": "razorpay_webhook_secret",
    }[key]
    if not value:
        bot.send_message(m.chat.id, "❌ Empty value accepted नहीं है.")
        return
    set_setting(db_key, value)
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass
    bot.send_message(m.chat.id, "✅ Razorpay setting saved. Secret value Telegram में दोबारा show नहीं की जाएगी.")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "🪙 Fancoin")
def admin_fancoin_help(m):
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("✏️ Update Fancoin", callback_data="fcoin|update"))
    bot.send_message(
        m.chat.id,
        "🪙 <b>Fancoin</b>\n\n"
        "अब Fancoin भी Telegram Admin Panel से update कर सकते हैं.\n\n"
        "Example: user_id 123456789, delta +10 या -10",
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data == "fcoin|update")
def admin_fancoin_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    admin_input_sessions[c.message.chat.id] = {"kind": "fancoin"}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✏️ इस format में भेजें:\n<code>USER_ID DELTA REASON</code>\nExample: <code>123456789 +10 Bonus</code>")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.chat.id in admin_input_sessions and admin_input_sessions[m.chat.id].get("kind") == "fancoin")
def admin_fancoin_input(m):
    admin_input_sessions.pop(m.chat.id, None)
    parts = (m.text or "").strip().split(maxsplit=2)
    if len(parts) < 2:
        bot.send_message(m.chat.id, "❌ Format: USER_ID DELTA REASON")
        return
    try:
        uid = int(parts[0])
        delta = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "Admin adjustment"
        balance = change_fancoin(uid, delta, reason)
        bot.send_message(m.chat.id, f"✅ Fancoin updated.\nUser: <code>{uid}</code>\nChange: <b>{delta:+d}</b>\nBalance: <b>{balance}</b>")
        try:
            bot.send_message(uid, f"🪙 <b>Fancoin Update</b>\n\nChange: <b>{delta:+d}</b>\nBalance: <b>{balance}</b>\nReason: {reason}")
        except Exception:
            pass
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Fancoin update failed: {e}")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📢 Broadcast")
def admin_broadcast_prompt(m):
    bot.send_message(
        m.chat.id,
        "📢 Broadcast के लिए message भेजें:\n\n"
        "<code>/broadcast आपका संदेश</code>"
    )

@bot.message_handler(commands=["broadcast"])
def broadcast_command(m):
    if not is_admin(m.from_user.id):
        return
    msg = m.text.replace("/broadcast", "", 1).strip()
    if not msg:
        bot.reply_to(m, "Usage: /broadcast आपका संदेश")
        return

    c = db()
    users = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    c.execute(
        "INSERT INTO broadcasts(message,created_at) VALUES(?,?)",
        (msg, now())
    )
    c.commit()
    c.close()

    sent = 0
    for r in users:
        try:
            bot.send_message(int(r["user_id"]), msg)
            sent += 1
        except Exception:
            pass

    bot.reply_to(m, f"📢 Broadcast complete.\nSent: {sent}")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📋 Applications")
def admin_apps(m):
    c = db()
    rows = c.execute("""
        SELECT id,user_id,category,plan,amount,status,created_at
        FROM applications ORDER BY id DESC LIMIT 20
    """).fetchall()
    c.close()

    if not rows:
        bot.send_message(m.chat.id, "कोई application नहीं.")
        return

    out = ["📋 <b>Recent Applications</b>\n"]
    for r in rows:
        out.append(
            f"#{r['id']} • <code>{r['user_id']}</code> • "
            f"₹{r['amount']} • {r['status']}"
        )
    bot.send_message(m.chat.id, "\n".join(out))

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "🔐 Razorpay Settings")
def admin_razorpay_settings(m):
    key_id, key_secret, webhook_secret = razorpay_credentials()
    public_url = setting("public_base_url") or PUBLIC_BASE_URL
    key_display = (key_id[:10] + "..." if key_id else "Not set")
    bot.send_message(
        m.chat.id,
        "🔐 <b>Razorpay Configuration</b>\n\n"
        f"Key ID: <code>{key_display}</code>\n"
        f"Key Secret: <b>{'Set' if key_secret else 'Not set'}</b>\n"
        f"Webhook Secret: <b>{'Set' if webhook_secret else 'Not set'}</b>\n"
        f"Public URL: <code>{public_url or 'Not set'}</code>\n\n"
        "अब Razorpay credentials सीधे Telegram Admin Panel से set/update हो सकते हैं.\n"
        "BOT_TOKEN के अलावा Render ENV में इन्हें रखना जरूरी नहीं है.",
        reply_markup=admin_razorpay_buttons()
    )

# ============================================================
# WEB ADMIN PANEL
# ============================================================

ADMIN_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Document Service Admin</title>
<style>
body{font-family:Arial;background:#0f172a;color:#e5e7eb;margin:0;padding:18px}
.wrap{max-width:1100px;margin:auto}
.card{background:#111827;border:1px solid #334155;border-radius:16px;padding:16px;margin:14px 0}
h1,h2{margin-top:0}
input,select,textarea{width:100%;box-sizing:border-box;padding:11px;margin:6px 0 12px;border-radius:9px;border:1px solid #475569;background:#0b1220;color:#fff}
button{padding:11px 16px;border:0;border-radius:10px;background:#2563eb;color:white;font-weight:bold}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px;border-bottom:1px solid #334155;text-align:left}
.small{color:#94a3b8;font-size:13px}
.badge{padding:4px 8px;border-radius:999px;background:#1e293b}
</style>
</head>
<body><div class="wrap">
<h1>👑 Document Service Admin</h1>

<div class="card">
<h2>💰 Pricing + UPI</h2>
<form method="post" action="/admin/save-settings">
<label>UPI ID</label><input name="upi" value="{{upi}}">
<label>Jati 48h</label><input name="j48" type="number" value="{{j48}}">
<label>Jati 3d</label><input name="j3" type="number" value="{{j3}}">
<label>Jati Online</label><input name="jo" type="number" value="{{jo}}">
<label>NCL</label><input name="ncl" type="number" value="{{ncl}}">
<label>PMS</label><input name="pms" type="number" value="{{pms}}">
<label>Payment Mode</label>
<select name="payment_mode">
<option value="razorpay" {% if payment_mode=="razorpay" %}selected{% endif %}>Razorpay only</option>
<option value="direct_upi" {% if payment_mode=="direct_upi" %}selected{% endif %}>Direct UPI (manual verification)</option>
<option value="both" {% if payment_mode=="both" %}selected{% endif %}>Both</option>
</select>
<label>Payment expiry (minutes)</label><input name="expiry" type="number" min="1" max="60" value="{{expiry}}">
<button>Save Settings</button>
</form>
<p class="small">Razorpay mode में amount fixed Payment Link पर जाता है और successful payment webhook से auto-verify होता है.</p>
</div>

<div class="card">
<h2>🔐 Razorpay</h2>
<p>Key ID: <span class="badge">{{rp_key}}</span></p>
<p>Key Secret: <span class="badge">{{rp_secret}}</span></p>
<p>Webhook Secret: <span class="badge">{{rp_webhook}}</span></p>
<p class="small">Razorpay credentials Telegram Admin Panel से manage किए जा सकते हैं. Secret values यहां दिखाई नहीं जातीं.</p>
<p><b>Webhook URL:</b> <code>{{webhook_url}}</code></p>
</div>

<div class="card">
<h2>🪙 Fancoin</h2>
<form method="post" action="/admin/fancoin">
<input name="user_id" placeholder="Telegram User ID" required>
<input name="delta" type="number" placeholder="+10 add / -10 deduct" required>
<input name="reason" placeholder="Reason" value="Admin adjustment">
<button>Update Fancoin</button>
</form>
</div>

<div class="card">
<h2>📢 Broadcast</h2>
<form method="post" action="/admin/broadcast">
<textarea name="message" rows="4" placeholder="Message..."></textarea>
<button>Broadcast</button>
</form>
</div>

<div class="card">
<h2>📋 Applications</h2>
<table>
<tr><th>ID</th><th>User</th><th>Category</th><th>Amount</th><th>Status</th><th>Created</th></tr>
{% for r in rows %}
<tr>
<td>#{{r.id}}</td><td>{{r.user_id}}</td><td>{{r.category}}</td>
<td>₹{{r.amount}}</td><td>{{r.status}}</td><td>{{r.created_at}}</td>
</tr>
{% endfor %}
</table>
</div>

<div class="card">
<h2>👥 Users / Fancoin</h2>
<table>
<tr><th>User ID</th><th>Username</th><th>Fancoin</th><th>Banned</th></tr>
{% for u in users %}
<tr><td>{{u.user_id}}</td><td>{{u.username}}</td><td>{{u.fancoin}}</td><td>{{u.banned}}</td></tr>
{% endfor %}
</table>
</div>

<div class="card">
<h2>❤️ Health</h2>
<a href="/health" style="color:#60a5fa">/health</a>
</div>
</div></body></html>
"""

@app.route("/")
def home():
    return redirect(url_for("admin"))

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("secret") != ADMIN_SECRET:
            return "Wrong password", 403
        session["admin"] = True

    if not session.get("admin"):
        return """
        <div style="font-family:Arial;max-width:420px;margin:60px auto;padding:20px">
        <h2>🔐 Admin Login</h2>
        <form method="post">
        <input name="secret" type="password" placeholder="Admin password"
               style="width:100%;padding:12px;box-sizing:border-box">
        <button style="margin-top:10px;padding:12px">Login</button>
        </form></div>
        """

    c = db()
    rows = c.execute(
        "SELECT * FROM applications ORDER BY id DESC LIMIT 100"
    ).fetchall()
    users = c.execute(
        "SELECT user_id,username,fancoin,banned FROM users ORDER BY user_id DESC LIMIT 100"
    ).fetchall()
    c.close()

    public_url = setting("public_base_url") or PUBLIC_BASE_URL
    webhook_url = f"{public_url}/razorpay/webhook" if public_url else "/razorpay/webhook"
    rp_key, rp_secret, rp_webhook = razorpay_credentials()

    return render_template_string(
        ADMIN_HTML,
        rows=rows,
        users=users,
        upi=setting("upi_id"),
        j48=setting("jati_48h"),
        j3=setting("jati_3d"),
        jo=setting("jati_online"),
        ncl=setting("ncl"),
        pms=setting("pms"),
        payment_mode=setting("payment_mode"),
        expiry=setting("payment_expiry"),
        rp_key=(rp_key[:10] + "..." if rp_key else "Not set"),
        rp_secret=("Set" if rp_secret else "Not set"),
        rp_webhook=("Set" if rp_webhook else "Not set"),
        webhook_url=webhook_url
    )

@app.route("/admin/save-settings", methods=["POST"])
def save_settings():
    if not session.get("admin"):
        return "Unauthorized", 403
    values = {
        "upi_id": request.form.get("upi", "").strip(),
        "jati_48h": request.form.get("j48", "300"),
        "jati_3d": request.form.get("j3", "200"),
        "jati_online": request.form.get("jo", "120"),
        "ncl": request.form.get("ncl", "50"),
        "pms": request.form.get("pms", "20"),
        "payment_mode": request.form.get("payment_mode", "razorpay"),
        "payment_expiry": request.form.get("expiry", "9"),
    }
    for k, v in values.items():
        set_setting(k, v)
    return redirect(url_for("admin"))

@app.route("/admin/fancoin", methods=["POST"])
def web_fancoin():
    if not session.get("admin"):
        return "Unauthorized", 403
    uid = int(request.form["user_id"])
    delta = int(request.form["delta"])
    reason = request.form.get("reason", "Admin adjustment")
    balance = change_fancoin(uid, delta, reason)
    try:
        bot.send_message(
            uid,
            f"🪙 <b>Fancoin Update</b>\n\n"
            f"Change: <b>{delta:+d}</b>\n"
            f"Balance: <b>{balance}</b>\n"
            f"Reason: {reason}"
        )
    except Exception:
        pass
    return redirect(url_for("admin"))

@app.route("/admin/broadcast", methods=["POST"])
def web_broadcast():
    if not session.get("admin"):
        return "Unauthorized", 403
    msg = request.form.get("message", "").strip()
    if not msg:
        return redirect(url_for("admin"))

    c = db()
    users = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    c.execute(
        "INSERT INTO broadcasts(message,created_at) VALUES(?,?)",
        (msg, now())
    )
    c.commit()
    c.close()

    sent = 0
    for r in users:
        try:
            bot.send_message(int(r["user_id"]), msg)
            sent += 1
        except Exception:
            pass

    return f"Broadcast sent: {sent}. <a href='/admin'>Back</a>"

@app.route("/health")
def health():
    return "OK", 200

# ============================================================
# START
# ============================================================

# Initialize SQLite settings before Telegram/Flask handlers are used.
init_db()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Telegram bot runs in the background.
    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True),
        daemon=True
    ).start()
    threading.Thread(
        target=razorpay_payment_monitor,
        daemon=True
    ).start()

    # IMPORTANT: Render needs exactly one web server bound to its port.
    # /health is served by this same Flask server, so there is no
    # second HTTPServer competing for port 10000.
    logging.info("Flask admin running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
