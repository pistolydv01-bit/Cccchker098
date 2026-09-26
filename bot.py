
import os
import sqlite3
import threading
import logging
import json
import time
import hmac
import hashlib
import requests
from io import BytesIO
from datetime import datetime, timezone

import telebot
from telebot import types
import qrcode
from flask import Flask, request, redirect, url_for, render_template_string, session

# =========================================================
# CONFIG
# =========================================================
DB = "bot.db"

# Put secrets in Render Environment Variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {7771292960, 6874667015}

DEFAULT_UPI = os.getenv("UPI_ID", "yourupi@bank")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me-now")
FLASK_SECRET = os.getenv("FLASK_SECRET", "change-this-secret")
PORT = int(os.getenv("PORT", "10000"))

# Automatic payment settings (Razorpay Payment Links)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
PAYMENT_MINUTES = 9
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@ooooooo929").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable is missing.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
app.secret_key = FLASK_SECRET

sessions = {}

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
}

# =========================================================
# DATABASE
# =========================================================
def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        fancoin INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS applications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        category TEXT,
        plan TEXT,
        amount INTEGER,
        status TEXT DEFAULT 'Pending',
        created_at TEXT,
        docs_json TEXT DEFAULT '',
        payment_link_id TEXT DEFAULT '',
        payment_reference TEXT DEFAULT '',
        payment_expires_at INTEGER DEFAULT 0,
        payment_id TEXT DEFAULT ''
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
    """)
    # Safe migration for an existing bot.db created by an older version.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(applications)").fetchall()}
    for name, typ, default in [
        ("docs_json", "TEXT", "''"),
        ("payment_link_id", "TEXT", "''"),
        ("payment_reference", "TEXT", "''"),
        ("payment_expires_at", "INTEGER", "0"),
        ("payment_id", "TEXT", "''"),
    ]:
        if name not in cols:
            c.execute(f"ALTER TABLE applications ADD COLUMN {name} {typ} DEFAULT {default}")
    for k, v in DEFAULTS.items():
        c.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (k, v)
        )
    c.commit()
    c.close()

def setting(key):
    c = db()
    row = c.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    c.close()
    return row["value"] if row else ""

def set_setting(key, value):
    c = db()
    c.execute("""
        INSERT INTO settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    c.commit()
    c.close()

def user_upsert(message):
    c = db()
    c.execute("""
        INSERT INTO users(user_id,username)
        VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
    """, (message.from_user.id, message.from_user.username or ""))
    c.commit()
    c.close()

def get_fancoin(uid):
    c = db()
    row = c.execute(
        "SELECT fancoin FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    c.close()
    return int(row["fancoin"]) if row else 0

def change_fancoin(uid, delta, reason="Admin adjustment"):
    c = db()
    c.execute("""
        INSERT INTO users(user_id,fancoin)
        VALUES(?,?)
        ON CONFLICT(user_id)
        DO UPDATE SET fancoin=fancoin+excluded.fancoin
    """, (uid, delta))
    c.execute("""
        INSERT INTO fancoin_tx(user_id,delta,reason,created_at)
        VALUES(?,?,?,?)
    """, (uid, delta, reason, now()))
    row = c.execute(
        "SELECT fancoin FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    c.commit()
    c.close()
    return int(row["fancoin"])

def banned(uid):
    c = db()
    row = c.execute(
        "SELECT banned FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    c.close()
    return bool(row and row["banned"])

def price(category, plan=None):
    if category == "NCL":
        return int(setting("ncl"))
    if category == "PMS":
        return int(setting("pms"))
    keys = {
        "48h": "jati_48h",
        "3d": "jati_3d",
        "online": "jati_online"
    }
    return int(setting(keys[plan]))

# =========================================================
# HELPERS
# =========================================================
def is_admin(uid):
    return uid in ADMIN_IDS

def admin_ids():
    return list(ADMIN_IDS)

def send_to_admins(method_name, *args, **kwargs):
    for aid in admin_ids():
        try:
            getattr(bot, method_name)(aid, *args, **kwargs)
        except Exception as exc:
            logging.warning("Admin send failed for %s: %s", aid, exc)

def admin_menu():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("📋 आवेदन", callback_data="adm|apps"),
        types.InlineKeyboardButton("💰 कीमत/UPI", callback_data="adm|pricing"),
    )
    mk.add(
        types.InlineKeyboardButton("🪙 Fancoin", callback_data="adm|coin"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="adm|broadcast"),
    )
    mk.add(
        types.InlineKeyboardButton("👥 Users", callback_data="adm|users"),
        types.InlineKeyboardButton("📊 Stats", callback_data="adm|stats"),
    )
    mk.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="adm|home"))
    return mk

def admin_home(chat_id, edit=False, message_id=None):
    text = (
        "🛠 <b>ADMIN CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "यहाँ से Bot की pricing, UPI, Fancoin,\n"
        "applications और broadcast manage करें.\n\n"
        "🔐 केवल authorized Admins के लिए."
    )
    if edit and message_id:
        bot.edit_message_text(
            text, chat_id, message_id, reply_markup=admin_menu()
        )
    else:
        bot.send_message(chat_id, text, reply_markup=admin_menu())

def make_qr(amount):
    upi = (
        f"upi://pay?pa={setting('upi_id')}"
        f"&pn=DocumentService&am={amount}&cu=INR"
    )
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(upi)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "payment_qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio, upi

# =========================================================
# USER UI
# =========================================================
@bot.message_handler(commands=["start"])
def start(message):
    user_upsert(message)
    if banned(message.from_user.id):
        bot.reply_to(
            message,
            "⛔ <b>आपका खाता प्रतिबंधित है।</b>"
        )
        return

    sessions[message.chat.id] = {"docs": {}}

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton(
            "📄 जाति / आवास / निवास",
            callback_data="cat|Jati / Aawas / Niwas"
        )
    )
    mk.add(
        types.InlineKeyboardButton(
            "🗂 NCL",
            callback_data="cat|NCL"
        )
    )
    mk.add(
        types.InlineKeyboardButton(
            "🎓 PMS – Post Matric Scholarship",
            callback_data="cat|PMS"
        )
    )
    mk.add(
        types.InlineKeyboardButton(
            "🪙 Fancoin Balance",
            callback_data="coin|balance"
        )
    )

    text = (
        "✨ <b>स्वागत है!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "कृपया नीचे से अपनी सेवा चुनें:\n\n"
        "📄 दस्तावेज़ सेवा\n"
        "💳 सुरक्षित भुगतान\n"
        "🪙 Fancoin सुविधा\n\n"
        "👇 <b>सेवा चुनें</b>"
    )
    bot.send_message(message.chat.id, text, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "coin|balance")
def coin_balance(call):
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"🪙 <b>आपका Fancoin Balance</b>\n\n"
        f"💎 <b>{get_fancoin(call.from_user.id)} Fancoin</b>"
    )

@bot.message_handler(commands=["fancoin"])
def fancoin_cmd(message):
    user_upsert(message)
    bot.reply_to(
        message,
        f"🪙 <b>आपका Fancoin Balance</b>\n\n"
        f"💎 <b>{get_fancoin(message.from_user.id)} Fancoin</b>"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def category(call):
    bot.answer_callback_query(call.id)
    category_name = call.data.split("|", 1)[1]

    s = sessions.setdefault(call.message.chat.id, {"docs": {}})
    s.update(category=category_name, docs={}, idx=0)

    mk = types.InlineKeyboardMarkup(row_width=1)

    if category_name == "Jati / Aawas / Niwas":
        mk.add(
            types.InlineKeyboardButton(
                f"⚡ 48 घंटे में — ₹{price(category_name, '48h')}",
                callback_data="plan|48h"
            ),
            types.InlineKeyboardButton(
                f"🕒 3 दिन में — ₹{price(category_name, '3d')}",
                callback_data="plan|3d"
            ),
            types.InlineKeyboardButton(
                f"💻 केवल Online — ₹{price(category_name, 'online')}",
                callback_data="plan|online"
            ),
        )
        text = (
            "📄 <b>जाति / आवास / निवास</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "अपना Delivery Plan चुनें:"
        )
    else:
        amount = price(category_name)
        label = "NCL" if category_name == "NCL" else "PMS"
        mk.add(
            types.InlineKeyboardButton(
                f"✅ {label} सेवा शुरू करें — ₹{amount}",
                callback_data="fixed|go"
            )
        )
        text = (
            f"📑 <b>{label}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰 Service Fee: <b>₹{amount}</b>\n\n"
            "आगे बढ़ने के लिए नीचे बटन दबाएँ:"
        )

    mk.add(types.InlineKeyboardButton("⬅️ मुख्य मेनू", callback_data="home"))
    bot.edit_message_text(
        text, call.message.chat.id, call.message.message_id,
        reply_markup=mk
    )

@bot.callback_query_handler(
    func=lambda c: c.data.startswith("plan|") or c.data == "fixed|go"
)
def plan(call):
    bot.answer_callback_query(call.id)
    s = sessions[call.message.chat.id]
    category_name = s["category"]
    key = call.data.split("|", 1)[1]

    amount = price(
        category_name,
        key if category_name == "Jati / Aawas / Niwas" else None
    )
    labels = {
        "48h": "48 घंटे में",
        "3d": "3 दिन में",
        "online": "केवल Online",
        "go": "Standard"
    }
    label = labels.get(key, "Standard")
    s.update(plan=label, amount=amount)

    bot.edit_message_text(
        f"✅ <b>Plan Selected</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 सेवा: <b>{category_name}</b>\n"
        f"🚚 Plan: <b>{label}</b>\n"
        f"💰 Fee: <b>₹{amount}</b>\n\n"
        "अब हम आपके आवश्यक documents एक-एक करके लेंगे.",
        call.message.chat.id,
        call.message.message_id
    )
    ask(call.message.chat.id)

def ask(cid):
    s = sessions[cid]
    idx = s["idx"]
    req = DOCS[s["category"]]

    if idx >= len(req):
        payment(cid)
        return

    msg = bot.send_message(
        cid,
        f"📌 <b>Step {idx + 1}/{len(req)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📎 <b>{req[idx]}</b> भेजें.\n\n"
        "Photo, PDF/document या text भेज सकते हैं."
    )
    bot.register_next_step_handler(msg, doc_input)

def doc_input(message):
    cid = message.chat.id
    if cid not in sessions:
        bot.reply_to(message, "⌛ Session समाप्त हो गया. /start दबाएँ.")
        return

    s = sessions[cid]
    name = DOCS[s["category"]][s["idx"]]

    if message.content_type == "photo":
        value = ("photo", message.photo[-1].file_id)
    elif message.content_type == "document":
        value = ("document", message.document.file_id)
    elif message.content_type == "text":
        value = ("text", message.text)
    else:
        bot.reply_to(
            message,
            "❌ केवल photo, document/PDF या text भेजें."
        )
        ask(cid)
        return

    s["docs"][name] = value
    s["idx"] += 1

    bot.send_message(
        cid,
        f"✅ <b>{name}</b> प्राप्त हो गया."
    )
    ask(cid)

def razorpay_enabled():
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET)

def create_razorpay_payment_link(amount, reference_id, user_id):
    """Create a fixed-amount UPI Payment Link that expires after 9 minutes."""
    if not razorpay_enabled():
        raise RuntimeError("Razorpay credentials/webhook secret are not configured")
    expire_at = int(time.time()) + PAYMENT_MINUTES * 60
    payload = {
        "upi_link": True,
        "amount": int(amount) * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": reference_id,
        "description": f"Document Service #{reference_id}",
        "expire_by": expire_at,
        "reminder_enable": False,
        "notes": {"telegram_user_id": str(user_id), "reference_id": reference_id},
    }
    r = requests.post(
        "https://api.razorpay.com/v1/payment_links",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json=payload, timeout=20
    )
    r.raise_for_status()
    data = r.json()
    return data["id"], data["short_url"], int(data.get("expire_by", expire_at))

def qr_from_url(url):
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "payment_qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio

def docs_for_db(docs):
    return json.dumps(docs, ensure_ascii=False)

def finalize_paid_application(app_id, payment_id=""):
    c = db()
    row = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        c.close()
        return False
    if row["status"] == "Paid":
        c.close()
        return True
    c.execute("UPDATE applications SET status='Paid', payment_id=? WHERE id=?", (payment_id, app_id))
    c.commit()
    c.close()

    mk = types.InlineKeyboardMarkup(row_width=1)
    if ADMIN_CONTACT:
        contact = ADMIN_CONTACT if ADMIN_CONTACT.startswith("http") else "https://t.me/" + ADMIN_CONTACT.lstrip("@")
        mk.add(types.InlineKeyboardButton("💬 Admin से बात करें", url=contact))
    mk.add(types.InlineKeyboardButton("🏠 मुख्य मेनू", callback_data="home"))
    bot.send_message(
        row["user_id"],
        f"🎉 <b>PAYMENT SUCCESSFUL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Application: <b>#{app_id}</b>\n"
        f"💰 Paid: <b>₹{row['amount']}</b>\n"
        f"✅ Payment verified automatically.\n\n"
        "अब Admin से आगे की बातचीत/काम के लिए नीचे बटन दबाएँ।",
        reply_markup=mk
    )

    send_to_admins(
        "send_message",
        f"💳 <b>PAYMENT VERIFIED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Application: <b>#{app_id}</b>\n"
        f"User: <code>{row['user_id']}</code>\n"
        f"Category: <b>{row['category']}</b>\n"
        f"Amount: <b>₹{row['amount']}</b>\n"
        f"Payment ID: <code>{payment_id or '-'}</code>"
    )

    try:
        docs = json.loads(row["docs_json"] or "{}")
    except Exception:
        docs = {}
    for name, item in docs.items():
        try:
            typ, val = item
            caption = f"📎 #{app_id} — {name}"
            for aid in admin_ids():
                if typ == "photo":
                    bot.send_photo(aid, val, caption=caption)
                elif typ == "document":
                    bot.send_document(aid, val, caption=caption)
                else:
                    bot.send_message(aid, f"{caption}: {val}")
        except Exception as exc:
            logging.warning("Could not send document %s: %s", name, exc)
    return True

def payment(cid):
    """Create a fixed-amount 9-minute payment link and QR after all documents."""
    s = sessions[cid]
    amount = int(s["amount"])
    category_name = s["category"]
    plan_name = s["plan"]

    if not razorpay_enabled():
        bot.send_message(
            cid,
            "⚠️ <b>Automatic payment अभी configured नहीं है.</b>\n\n"
            "Razorpay credentials + webhook setup करना होगा। "
            "सिर्फ normal UPI ID से bot payment को अपने-आप verified नहीं कर सकता।"
        )
        return

    reference_id = f"TG{cid}{int(time.time())}"[-40:]
    try:
        link_id, short_url, expires_at = create_razorpay_payment_link(amount, reference_id, cid)
    except Exception:
        logging.exception("Payment link creation failed")
        bot.send_message(cid, "❌ Payment link बन नहीं पाया। थोड़ी देर बाद फिर कोशिश करें।")
        return

    c = db()
    cur = c.execute("""
        INSERT INTO applications
        (user_id,username,category,plan,amount,status,created_at,docs_json,
         payment_link_id,payment_reference,payment_expires_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cid, str(cid), category_name, plan_name, amount, "Payment Pending", now(),
        docs_for_db(s["docs"]), link_id, reference_id, expires_at
    ))
    app_id = cur.lastrowid
    c.commit()
    c.close()

    img = qr_from_url(short_url)
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton(f"💳 ₹{amount} Pay Now", url=short_url))
    mk.add(types.InlineKeyboardButton("🔄 Payment Status Check", callback_data=f"paycheck|{app_id}"))
    mk.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"paycancel|{app_id}"))

    caption = (
        f"💳 <b>SECURE PAYMENT</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Application: <b>#{app_id}</b>\n"
        f"📄 Service: <b>{category_name}</b>\n"
        f"🚚 Plan: <b>{plan_name}</b>\n"
        f"💰 <b>FIXED AMOUNT: ₹{amount}</b>\n\n"
        "📱 QR scan करने पर amount पहले से fixed रहेगा; user उसे बदल नहीं सकेगा।\n"
        f"⏳ यह payment request <b>{PAYMENT_MINUTES} मिनट</b> में expire होगी।\n\n"
        "Payment successful होते ही webhook से bot अपने-आप success message भेजेगा।"
    )
    bot.send_photo(cid, img, caption=caption, reply_markup=mk)
    bot.send_message(
        cid,
        f"⏳ <b>Payment Timer: {PAYMENT_MINUTES}:00</b>\n"
        f"💰 Amount: <b>₹{amount}</b>\n\n"
        "Payment होने के बाद Screenshot/UTR भेजने की जरूरत नहीं होगी। "
        "Bot gateway confirmation का इंतज़ार करेगा।"
    )
    s["stage"] = "payment_auto"
    s["application_id"] = app_id

@bot.callback_query_handler(func=lambda c: c.data == "payrefresh")
def payment_refresh(call):
    bot.answer_callback_query(call.id, "नई payment request बनाई जा रही है...")
    cid = call.message.chat.id
    if cid not in sessions or sessions[cid].get("category") is None:
        bot.send_message(cid, "⌛ Payment session नहीं मिला. /start से फिर शुरू करें.")
        return
    payment(cid)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_payment")
def cancel_payment(call):
    bot.answer_callback_query(call.id)
    sessions.pop(call.message.chat.id, None)
    bot.send_message(call.message.chat.id, "❌ Payment session cancel कर दिया गया. /start से फिर शुरू करें.")

def payment_proof(message):
    # Automatic mode does not accept screenshots as proof of payment.
    # Keeping this handler prevents old next-step handlers from creating fake
    # successful payments.
    bot.reply_to(
        message,
        "ℹ️ Automatic payment mode में Screenshot/UTR की जरूरत नहीं है. "
        "Payment successful होते ही gateway webhook bot को खुद बता देगा."
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("paycheck|"))
def payment_check(call):
    bot.answer_callback_query(call.id, "Status check किया जा रहा है...")
    app_id = int(call.data.split("|",1)[1])
    c = db()
    row = c.execute("SELECT status,payment_expires_at,amount FROM applications WHERE id=?", (app_id,)).fetchone()
    c.close()
    if not row:
        bot.send_message(call.message.chat.id, "❌ Payment request नहीं मिली.")
        return
    if row["status"] == "Paid":
        bot.send_message(call.message.chat.id, "✅ <b>Payment already verified.</b>")
    elif int(row["payment_expires_at"] or 0) <= int(time.time()):
        bot.send_message(call.message.chat.id, "⌛ यह ₹%s payment request expire हो चुकी है. /start से नया request बनाएं." % row["amount"])
    else:
        left = int(row["payment_expires_at"]) - int(time.time())
        bot.send_message(call.message.chat.id, f"⏳ Payment अभी pending है. लगभग <b>{left} सेकंड</b> बाकी हैं.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("paycancel|"))
def payment_cancel(call):
    bot.answer_callback_query(call.id)
    app_id = int(call.data.split("|",1)[1])
    c = db()
    c.execute("UPDATE applications SET status='Cancelled' WHERE id=? AND status='Payment Pending'", (app_id,))
    c.commit(); c.close()
    bot.send_message(call.message.chat.id, "❌ Payment request cancel कर दी गई.")

@app.route("/payment/webhook", methods=["POST"])
def razorpay_webhook():
    if not RAZORPAY_WEBHOOK_SECRET:
        return "webhook not configured", 503
    raw = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "invalid signature", 400

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return "invalid json", 400

    event = payload.get("event", "")
    if event != "payment_link.paid":
        return "ok", 200

    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    reference = entity.get("reference_id", "")
    payment_id = ""
    payments = entity.get("payments") or []
    if payments:
        payment_id = payments[-1].get("payment_id") or payments[-1].get("id") or ""

    c = db()
    row = c.execute(
        "SELECT id,amount FROM applications WHERE payment_reference=? AND payment_link_id=?",
        (reference, entity.get("id", ""))
    ).fetchone()
    c.close()
    if not row:
        return "unknown reference", 200

    # Do not trust the event alone for amount; compare the gateway amount too.
    gateway_amount = int(entity.get("amount_paid") or entity.get("amount") or 0)
    expected_amount = int(row["amount"]) * 100
    if gateway_amount < expected_amount:
        return "amount mismatch", 400

    finalize_paid_application(int(row["id"]), payment_id)
    return "ok", 200

# =========================================================
# TELEGRAM ADMIN PANEL
# =========================================================
@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ यह command केवल Admin के लिए है.")
        return
    admin_home(message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "home")
def home_callback(call):
    bot.answer_callback_query(call.id)
    start(call.message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm|"))
def admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Unauthorized", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    action = call.data.split("|", 1)[1]

    if action == "home":
        admin_home(call.message.chat.id, True, call.message.message_id)

    elif action == "stats":
        c = db()
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        apps = c.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]
        pending = c.execute(
            "SELECT COUNT(*) n FROM applications WHERE status='Pending'"
        ).fetchone()["n"]
        coins = c.execute(
            "SELECT COALESCE(SUM(fancoin),0) n FROM users"
        ).fetchone()["n"]
        c.close()

        text = (
            "📊 <b>Bot Statistics</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users: <b>{users}</b>\n"
            f"📋 Applications: <b>{apps}</b>\n"
            f"⏳ Pending: <b>{pending}</b>\n"
            f"🪙 Total Fancoin: <b>{coins}</b>"
        )
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton("⬅️ Back", callback_data="adm|home"))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=mk
        )

    elif action == "users":
        c = db()
        rows = c.execute(
            "SELECT user_id,username,fancoin,banned FROM users "
            "ORDER BY rowid DESC LIMIT 15"
        ).fetchall()
        c.close()

        lines = ["👥 <b>Latest Users</b>", "━━━━━━━━━━━━━━━━━━"]
        for r in rows:
            lines.append(
                f"• <code>{r['user_id']}</code> "
                f"@{r['username'] or '-'} "
                f"🪙 {r['fancoin']}"
            )
        if len(lines) == 2:
            lines.append("अभी कोई user नहीं.")
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton("⬅️ Back", callback_data="adm|home"))
        bot.edit_message_text(
            "\n".join(lines), call.message.chat.id,
            call.message.message_id, reply_markup=mk
        )

    elif action == "apps":
        c = db()
        rows = c.execute(
            "SELECT id,user_id,category,plan,amount,status "
            "FROM applications ORDER BY id DESC LIMIT 10"
        ).fetchall()
        c.close()

        text = "📋 <b>Latest Applications</b>\n━━━━━━━━━━━━━━━━━━\n"
        mk = types.InlineKeyboardMarkup(row_width=2)
        if not rows:
            text += "कोई application नहीं."
        else:
            for r in rows:
                text += (
                    f"\n<b>#{r['id']}</b> | {r['category']}\n"
                    f"User: <code>{r['user_id']}</code> | "
                    f"₹{r['amount']} | <b>{r['status']}</b>\n"
                )
                mk.add(
                    types.InlineKeyboardButton(
                        f"✅ #{r['id']}",
                        callback_data=f"status|{r['id']}|Approved"
                    ),
                    types.InlineKeyboardButton(
                        f"❌ #{r['id']}",
                        callback_data=f"status|{r['id']}|Rejected"
                    )
                )
        mk.add(types.InlineKeyboardButton("⬅️ Back", callback_data="adm|home"))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=mk
        )

    elif action == "pricing":
        text = (
            "💰 <b>Pricing & UPI</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Jati 48h: ₹{setting('jati_48h')}\n"
            f"🕒 Jati 3d: ₹{setting('jati_3d')}\n"
            f"💻 Jati Online: ₹{setting('jati_online')}\n"
            f"🗂 NCL: ₹{setting('ncl')}\n"
            f"🎓 PMS: ₹{setting('pms')}\n"
            f"🏦 UPI: <code>{setting('upi_id')}</code>"
        )
        mk = types.InlineKeyboardMarkup(row_width=2)
        mk.add(
            types.InlineKeyboardButton("⚡ 48h", callback_data="editprice|jati_48h"),
            types.InlineKeyboardButton("🕒 3d", callback_data="editprice|jati_3d"),
            types.InlineKeyboardButton("💻 Online", callback_data="editprice|jati_online"),
            types.InlineKeyboardButton("🗂 NCL", callback_data="editprice|ncl"),
            types.InlineKeyboardButton("🎓 PMS", callback_data="editprice|pms"),
            types.InlineKeyboardButton("🏦 UPI", callback_data="editprice|upi_id"),
        )
        mk.add(types.InlineKeyboardButton("⬅️ Back", callback_data="adm|home"))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=mk
        )

    elif action == "coin":
        text = (
            "🪙 <b>Fancoin Management</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "User को Fancoin देने/घटाने के लिए नीचे बटन दबाएँ."
        )
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton(
            "➕ / ➖ Fancoin Update", callback_data="coinadm|update"
        ))
        mk.add(types.InlineKeyboardButton(
            "⬅️ Back", callback_data="adm|home"
        ))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=mk
        )

    elif action == "broadcast":
        bot.send_message(
            call.message.chat.id,
            "📢 <b>Broadcast</b>\n\n"
            "अब अपना message भेजें. वह सभी non-banned users को भेजा जाएगा."
        )
        bot.register_next_step_handler(
            call.message, admin_broadcast_input
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("editprice|"))
def edit_price(call):
    if not is_admin(call.from_user.id):
        return
    key = call.data.split("|", 1)[1]
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"✏️ <b>{key}</b> की नई value भेजें.\n"
        "UPI के लिए UPI ID भेजें, बाकी के लिए केवल amount."
    )
    bot.register_next_step_handler(call.message, admin_price_input, key)

def admin_price_input(message, key):
    if not is_admin(message.from_user.id):
        return
    value = message.text.strip()
    if key != "upi_id":
        try:
            value = str(int(value))
            if int(value) < 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Amount गलत है. केवल number भेजें.")
            return
    set_setting(key, value)
    bot.reply_to(message, f"✅ <b>{key}</b> update हो गया: <code>{value}</code>")
    admin_home(message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "coinadm|update")
def coin_update_start(call):
    if not is_admin(call.from_user.id):
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "🪙 इस format में भेजें:\n"
        "<code>USER_ID AMOUNT REASON</code>\n\n"
        "Example:\n<code>123456789 50 Bonus</code>\n"
        "कटौती के लिए amount negative रखें."
    )
    bot.register_next_step_handler(call.message, coin_update_input)

def coin_update_input(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Format: USER_ID AMOUNT REASON")
        return
    try:
        uid = int(parts[0])
        delta = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ User ID और Amount number होने चाहिए.")
        return
    reason = parts[2] if len(parts) > 2 else "Admin adjustment"
    balance = change_fancoin(uid, delta, reason)
    try:
        bot.send_message(
            uid,
            f"🪙 <b>Fancoin Update</b>\n"
            f"Change: <b>{delta:+d}</b>\n"
            f"Balance: <b>{balance}</b>\n"
            f"Reason: {reason}"
        )
    except Exception:
        pass
    bot.reply_to(
        message,
        f"✅ User <code>{uid}</code> का नया balance: <b>{balance}</b>"
    )
    admin_home(message.chat.id)

def admin_broadcast_input(message):
    if not is_admin(message.from_user.id):
        return
    msg = message.text.strip()
    if not msg:
        bot.reply_to(message, "❌ खाली message नहीं भेज सकते.")
        return

    c = db()
    rows = c.execute(
        "SELECT user_id FROM users WHERE banned=0"
    ).fetchall()
    c.execute(
        "INSERT INTO broadcasts(message,created_at) VALUES(?,?)",
        (msg, now())
    )
    c.commit()
    c.close()

    sent = 0
    for row in rows:
        try:
            bot.send_message(row["user_id"], msg)
            sent += 1
        except Exception:
            pass

    bot.reply_to(
        message,
        f"📢 <b>Broadcast complete</b>\n"
        f"Sent: <b>{sent}</b>"
    )
    admin_home(message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("status|"))
def update_status(call):
    if not is_admin(call.from_user.id):
        return
    _, app_id, status = call.data.split("|", 2)
    app_id = int(app_id)

    c = db()
    row = c.execute(
        "SELECT user_id FROM applications WHERE id=?", (app_id,)
    ).fetchone()
    c.execute(
        "UPDATE applications SET status=? WHERE id=?",
        (status, app_id)
    )
    c.commit()
    c.close()

    if row:
        try:
            bot.send_message(
                row["user_id"],
                f"📢 <b>Application #{app_id}</b>\n"
                f"Status: <b>{status}</b>"
            )
        except Exception:
            pass

    bot.answer_callback_query(call.id, f"Status: {status}")
    admin_home(call.message.chat.id)

# =========================================================
# FLASK WEB ADMIN
# =========================================================
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
        return render_template_string("""
        <!doctype html>
        <html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Admin Login</title>
        <style>
        body{font-family:Arial;background:#0f172a;color:#fff;padding:30px}
        .box{max-width:420px;margin:50px auto;background:#111827;padding:25px;border-radius:18px}
        input,button{width:100%;padding:13px;margin-top:10px;border-radius:10px;border:0}
        button{background:#2563eb;color:#fff;font-weight:700}
        </style></head><body>
        <div class="box"><h2>🛠 Admin Login</h2>
        <form method="post"><input name="secret" type="password"
        placeholder="Admin password"><button>Login</button></form></div>
        </body></html>
        """)

    c = db()
    rows = c.execute(
        "SELECT * FROM applications ORDER BY id DESC LIMIT 100"
    ).fetchall()
    users = c.execute(
        "SELECT user_id,username,fancoin,banned FROM users "
        "ORDER BY rowid DESC LIMIT 100"
    ).fetchall()
    tx = c.execute(
        "SELECT * FROM fancoin_tx ORDER BY id DESC LIMIT 30"
    ).fetchall()
    c.close()

    html = """
    <!doctype html>
    <html><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Document Service Admin</title>
    <style>
    *{box-sizing:border-box} body{margin:0;font-family:Arial;background:#0b1120;color:#e5e7eb}
    .wrap{max-width:1100px;margin:auto;padding:18px}
    .hero{background:linear-gradient(135deg,#1d4ed8,#7c3aed);padding:22px;border-radius:20px;margin-bottom:16px}
    .card{background:#111827;border:1px solid #253047;padding:18px;border-radius:16px;margin:14px 0}
    h2,h3{margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
    input,textarea,button{width:100%;padding:11px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#fff;margin:5px 0}
    button{background:#2563eb;border:0;font-weight:700}.stat{font-size:24px;font-weight:800}
    .tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:700px}
    th,td{padding:9px;border-bottom:1px solid #263244;text-align:left}
    th{color:#93c5fd}.muted{color:#94a3b8}
    </style></head><body><div class="wrap">
    <div class="hero"><h2>🛠 Document Service Admin</h2>
    <div class="muted">Pricing • UPI • Fancoin • Applications • Broadcast</div></div>

    <div class="grid">
      <div class="card"><div class="muted">Users</div><div class="stat">{{users|length}}</div></div>
      <div class="card"><div class="muted">Applications</div><div class="stat">{{rows|length}}</div></div>
      <div class="card"><div class="muted">Fancoin Changes</div><div class="stat">{{tx|length}}</div></div>
    </div>

    <div class="card"><h3>💰 Pricing & UPI</h3>
    <form method="post" action="/settings" class="grid">
      <div>UPI<input name="upi" value="{{upi}}"></div>
      <div>Jati 48h<input name="j48" value="{{j48}}"></div>
      <div>Jati 3d<input name="j3" value="{{j3}}"></div>
      <div>Jati Online<input name="jo" value="{{jo}}"></div>
      <div>NCL<input name="ncl" value="{{ncl}}"></div>
      <div>PMS<input name="pms" value="{{pms}}"></div>
      <div><button>💾 Save Settings</button></div>
    </form></div>

    <div class="card"><h3>🪙 Fancoin</h3>
    <form method="post" action="/fancoin" class="grid">
      <input name="user_id" placeholder="Telegram User ID" required>
      <input name="delta" placeholder="+50 या -20" required>
      <input name="reason" placeholder="Reason" value="Admin adjustment">
      <button>Update Fancoin</button>
    </form></div>

    <div class="card"><h3>📢 Broadcast</h3>
    <form method="post" action="/broadcast">
      <textarea name="message" rows="4" placeholder="Broadcast message"></textarea>
      <button>📢 Send Broadcast</button>
    </form></div>

    <div class="card"><h3>📋 Applications</h3><div class="tablewrap">
    <table><tr><th>ID</th><th>User</th><th>Category</th><th>Plan</th><th>Amount</th><th>Status</th></tr>
    {% for r in rows %}<tr><td>{{r.id}}</td><td>{{r.user_id}}</td><td>{{r.category}}</td>
    <td>{{r.plan}}</td><td>₹{{r.amount}}</td><td>{{r.status}}</td></tr>{% endfor %}
    </table></div></div>

    <div class="card"><h3>👥 Users</h3><div class="tablewrap">
    <table><tr><th>User ID</th><th>Username</th><th>Fancoin</th><th>Banned</th></tr>
    {% for u in users %}<tr><td>{{u.user_id}}</td><td>{{u.username}}</td>
    <td>{{u.fancoin}}</td><td>{{u.banned}}</td></tr>{% endfor %}
    </table></div></div>

    <div class="card"><h3>🪙 Fancoin History</h3><div class="tablewrap">
    <table><tr><th>ID</th><th>User</th><th>Change</th><th>Reason</th><th>Time</th></tr>
    {% for t in tx %}<tr><td>{{t.id}}</td><td>{{t.user_id}}</td><td>{{t.delta}}</td>
    <td>{{t.reason}}</td><td>{{t.created_at}}</td></tr>{% endfor %}
    </table></div></div>

    <div class="card">🟢 Health: <a href="/health" style="color:#60a5fa">/health</a></div>
    </div></body></html>
    """

    return render_template_string(
        html, rows=rows, users=users, tx=tx,
        upi=setting("upi_id"), j48=setting("jati_48h"),
        j3=setting("jati_3d"), jo=setting("jati_online"),
        ncl=setting("ncl"), pms=setting("pms")
    )

@app.route("/settings", methods=["POST"])
def settings():
    if not session.get("admin"):
        return "Unauthorized", 403
    for key, field in {
        "upi_id": "upi", "jati_48h": "j48", "jati_3d": "j3",
        "jati_online": "jo", "ncl": "ncl", "pms": "pms"
    }.items():
        set_setting(key, request.form.get(field, ""))
    return redirect(url_for("admin"))

@app.route("/fancoin", methods=["POST"])
def fancoin_web():
    if not session.get("admin"):
        return "Unauthorized", 403
    uid = int(request.form["user_id"])
    delta = int(request.form["delta"])
    reason = request.form.get("reason", "Admin adjustment")
    balance = change_fancoin(uid, delta, reason)
    try:
        bot.send_message(
            uid,
            f"🪙 <b>Fancoin Update</b>\n"
            f"Change: <b>{delta:+d}</b>\n"
            f"Balance: <b>{balance}</b>\nReason: {reason}"
        )
    except Exception:
        pass
    return redirect(url_for("admin"))

@app.route("/broadcast", methods=["POST"])
def broadcast_web():
    if not session.get("admin"):
        return "Unauthorized", 403
    msg = request.form.get("message", "").strip()
    if not msg:
        return redirect(url_for("admin"))

    c = db()
    rows = c.execute(
        "SELECT user_id FROM users WHERE banned=0"
    ).fetchall()
    c.execute(
        "INSERT INTO broadcasts(message,created_at) VALUES(?,?)",
        (msg, now())
    )
    c.commit()
    c.close()

    sent = 0
    for row in rows:
        try:
            bot.send_message(row["user_id"], msg)
            sent += 1
        except Exception:
            pass

    return f"Broadcast sent: {sent}. <a href='/admin'>Back</a>"

@app.route("/health")
def health():
    return "OK", 200

def run_web():
    logging.info("🌐 Web/Admin server running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# =========================================================
# START
# =========================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    logging.info("🤖 Telegram bot starting...")
    bot.infinity_polling(skip_pending=True)
