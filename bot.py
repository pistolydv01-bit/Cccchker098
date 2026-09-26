import os, sqlite3, threading, logging, time, hmac, hashlib, json, secrets
from datetime import datetime, timezone, timedelta
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import telebot
from telebot import types
import qrcode
from flask import Flask, request, redirect, url_for, render_template_string, session
from urllib.parse import quote

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
    "payment_expiry": str(PAYMENT_TIMEOUT_MINUTES),
    "upi_enabled": "1",
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
    existing_upi = c.execute("SELECT value FROM settings WHERE key='upi_id'").fetchone()
    if not existing_upi or not (existing_upi["value"] or "").strip() or (existing_upi["value"] or "").strip().upper() in {
        "YOUR_UPI_ID_HERE", "YOURUPI@BANK"
    }:
        c.execute("INSERT INTO settings(key,value) VALUES('upi_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DEFAULT_UPI,))
    c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('upi_enabled','1')")
    c.commit()
    c.close()

def normalize_upi_setting():
    current = (setting("upi_id") or "").strip().replace(" ", "")
    if not valid_upi_id(current):
        set_setting("upi_id", DEFAULT_UPI)

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
    mk.add("❌ Close Admin")
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
# DIRECT UPI PAYMENT
# ============================================================

def valid_upi_id(value):
    value = (value or "").strip().replace(" ", "")
    return bool(value and "@" in value and len(value) >= 5 and not value.startswith("@") and not value.endswith("@"))

def direct_upi_qr(amount, reference):
    upi_id = (setting("upi_id") or "").strip().replace(" ", "")
    if not valid_upi_id(upi_id):
        upi_id = DEFAULT_UPI
        set_setting("upi_id", upi_id)
    upi = (
        "upi://pay?"
        f"pa={quote(upi_id, safe='@')}"
        f"&pn={quote('PowerOfSandip')}"
        f"&am={int(amount)}"
        "&cu=INR"
        f"&tn={quote(f'Application-{reference}')}"
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

    if setting("upi_enabled") != "1":
        bot.send_message(
            cid,
            "⚠️ <b>UPI payment अभी OFF है.</b>\n\n"
            "Admin → 💳 Payment Settings में UPI को ON करें."
        )
        return

    try:
        bio, upi_url = direct_upi_qr(amount, appid)
        expiry = int(setting("payment_expiry") or PAYMENT_TIMEOUT_MINUTES)

        c = db()
        c.execute(
            "UPDATE applications SET payment_method='direct_upi', payment_ref=?, payment_link=? WHERE id=?",
            (upi_url, upi_url, appid)
        )
        c.execute(
            "INSERT INTO payments(application_id,provider,provider_id,amount,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (appid, "direct_upi", str(appid), amount, "awaiting_proof",
             (datetime.now(timezone.utc) + timedelta(minutes=expiry)).isoformat(), now(), now())
        )
        c.commit()
        c.close()

        # Update payment_sessions state for fallback screenshot uploads
        payment_sessions[f"proof-{cid}"] = {
            "application_id": appid,
            "chat_id": cid
        }

        mk = types.InlineKeyboardMarkup(row_width=1)
        mk.add(types.InlineKeyboardButton(
            f"📲 Pay ₹{amount} via UPI",
            url=upi_url
        ))
        mk.add(types.InlineKeyboardButton(
            "📸 Payment Screenshot भेजें",
            callback_data=f"sendproof|{appid}"
        ))

        bot.send_photo(
            cid,
            bio,
            caption=(
                f"💳 <b>Payment QR Code</b>\n\n"
                f"🧾 <b>Application:</b> #{appid}\n"
                f"💰 <b>Amount:</b> ₹{amount}\n"
                f"📌 <b>UPI ID:</b> <code>{setting('upi_id')}</code>\n\n"
                f"⏳ QR window: <b>{expiry} minutes</b>\n\n"
                "1. इस QR को किसी भी UPI app (Paytm/GPay/PhonePe) से scan करके payment करें.\n"
                "2. Payment के बाद नीचे <b>Payment Screenshot भेजें</b> दबाकर screenshot/UTR भेजें.\n"
                "3. Admin payment verify करेगा."
            ),
            reply_markup=mk
        )
    except Exception as e:
        logging.exception("Direct UPI payment failed: %s", e)
        bot.send_message(
            cid,
            "⚠️ <b>UPI payment QR बनाने में समस्या आई.</b>\n\n"
            "Admin → 💳 Payment Settings में valid UPI ID check करें."
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
# PAYMENT PROOF / ADMIN NOTIFICATIONS
# ============================================================

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

    admin_notify(
        f"📸 <b>Payment Screenshot Received</b>\n"
        f"Application: <b>#{appid}</b>\n"
        f"User: <code>{m.chat.id}</code>"
    )
    try:
        bot.forward_message(ADMIN_ID, m.chat.id, m.message_id)
    except Exception:
        pass

    s = sessions.get(m.chat.id)
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
        types.InlineKeyboardButton("⏱️ Expiry", callback_data="cfg|payment_expiry"),
    )
    return mk

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "💰 Pricing / UPI")
def admin_pricing(m):
    text = (
        "💰 <b>Current Pricing & Config</b>\n\n"
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
    text = (
        "💳 <b>UPI Payment Settings</b>\n\n"
        f"UPI ID: <code>{setting('upi_id') or 'Not set'}</code>\n"
        f"UPI Status: <b>{'ON 🟢' if setting('upi_enabled') == '1' else 'OFF 🔴'}</b>\n"
        f"Expiry: <b>{setting('payment_expiry')} min</b>\n\n"
        "UPI ON होने पर saved UPI ID से QR बनेगा. UPI OFF होने पर नया payment नहीं बनेगा."
    )
    mk = types.InlineKeyboardMarkup(row_width=2)
    upi_state = "🟢 ON" if setting("upi_enabled") == "1" else "🔴 OFF"
    mk.add(types.InlineKeyboardButton(f"📲 UPI {upi_state}", callback_data="paytoggle|upi"))
    mk.add(
        types.InlineKeyboardButton("✏️ UPI ID", callback_data="cfg|upi_id"),
        types.InlineKeyboardButton("⏱️ Expiry", callback_data="cfg|payment_expiry"),
    )
    bot.send_message(m.chat.id, text, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("paytoggle|"))
def payment_toggle_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    target = c.data.split("|", 1)[1]
    if target != "upi":
        bot.answer_callback_query(c.id, "Only UPI is enabled in this build", show_alert=True)
        return
    new_value = "0" if setting("upi_enabled") == "1" else "1"
    set_setting("upi_enabled", new_value)
    state = "ON 🟢" if new_value == "1" else "OFF 🔴"
    bot.answer_callback_query(c.id, f"UPI: {state}")
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
                if "@" not in value or len(value) < 5 or " " in value:
                    raise ValueError("Invalid UPI ID")
                set_setting("upi_id", value)
                set_setting("upi_enabled", "1")
                bot.send_message(m.chat.id, f"✅ UPI ID saved successfully.\nUPI: <code>{value}</code>\nStatus: <b>ON 🟢</b>")
                return
            elif key in {"jati_48h", "jati_3d", "jati_online", "ncl", "pms"}:
                value = str(max(0, int(value)))
            elif key == "payment_expiry":
                value = str(min(60, max(1, int(value))))
            set_setting(key, value)
            bot.send_message(m.chat.id, f"✅ <b>{key}</b> updated successfully.\nNew value: <code>{value}</code>")
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ Setting update failed: {e}")

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
<label>UPI Status</label>
<select name="upi_enabled">
<option value="1" {% if upi_enabled=="1" %}selected{% endif %}>ON</option>
<option value="0" {% if upi_enabled=="0" %}selected{% endif %}>OFF</option>
</select>
<label>Payment expiry (minutes)</label><input name="expiry" type="number" min="1" max="60" value="{{expiry}}">
<button>Save Settings</button>
</form>
<p class="small">UPI mode में saved UPI ID से QR/deep-link बनता है. Payment के बाद screenshot/UTR admin verify करता है.</p>
</div>

<div class="card">
<h2>📲 UPI Payment</h2>
<p class="small">इस build में केवल Direct UPI payment active है.</p>
<p class="small">UPI ID और ON/OFF इसी Admin Panel से बदलें; Render ENV में UPI setting की जरूरत नहीं है.</p>
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
        upi_enabled=setting("upi_enabled"),
        expiry=setting("payment_expiry"),
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
        "upi_enabled": request.form.get("upi_enabled", "1"),
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

init_db()
normalize_upi_setting()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    threading.Thread(
        target=lambda: bot.infinity_polling(skip_pending=True),
        daemon=True
    ).start()

    logging.info("Flask admin running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
