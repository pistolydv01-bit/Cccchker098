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
# Other basic settings stay in this file.
# ============================================================

DB = "bot.db"

# Render ENV: BOT_TOKEN only
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Admin Telegram IDs
ADMIN_IDS = [7771292960, 6874667015]
ADMIN_ID = ADMIN_IDS[0]

# Basic settings
DEFAULT_UPI = "sandeepkumar960148.rzp@rxairtel"
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
    if not existing_upi or not (existing_upi["value"] or "").strip() or not valid_upi_id(existing_upi["value"]):
        c.execute("INSERT INTO settings(key,value) VALUES('upi_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DEFAULT_UPI,))
    c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('upi_enabled','1')")
    c.commit()
    c.close()

def valid_upi_id(value):
    value = (value or "").strip().replace(" ", "")
    return bool(value and "@" in value and len(value) >= 5 and not value.startswith("@") and not value.endswith("@"))

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
# UI HELPERS (ATTRACTIVE LOOK)
# ============================================================

def main_menu():
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("📜 Jati / Aawas / Niwas (जाति / आवास / निवास)", callback_data="cat|Jati / Aawas / Niwas"),
        types.InlineKeyboardButton("📑 NCL Certificate (Non-Creamy Layer)", callback_data="cat|NCL"),
        types.InlineKeyboardButton("🎓 PMS Scholarship (पोस्ट मैट्रिक छात्रवृत्ति)", callback_data="cat|PMS"),
        types.InlineKeyboardButton("🪙 My Fancoin Balance", callback_data="coin|balance"),
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
        mk.add(types.InlineKeyboardButton("💬 Admin सहायता से जुड़ें", url=f"https://t.me/{ADMIN_USERNAME}"))
    else:
        mk.add(types.InlineKeyboardButton("💬 Admin Chat", callback_data="admin|contact"))
    return mk

# ============================================================
# DIRECT UPI PAYMENT (WITH AUTO FALLBACK FIX)
# ============================================================

def direct_upi_qr(amount, reference):
    upi_id = (setting("upi_id") or "").strip().replace(" ", "")
    
    # Validation & Fallback: If invalid or empty, use DEFAULT_UPI
    if not valid_upi_id(upi_id):
        upi_id = DEFAULT_UPI
        set_setting("upi_id", DEFAULT_UPI)

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
# APPLICATION FLOW (ATTRACTIVE BOT INTERFACE)
# ============================================================

@bot.message_handler(commands=["start"])
def start(m):
    user_upsert(m)
    if banned(m.from_user.id):
        bot.reply_to(m, "⛔ <b>आपका Account Restricted है।</b>\nअधिक जानकारी के लिए एडमिन से संपर्क करें।")
        return

    sessions[m.chat.id] = {"docs": {}, "idx": 0}
    
    first_name = m.from_user.first_name or "User"
    username = f"@{m.from_user.username}" if m.from_user.username else "N/A"
    
    text = (
        f"✨ <b>स्वागत है, {first_name}!</b> ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{m.from_user.id}</code>\n"
        f"👤 <b>Username:</b> {username}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🏛️ <b>डिजिटल डॉक्यूमेंट सर्विस पोर्टल पर आपका स्वागत है।</b>\n"
        "नीचे दिए गए विकल्पों में से अपनी आवश्यक सर्विस चुनें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=main_menu())

@bot.callback_query_handler(func=lambda c: c.data == "coin|balance")
def coin_balance(c):
    bot.answer_callback_query(c.id)
    bal = get_fancoin(c.from_user.id)
    bot.send_message(
        c.message.chat.id,
        f"🪙 <b>आपका Fancoin Balance</b>\n━━━━━━━━━━━━━━━━━━━━\n💰 कुल कॉइन्स: <b>{bal} Fancoins</b>"
    )

@bot.message_handler(commands=["fancoin"])
def fancoin_cmd(m):
    user_upsert(m)
    bot.reply_to(m, f"🪙 <b>Fancoin Balance:</b> <b>{get_fancoin(m.from_user.id)}</b>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def category(c):
    bot.answer_callback_query(c.id)
    category_name = c.data.split("|", 1)[1]
    s = sessions.setdefault(c.message.chat.id, {"docs": {}, "idx": 0})
    s.update(category=category_name, docs={}, idx=0)

    mk = types.InlineKeyboardMarkup(row_width=1)
    if category_name == "Jati / Aawas / Niwas":
        for key, label in [
            ("48h", "⚡ Superfast • 48 घंटे में"),
            ("3d", "📅 Standard • 3 दिन में"),
            ("online", "💻 Online Only • सिर्फ ऑनलाइन"),
        ]:
            mk.add(types.InlineKeyboardButton(
                f"{label} (₹{price(category_name, key)})",
                callback_data=f"plan|{key}"
            ))
        text = (
            f"📌 <b>सर्विस:</b> {category_name}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "कृपया अपनी सुविधानुसार Delivery Plan चुनें:"
        )
    else:
        amount = price(category_name)
        mk.add(types.InlineKeyboardButton(
            f"🚀 Proceed • ₹{amount}", callback_data="fixed|go"
        ))
        text = (
            f"📌 <b>सर्विस:</b> {category_name}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>सर्विस चार्ज:</b> ₹{amount}\n\n"
            "आगे बढ़ने और दस्तावेज़ अपलोड करने के लिए <b>Proceed</b> पर क्लिक करें।"
        )

    mk.add(types.InlineKeyboardButton("⬅️ बैक (मुख्य मेन्यू)", callback_data="back|main"))
    bot.edit_message_text(
        text, c.message.chat.id, c.message.message_id, reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan|") or c.data == "fixed|go")
def plan(c):
    bot.answer_callback_query(c.id)
    s = sessions.get(c.message.chat.id)
    if not s:
        bot.send_message(c.message.chat.id, "⚠️ Session Expire हो गया। कृपया फिर से /start करें।")
        return

    category_name = s["category"]
    key = c.data.split("|", 1)[1]
    amount = price(category_name, key if category_name == "Jati / Aawas / Niwas" else None)
    label = {
        "48h": "48 घंटे में (Superfast)",
        "3d": "3 दिन में (Standard)",
        "online": "सिर्फ Online"
    }.get(key, "Standard")

    s.update(plan=label, amount=amount)

    text = (
        "✅ <b>प्लाँन का चयन सफल रहा!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>सर्विस:</b> {category_name}\n"
        f"⏱️ <b>समय:</b> {label}\n"
        f"💵 <b>कुल शुल्क:</b> ₹{amount}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📥 <i>अब आपसे आवश्यक दस्तावेज़ एक-एक करके मांगे जाएंगे...</i>"
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id)
    ask(c.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "back|main")
def back_main(c):
    bot.answer_callback_query(c.id)
    first_name = c.from_user.first_name or "User"
    text = (
        f"✨ <b>मुख्य मेन्यू</b> ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "अपनी आवश्यकतानुसार सर्विस चुनें 👇"
    )
    bot.edit_message_text(
        text,
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
        f"📄 <b>Step {idx+1}/{len(req)}: दस्तावेज़ अपलोड</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 कृपया अपना <b>{doc_name}</b> भेजें।\n\n"
        "<i>(आप Photo, PDF या Text मैसेज भेज सकते हैं)</i>"
    )
    bot.register_next_step_handler(msg, doc_input)

def doc_input(m):
    cid = m.chat.id
    if cid not in sessions:
        bot.reply_to(m, "⚠️ Session Expire हो गया। कृपया फिर से /start करें।")
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
        bot.reply_to(m, "❌ अमान्य फॉर्मेट! कृपया केवल Photo, Document या Text भेजें।")
        ask(cid)
        return

    s["docs"][name] = value
    s["idx"] += 1

    bot.send_message(m.chat.id, f"✅ <b>{name}</b> सफलतापूर्वक प्राप्त हुआ।")
    ask(cid)

# ============================================================
# PAYMENT FLOW
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
            "⚠️ <b>UPI Payment वर्तमान में बंद है।</b>\n\n"
            "कृपया कुछ समय बाद प्रयास करें या Admin से संपर्क करें।"
        )
        return

    try:
        bio, upi_url = direct_upi_qr(amount, appid)
        expiry = int(setting("payment_expiry") or PAYMENT_TIMEOUT_MINUTES)
        current_upi = setting("upi_id") or DEFAULT_UPI

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

        payment_sessions[f"proof-{cid}"] = {
            "application_id": appid,
            "chat_id": cid
        }

        mk = types.InlineKeyboardMarkup(row_width=1)
        mk.add(types.InlineKeyboardButton(
            f"📲 Direct App Pay (₹{amount})",
            url=upi_url
        ))
        mk.add(types.InlineKeyboardButton(
            "📸 Payment Screenshot / Proof भेजें",
            callback_data=f"sendproof|{appid}"
        ))

        bot.send_photo(
            cid,
            bio,
            caption=(
                f"💳 <b>UPI PAYMENT GATEWAY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧾 <b>Application No:</b> <code>#{appid}</code>\n"
                f"💰 <b>फिक्स अमाउंट:</b> <b>₹{amount}</b>\n"
                f"📌 <b>UPI ID:</b> <code>{current_upi}</code>\n"
                f"⏳ <b>समय सीमा:</b> {expiry} मिनट\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>भुगतान कैसे करें:</b>\n"
                "1️⃣ ऊपर दिए गए QR को Paytm / PhonePe / GPay से स्कैन करें।\n"
                "2️⃣ भुगतान सफल होने के बाद नीचे <b>Payment Screenshot भेजें</b> बटन पर क्लिक करके स्क्रीनशॉट अपलोड करें।"
            ),
            reply_markup=mk
        )
    except Exception as e:
        logging.exception("Direct UPI payment failed: %s", e)
        bot.send_message(
            cid,
            "⚠️ Payment QR जनरेट करने में समस्या आई। कृपया Admin से संपर्क करें।"
        )

# ============================================================
# PROOF & ADMIN NOTIFICATIONS
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
        f"📸 <b>Application #{appid}</b>\n"
        "कृपया भुगतान का <b>Screenshot</b> या UTR नंबर भेजें:"
    )
    bot.register_next_step_handler(msg, paid_payment_proof)

def paid_payment_proof(m):
    key = f"proof-{m.chat.id}"
    info = payment_sessions.pop(key, None)
    if not info:
        bot.reply_to(m, "⚠️ Proof Session Expire हो गया।")
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
        f"📸 <b>NEW PAYMENT PROOF RECEIVED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Application: <b>#{appid}</b>\n"
        f"👤 User ID: <code>{m.chat.id}</code>"
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
                        bot.send_photo(aid, val, caption=f"App #{appid} • {name}")
                    elif typ == "document":
                        bot.send_document(aid, val, caption=f"App #{appid} • {name}")
                    else:
                        bot.send_message(aid, f"App #{appid} • {name}: {val}")
                except Exception:
                    pass

    bot.send_message(
        m.chat.id,
        f"🎉 <b>स्क्रीनशॉट प्राप्त हो गया है!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Application: <b>#{appid}</b>\n\n"
        "एडमिन वेरिफिकेशन के बाद आपकी सेवा जल्द पूरी की जाएगी।",
        reply_markup=admin_contact_markup()
    )

# ============================================================
# ADMIN TELEGRAM PANEL
# ============================================================

@bot.message_handler(commands=["admin"])
def admin_command(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "⛔ आपके पास Admin Access नहीं है।")
        return
    bot.send_message(
        m.chat.id,
        "⚡ <b>ADMIN CONTROL PANEL</b> ⚡\n━━━━━━━━━━━━━━━━━━━━\nनीचे दिए गए ऑप्शन्स से बॉट मैनेज करें:",
        reply_markup=admin_menu()
    )

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "❌ Close Admin")
def admin_close(m):
    bot.send_message(m.chat.id, "Admin Panel बंद कर दिया गया है।", reply_markup=types.ReplyKeyboardRemove())

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
        f"📊 <b>BOT LIVE STATS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 कुल यूज़र्स: <b>{users}</b>\n"
        f"📋 कुल एप्लिकेशन्स: <b>{apps}</b>\n"
        f"✅ सफल/पेमेंट प्राप्त: <b>{paid}</b>\n"
        f"⏳ पेंडिंग पेमेंट: <b>{pending}</b>"
    )

def admin_setting_buttons():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("✏️ Change UPI ID", callback_data="cfg|upi_id"),
        types.InlineKeyboardButton("💰 Jati 48h", callback_data="cfg|jati_48h"),
        types.InlineKeyboardButton("💰 Jati 3d", callback_data="cfg|jati_3d"),
        types.InlineKeyboardButton("💰 Jati Online", callback_data="cfg|jati_online"),
        types.InlineKeyboardButton("💰 NCL Price", callback_data="cfg|ncl"),
        types.InlineKeyboardButton("💰 PMS Price", callback_data="cfg|pms"),
        types.InlineKeyboardButton("⏱️ Expiry Min", callback_data="cfg|payment_expiry"),
    )
    return mk

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "💰 Pricing / UPI")
def admin_pricing(m):
    text = (
        "💰 <b>PRICING & UPI CONFIGURATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Jati 48h:</b> ₹{setting('jati_48h')}\n"
        f"<b>Jati 3d:</b> ₹{setting('jati_3d')}\n"
        f"<b>Jati Online:</b> ₹{setting('jati_online')}\n"
        f"<b>NCL Price:</b> ₹{setting('ncl')}\n"
        f"<b>PMS Price:</b> ₹{setting('pms')}\n\n"
        f"📌 <b>Current UPI ID:</b> <code>{setting('upi_id') or DEFAULT_UPI}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "नीचे दिए गए बटन से कोई भी वैल्यू तुरंत बदलें:"
    )
    bot.send_message(m.chat.id, text, reply_markup=admin_setting_buttons())

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "💳 Payment Settings")
def admin_payment_settings(m):
    text = (
        "💳 <b>UPI PAYMENT SYSTEM</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Current UPI:</b> <code>{setting('upi_id') or DEFAULT_UPI}</code>\n"
        f"<b>Status:</b> <b>{'🟢 ACTIVE (ON)' if setting('upi_enabled') == '1' else '🔴 INACTIVE (OFF)'}</b>\n"
        f"<b>Expiry Window:</b> {setting('payment_expiry')} Minutes"
    )
    mk = types.InlineKeyboardMarkup(row_width=2)
    upi_state = "🟢 Turn OFF" if setting("upi_enabled") == "1" else "🔴 Turn ON"
    mk.add(types.InlineKeyboardButton(f"{upi_state}", callback_data="paytoggle|upi"))
    mk.add(
        types.InlineKeyboardButton("✏️ Edit UPI ID", callback_data="cfg|upi_id"),
        types.InlineKeyboardButton("⏱️ Edit Expiry", callback_data="cfg|payment_expiry"),
    )
    bot.send_message(m.chat.id, text, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("paytoggle|"))
def payment_toggle_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    new_value = "0" if setting("upi_enabled") == "1" else "1"
    set_setting("upi_enabled", new_value)
    state = "ACTIVE 🟢" if new_value == "1" else "INACTIVE 🔴"
    bot.answer_callback_query(c.id, f"UPI Status: {state}")
    admin_payment_settings(c.message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("cfg|"))
def admin_cfg_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    key = c.data.split("|", 1)[1]
    prompts = {
        "upi_id": "✏️ नई UPI ID भेजें (जैसे: flipkshop@axl):",
        "jati_48h": "💰 Jati 48h का नया प्राइस (केवल नंबर) भेजें:",
        "jati_3d": "💰 Jati 3d का नया प्राइस (केवल नंबर) भेजें:",
        "jati_online": "💰 Jati Online का नया प्राइस भेजें:",
        "ncl": "💰 NCL का नया प्राइस भेजें:",
        "pms": "💰 PMS का नया प्राइस भेजें:",
        "payment_expiry": "⏱️ Payment Expiry समय मिनट में (1-60) भेजें:",
    }
    if key not in prompts:
        bot.answer_callback_query(c.id, "Invalid Setting", show_alert=True)
        return
    admin_input_sessions[c.message.chat.id] = {"kind": "setting", "key": key}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, prompts[key])

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
                if not valid_upi_id(value):
                    raise ValueError("Invalid UPI ID format")
                set_setting("upi_id", value)
                set_setting("upi_enabled", "1")
                bot.send_message(m.chat.id, f"✅ <b>UPI ID अपडेट हो गई!</b>\n\n📌 UPI: <code>{value}</code>\n🟢 Status: <b>Active</b>")
                return
            elif key in {"jati_48h", "jati_3d", "jati_online", "ncl", "pms"}:
                value = str(max(0, int(value)))
            elif key == "payment_expiry":
                value = str(min(60, max(1, int(value))))
            set_setting(key, value)
            bot.send_message(m.chat.id, f"✅ <b>{key}</b> की नई वैल्यू सेट कर दी गई है: <code>{value}</code>")
        except Exception as e:
            bot.send_message(m.chat.id, f"❌ अपडेट असफल: {e}")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "🪙 Fancoin")
def admin_fancoin_help(m):
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("✏️ Fancoin Balance बदलें", callback_data="fcoin|update"))
    bot.send_message(
        m.chat.id,
        "🪙 <b>FANCOIN MANAGEMENT</b>\n━━━━━━━━━━━━━━━━━━━━\nयूज़र के कॉइन्स जोड़ने या घटाने के लिए नीचे बटन पर क्लिक करें।",
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data == "fcoin|update")
def admin_fancoin_callback(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Admin only", show_alert=True)
        return
    admin_input_sessions[c.message.chat.id] = {"kind": "fancoin"}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✏️ इस फॉर्मेट में भेजें:\n<code>USER_ID DELTA REASON</code>\n\n<i>उदाहरण: <code>123456789 +50 Bonus</code></i>")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.chat.id in admin_input_sessions and admin_input_sessions[m.chat.id].get("kind") == "fancoin")
def admin_fancoin_input(m):
    admin_input_sessions.pop(m.chat.id, None)
    parts = (m.text or "").strip().split(maxsplit=2)
    if len(parts) < 2:
        bot.send_message(m.chat.id, "❌ गलत फॉर्मेट! फॉर्मेट: USER_ID DELTA REASON")
        return
    try:
        uid = int(parts[0])
        delta = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "Admin Adjustment"
        balance = change_fancoin(uid, delta, reason)
        bot.send_message(m.chat.id, f"✅ <b>Fancoin Updated!</b>\n\n👤 User: <code>{uid}</code>\n➕ Change: <b>{delta:+d}</b>\n🪙 Total Balance: <b>{balance}</b>")
        try:
            bot.send_message(uid, f"🪙 <b>Fancoin Update</b>\n\nChange: <b>{delta:+d}</b>\nNew Balance: <b>{balance}</b>\nReason: {reason}")
        except Exception:
            pass
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Fancoin Update Failed: {e}")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📢 Broadcast")
def admin_broadcast_prompt(m):
    bot.send_message(
        m.chat.id,
        "📢 <b>BROADCAST MESSAGE</b>\n━━━━━━━━━━━━━━━━━━━━\nसभी यूज़र्स को मैसेज भेजने के लिए कमांड लिखें:\n\n<code>/broadcast आपका संदेश यहाँ लिखें</code>"
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

    bot.reply_to(m, f"📢 <b>Broadcast Complete!</b>\n\nकुल {sent} यूज़र्स को संदेश भेजा गया।")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📋 Applications")
def admin_apps(m):
    c = db()
    rows = c.execute("""
        SELECT id,user_id,category,plan,amount,status,created_at
        FROM applications ORDER BY id DESC LIMIT 20
    """).fetchall()
    c.close()

    if not rows:
        bot.send_message(m.chat.id, "कोई हालिया एप्लिकेशन उपलब्ध नहीं है।")
        return

    out = ["📋 <b>RECENT APPLICATIONS</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        out.append(
            f"<b>#{r['id']}</b> | 👤 <code>{r['user_id']}</code>\n"
            f"📂 {r['category']} ({r['plan']})\n"
            f"💵 ₹{r['amount']} | 📌 <code>{r['status']}</code>\n"
            "------------------------------------"
        )
    bot.send_message(m.chat.id, "\n".join(out))

# ============================================================
# MODERN GLASSMORPHISM WEB ADMIN PANEL
# ============================================================

ADMIN_HTML = """
<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Dashboard | Document Service</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
body { background: #0b0f19; color: #f1f5f9; padding: 24px; min-height: 100vh; }
.wrap { max-width: 1200px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; padding-bottom: 16px; border-bottom: 1px solid #1e293b; }
h1 { font-size: 24px; font-weight: 700; background: linear-gradient(135deg, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-bottom: 24px; }
.card { background: rgba(17, 24, 39, 0.7); backdrop-filter: blur(12px); border: 1px solid #1e293b; border-radius: 18px; padding: 22px; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
.card h2 { font-size: 16px; font-weight: 600; color: #94a3b8; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
label { font-size: 13px; font-weight: 500; color: #cbd5e1; display: block; margin-top: 10px; margin-bottom: 4px; }
input, select, textarea { width: 100%; padding: 11px 14px; border-radius: 10px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 14px; transition: all 0.2s; }
input:focus, select:focus, textarea:focus { border-color: #38bdf8; outline: none; box-shadow: 0 0 0 3px rgba(56,189,248,0.15); }
button { width: 100%; padding: 12px; margin-top: 16px; border: 0; border-radius: 10px; background: linear-gradient(135deg, #2563eb, #3b82f6); color: white; font-weight: 600; cursor: pointer; transition: transform 0.1s, opacity 0.2s; }
button:hover { opacity: 0.95; transform: translateY(-1px); }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
th { text-align: left; padding: 12px; color: #64748b; font-weight: 600; border-bottom: 1px solid #1e293b; }
td { padding: 12px; border-bottom: 1px solid #1e293b; color: #e2e8f0; }
.badge { display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.badge-success { background: rgba(34,197,94,0.15); color: #4ade80; }
.badge-warning { background: rgba(234,179,8,0.15); color: #facc15; }
.badge-danger { background: rgba(239,68,68,0.15); color: #f87171; }
.full-width { grid-column: 1 / -1; }
.small-desc { color: #64748b; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>👑 Admin Control Panel</h1>
  <span class="badge badge-success">● System Live</span>
</header>

<div class="grid">
  <!-- Pricing & UPI Settings -->
  <div class="card">
    <h2>⚙️ Pricing & UPI Configuration</h2>
    <form method="post" action="/admin/save-settings">
      <label>UPI ID (For Payments)</label>
      <input name="upi" value="{{upi}}" required>
      
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div>
          <label>Jati 48h (₹)</label>
          <input name="j48" type="number" value="{{j48}}">
        </div>
        <div>
          <label>Jati 3d (₹)</label>
          <input name="j3" type="number" value="{{j3}}">
        </div>
      </div>

      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div>
          <label>Jati Online (₹)</label>
          <input name="jo" type="number" value="{{jo}}">
        </div>
        <div>
          <label>NCL Price (₹)</label>
          <input name="ncl" type="number" value="{{ncl}}">
        </div>
      </div>

      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div>
          <label>PMS Price (₹)</label>
          <input name="pms" type="number" value="{{pms}}">
        </div>
        <div>
          <label>Payment Expiry (Min)</label>
          <input name="expiry" type="number" min="1" max="60" value="{{expiry}}">
        </div>
      </div>

      <label>UPI Gateway Status</label>
      <select name="upi_enabled">
        <option value="1" {% if upi_enabled=="1" %}selected{% endif %}>🟢 Active (ON)</option>
        <option value="0" {% if upi_enabled=="0" %}selected{% endif %}>🔴 Inactive (OFF)</option>
      </select>

      <button type="submit">Save Changes</button>
    </form>
  </div>

  <!-- Fancoin Management -->
  <div class="card">
    <h2>🪙 Fancoin Balance Manager</h2>
    <form method="post" action="/admin/fancoin">
      <label>Telegram User ID</label>
      <input name="user_id" placeholder="e.g. 123456789" required>
      <label>Coin Delta (+Add / -Deduct)</label>
      <input name="delta" type="number" placeholder="e.g. +50 or -20" required>
      <label>Reason / Note</label>
      <input name="reason" placeholder="Reason" value="Admin adjustment">
      <button type="submit">Update Coins</button>
    </form>
    <p class="small-desc">यह तुरंत यूजर के Fancoin वॉलेट में अपडेट कर देगा।</p>
  </div>

  <!-- Broadcast Section -->
  <div class="card">
    <h2>📢 Mass Broadcast</h2>
    <form method="post" action="/admin/broadcast">
      <label>Broadcast Message</label>
      <textarea name="message" rows="5" placeholder="यहाँ संदेश लिखें जो सभी बॉट यूज़र्स को भेजा जाएगा..." required></textarea>
      <button type="submit" style="background: linear-gradient(135deg, #8b5cf6, #6366f1);">Send Broadcast</button>
    </form>
  </div>
</div>

<!-- Recent Applications Table -->
<div class="card full-width">
  <h2>📋 Recent Applications (Recent 100)</h2>
  <div style="overflow-x:auto;">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>User ID</th>
          <th>Category</th>
          <th>Plan</th>
          <th>Amount</th>
          <th>Status</th>
          <th>Created At</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td><b>#{{r.id}}</b></td>
          <td><code>{{r.user_id}}</code></td>
          <td>{{r.category}}</td>
          <td>{{r.plan}}</td>
          <td><b>₹{{r.amount}}</b></td>
          <td>
            {% if 'paid' in r.status or 'received' in r.status %}
              <span class="badge badge-success">{{r.status}}</span>
            {% elif 'awaiting' in r.status %}
              <span class="badge badge-warning">{{r.status}}</span>
            {% else %}
              <span class="badge badge-danger">{{r.status}}</span>
            {% endif %}
          </td>
          <td>{{r.created_at[:19]}}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<!-- Users List Table -->
<div class="card full-width" style="margin-top:20px;">
  <h2>👥 Registered Users</h2>
  <div style="overflow-x:auto;">
    <table>
      <thead>
        <tr>
          <th>User ID</th>
          <th>Username</th>
          <th>Fancoin</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {% for u in users %}
        <tr>
          <td><code>{{u.user_id}}</code></td>
          <td>{{u.username or 'N/A'}}</td>
          <td><b>{{u.fancoin}} Coins</b></td>
          <td>
            {% if u.banned %}
              <span class="badge badge-danger">Banned</span>
            {% else %}
              <span class="badge badge-success">Active</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

</div>
</body>
</html>
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
        <div style="font-family:'Plus Jakarta Sans',sans-serif;max-width:380px;margin:100px auto;padding:30px;background:#0b0f19;color:#fff;border-radius:18px;border:1px solid #1e293b;box-shadow:0 10px 30px rgba(0,0,0,0.5)">
        <h2 style="margin-bottom:20px;font-size:20px;text-align:center">🔐 Admin Access</h2>
        <form method="post">
        <input name="secret" type="password" placeholder="Admin Password"
               style="width:100%;padding:12px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#fff;box-sizing:border-box">
        <button style="width:100%;margin-top:16px;padding:12px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-weight:600;cursor:pointer">Login Dashboard</button>
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
        upi=setting("upi_id") or DEFAULT_UPI,
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
    
    new_upi = request.form.get("upi", "").strip()
    if not valid_upi_id(new_upi):
        new_upi = DEFAULT_UPI

    values = {
        "upi_id": new_upi,
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
            f"New Balance: <b>{balance}</b>\n"
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

    return f"Broadcast Sent To {sent} Users. <a href='/admin'>Back to Admin</a>"

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
