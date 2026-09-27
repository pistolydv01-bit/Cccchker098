import os
import sys
import json
import gc
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from io import BytesIO
from urllib.parse import quote

import telebot
from telebot import types
import qrcode
from flask import Flask, request, redirect, url_for, render_template_string, session, send_from_directory

# ==============================================================================
# 1. LOGGING & GLOBAL CONFIGURATION
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

DB_NAME = "bot.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Admin Telegram User IDs
ADMIN_IDS = [7771292960, 6874667015]
PRIMARY_ADMIN_ID = ADMIN_IDS[0]

# System Defaults
DEFAULT_UPI = "flipkshop@axl"
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET", "admin12345")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET", "flask_super_secret_key_99")
SERVER_PORT = int(os.getenv("PORT", 10000))
PAYMENT_TIMEOUT_MINUTES = 10

if not BOT_TOKEN:
    logging.critical("FATAL: BOT_TOKEN environment variable is missing!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# Document Requirements per Category
DOCUMENT_REQUIREMENTS = {
    "Jati / Aawas / Niwas": [
        "Identity Proof / Document Details",
        "Passport Size Photo",
        "Mobile Number",
        "Email Address",
        "Full Father's Name & Address Details"
    ],
    "Non-Creamy Layer (NCL)": [
        "Identity Proof Document",
        "Passport Size Photo",
        "Income Details / Certificate",
        "Caste Details / Certificate",
        "Mobile Number & Email"
    ],
    "Post Matric Scholarship (PMS)": [
        "Caste Certificate Details",
        "Income Certificate Details",
        "Residence Certificate Details",
        "Identity Proof",
        "10th/12th Marksheet Details",
        "Bonafide Certificate Details",
        "Fee Receipt Details",
        "Passport Photo",
        "Mobile Number"
    ],
}

DEFAULT_SETTINGS = {
    "upi_id": DEFAULT_UPI,
    "jati_48h": "300",
    "jati_3d": "200",
    "jati_online": "120",
    "ncl": "150",
    "pms": "250",
    "payment_expiry": str(PAYMENT_TIMEOUT_MINUTES),
    "upi_enabled": "1",
}

# In-Memory Sessions
user_sessions = {}
payment_sessions = {}

# ==============================================================================
# 2. DATABASE MANAGEMENT & ORM LAYER
# ==============================================================================

def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def format_readable_time(iso_str):
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return iso_str

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            banned INTEGER DEFAULT 0,
            fancoin INTEGER DEFAULT 0,
            created_at TEXT
        );
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            category TEXT NOT NULL,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            collected_docs TEXT,
            created_at TEXT,
            completed_at TEXT
        );
    """)
    
    for k, v in DEFAULT_SETTINGS.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", (k, v))
        
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully.")

def get_setting(key):
    conn = get_db()
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return r["value"] if r else DEFAULT_SETTINGS.get(key, "")

def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        (key, str(value))
    )
    conn.commit()
    conn.close()

def db_upsert_user(tg_user):
    conn = get_db()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name;
    """, (tg_user.id, tg_user.username or "", tg_user.first_name or "", tg_user.last_name or "", utc_now_iso()))
    conn.commit()
    conn.close()

def is_user_banned(user_id):
    conn = get_db()
    r = conn.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(r and r["banned"] == 1)

def is_admin_user(user_id):
    return int(user_id) in ADMIN_IDS

def calculate_price(category, plan_key=None):
    if category == "Non-Creamy Layer (NCL)":
        return int(get_setting("ncl") or 150)
    if category == "Post Matric Scholarship (PMS)":
        return int(get_setting("pms") or 250)
    
    mapping = {"48h": "jati_48h", "3d": "jati_3d", "online": "jati_online"}
    setting_key = mapping.get(plan_key, "jati_online")
    return int(get_setting(setting_key) or 120)

# ==============================================================================
# 3. HTML FILE GENERATOR FOR COMPLETED APPLICATIONS
# ==============================================================================

def generate_application_html_file(app_id, user_id, docs_dict):
    conn = get_db()
    app_record = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    conn.close()

    if not app_record:
        return None

    cat = app_record["category"]
    plan = app_record["plan"]
    amt = app_record["amount"]
    c_time = format_readable_time(app_record["created_at"])

    html_markup = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Application #{app_id} - Details</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            margin: 0;
            padding: 30px 15px;
        }}
        .container {{
            max-width: 700px;
            margin: 0 auto;
            background: #1e293b;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            border: 1px solid #334155;
        }}
        .header {{
            text-align: center;
            border-bottom: 2px solid #334155;
            padding-bottom: 20px;
            margin-bottom: 25px;
        }}
        .header h1 {{
            margin: 0;
            color: #6366f1;
            font-size: 26px;
        }}
        .badge {{
            background: #10b981;
            color: #ffffff;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: bold;
            display: inline-block;
            margin-top: 10px;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 25px;
            background: #0f172a;
            padding: 15px;
            border-radius: 8px;
        }}
        .info-item span {{
            display: block;
            font-size: 12px;
            color: #94a3b8;
            text-transform: uppercase;
        }}
        .info-item strong {{
            font-size: 16px;
            color: #e2e8f0;
        }}
        .section-title {{
            font-size: 18px;
            color: #38bdf8;
            margin-bottom: 15px;
            border-left: 4px solid #38bdf8;
            padding-left: 10px;
        }}
        .doc-card {{
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 12px 15px;
            margin-bottom: 12px;
        }}
        .doc-name {{
            font-weight: bold;
            color: #cbd5e1;
            margin-bottom: 5px;
        }}
        .doc-val {{
            color: #f1f5f9;
            word-break: break-all;
            font-family: monospace;
            background: #1e293b;
            padding: 6px 10px;
            border-radius: 4px;
            display: block;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            font-size: 12px;
            color: #64748b;
            border-top: 1px solid #334155;
            padding-top: 15px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📄 Digital Application File</h1>
            <div class="badge">APPLICATION ID #{app_id}</div>
        </div>

        <div class="info-grid">
            <div class="info-item"><span>User Telegram ID</span><strong>{user_id}</strong></div>
            <div class="info-item"><span>Category</span><strong>{cat}</strong></div>
            <div class="info-item"><span>Selected Plan</span><strong>{plan}</strong></div>
            <div class="info-item"><span>Amount Paid</span><strong>₹{amt}</strong></div>
            <div class="info-item" style="grid-column: span 2;"><span>Submission Date</span><strong>{c_time}</strong></div>
        </div>

        <div class="section-title">Submitted Documents & Inputs</div>
"""

    if isinstance(docs_dict, dict) and docs_dict:
        for doc_k, (d_type, d_val) in docs_dict.items():
            if d_type == "text":
                val_str = f"TEXT: {d_val}"
            else:
                val_str = f"[{d_type.upper()} TELEGRAM FILE ID]: {d_val}"

            html_markup += f"""
        <div class="doc-card">
            <div class="doc-name">📌 {doc_k}</div>
            <div class="doc-val">{val_str}</div>
        </div>"""
    else:
        html_markup += "<p style='color:#94a3b8;'>कोई अतिरिक्त डॉक्यूमेंट डेटा नहीं मिला।</p>"

    html_markup += f"""
        <div class="footer">
            Generated Automatically by Digital Portal System | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>"""

    os.makedirs("applications_files", exist_ok=True)
    out_file_path = os.path.join("applications_files", f"application_{app_id}.html")

    with open(out_file_path, "w", encoding="utf-8") as f:
        f.write(html_markup)

    # Force RAM garbage collection after file generation
    gc.collect()

    return out_file_path

# ==============================================================================
# 4. UPI QR GENERATION & KEYBOARD UI UTILITIES
# ==============================================================================

def generate_upi_qr_code(amount, app_id):
    upi_id = (get_setting("upi_id") or "").strip().replace(" ", "")
    if not upi_id or "@" not in upi_id:
        upi_id = DEFAULT_UPI
        set_setting("upi_id", upi_id)

    upi_payload = (
        f"upi://pay?pa={quote(upi_id, safe='@')}"
        f"&pn={quote('Digital Portal Services')}"
        f"&am={int(amount)}"
        f"&cu=INR"
        f"&tn={quote(f'Application #{app_id}')}"
    )

    qr = qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(upi_payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0F172A", back_color="white")
    buf = BytesIO()
    buf.name = f"qr_app_{app_id}.png"
    img.save(buf, "PNG")
    buf.seek(0)

    return buf, upi_payload

def get_main_menu_keyboard():
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("📜 Jati / Aawas / Niwas", callback_data="cat|Jati / Aawas / Niwas"),
        types.InlineKeyboardButton("📄 Non-Creamy Layer (NCL)", callback_data="cat|Non-Creamy Layer (NCL)"),
        types.InlineKeyboardButton("🎓 Post Matric Scholarship (PMS)", callback_data="cat|Post Matric Scholarship (PMS)"),
    )
    return mk

# ==============================================================================
# 5. BOT COMMAND & CONVERSATION FLOW
# ==============================================================================

@bot.message_handler(commands=["start"])
def command_start(m):
    db_upsert_user(m.from_user)
    if is_user_banned(m.from_user.id):
        bot.reply_to(m, "⛔ आपका खाता इस बोट पर प्रतिबंधित (Banned) है।")
        return

    user_sessions[m.chat.id] = {"docs": {}, "idx": 0}
    text = (
        f"✨ <b>नमस्ते {m.from_user.first_name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "डिजिटल सेवा पोर्टल पर आपका स्वागत है।\n\n"
        "कृपया नीचे दी गई सेवाओं में से अपनी आवश्यकता अनुसार चुनें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=get_main_menu_keyboard())

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def handle_category_selection(c):
    bot.answer_callback_query(c.id)
    cat_name = c.data.split("|", 1)[1]

    sess = user_sessions.setdefault(c.message.chat.id, {"docs": {}, "idx": 0})
    sess.update(category=cat_name, docs={}, idx=0)

    mk = types.InlineKeyboardMarkup(row_width=1)
    if cat_name == "Jati / Aawas / Niwas":
        mk.add(
            types.InlineKeyboardButton(f"⚡ Express (48 घंटे) — ₹{calculate_price(cat_name, '48h')}", callback_data="plan|48h"),
            types.InlineKeyboardButton(f"📅 Normal (3 दिन) — ₹{calculate_price(cat_name, '3d')}", callback_data="plan|3d"),
            types.InlineKeyboardButton(f"💻 Direct Digital — ₹{calculate_price(cat_name, 'online')}", callback_data="plan|online")
        )
        msg_text = f"📄 <b>{cat_name}</b>\n\nअपनी सुविधा अनुसार डिलीवरी समय चुनें:"
    else:
        price_val = calculate_price(cat_name)
        mk.add(types.InlineKeyboardButton(f"🚀 आगे बढ़ें (₹{price_val})", callback_data="plan|fixed"))
        msg_text = f"📄 <b>{cat_name}</b>\n\nकुल शुल्क: <b>₹{price_val}</b>\n\nजारी रखने के लिए नीचे बटन दबाएं।"

    mk.add(types.InlineKeyboardButton("🔙 मुख्य मेनू", callback_data="nav|main"))
    bot.edit_message_text(msg_text, c.message.chat.id, c.message.message_id, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan|"))
def handle_plan_selection(c):
    bot.answer_callback_query(c.id)
    sess = user_sessions.get(c.message.chat.id)
    if not sess or "category" not in sess:
        bot.send_message(c.message.chat.id, "आपका सेशन टाइमआउट हो गया है। कृपया पुनः /start करें।")
        return

    plan_key = c.data.split("|", 1)[1]
    cat_name = sess["category"]

    label_map = {"48h": "48 घंटे (Express)", "3d": "3 दिन (Normal)", "online": "Direct Digital", "fixed": "Standard"}
    plan_label = label_map.get(plan_key, "Standard")
    amt = calculate_price(cat_name, plan_key if cat_name == "Jati / Aawas / Niwas" else None)

    sess.update(plan=plan_label, amount=amt)

    bot.edit_message_text(
        f"✅ <b>प्लाँन चुना गया: {plan_label}</b>\n\nअब आपसे डॉक्यूमेंट्स लिए जाएंगे।",
        c.message.chat.id,
        c.message.message_id
    )
    prompt_next_document_step(c.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "nav|main")
def handle_nav_main(c):
    bot.answer_callback_query(c.id)
    bot.edit_message_text(
        "📱 <b>मुख्य मेनू</b>\n\nकृपया सर्विस चुनें 👇",
        c.message.chat.id,
        c.message.message_id,
        reply_markup=get_main_menu_keyboard()
    )

def prompt_next_document_step(chat_id):
    sess = user_sessions.get(chat_id)
    if not sess:
        return

    req_list = DOCUMENT_REQUIREMENTS.get(sess["category"], [])
    idx = sess["idx"]

    if idx >= len(req_list):
        initiate_payment_stage(chat_id)
        return

    doc_name = req_list[idx]
    msg = bot.send_message(
        chat_id,
        f"📎 <b>Step {idx + 1} of {len(req_list)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"कृपया <b>{doc_name}</b> की साफ़ फ़ोटो/फ़ाइल अपलोड करें या विवरण टेक्स्ट में लिखें।"
    )
    bot.register_next_step_handler(msg, process_document_input)

def process_document_input(m):
    cid = m.chat.id
    sess = user_sessions.get(cid)
    if not sess:
        bot.reply_to(m, "सेशन समाप्त हो गया है। /start लिखें।")
        return

    req_list = DOCUMENT_REQUIREMENTS.get(sess["category"], [])
    doc_name = req_list[sess["idx"]]

    if m.content_type == "photo":
        val = ("photo", m.photo[-1].file_id)
    elif m.content_type == "document":
        val = ("document", m.document.file_id)
    elif m.content_type == "text":
        val = ("text", m.text)
    else:
        bot.reply_to(m, "❌ अमान्य इनपुट! केवल फ़ोटो, डॉक्यूमेंट या टेक्स्ट भेजें।")
        prompt_next_document_step(cid)
        return

    sess["docs"][doc_name] = val
    sess["idx"] += 1
    bot.send_message(cid, f"✅ <b>{doc_name}</b> दर्ज हो गया।")
    prompt_next_document_step(cid)

# ==============================================================================
# 6. PAYMENT GENERATION & ADMIN SCREENSHOT NOTIFICATION
# ==============================================================================

def create_db_application_entry(cid):
    sess = user_sessions[cid]
    conn = get_db()
    cursor = conn.cursor()

    docs_json = json.dumps(sess.get("docs", {}))

    cursor.execute("""
        INSERT INTO applications (user_id, username, category, plan, amount, status, collected_docs, created_at)
        VALUES (?, ?, ?, ?, ?, 'awaiting_payment', ?, ?);
    """, (cid, sess.get("username", ""), sess["category"], sess["plan"], int(sess["amount"]), docs_json, utc_now_iso()))

    app_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return app_id

def initiate_payment_stage(cid):
    sess = user_sessions[cid]
    app_id = create_db_application_entry(cid)
    sess["application_id"] = app_id
    amt = int(sess["amount"])

    qr_buf, upi_url = generate_upi_qr_code(amt, app_id)

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton(f"💳 Pay ₹{amt} via UPI App", url=upi_url))
    mk.add(types.InlineKeyboardButton("📸 पेमेंट का स्क्रीनशॉट भेजें", callback_data=f"sendproof|{app_id}"))

    bot.send_photo(
        cid,
        qr_buf,
        caption=(
            f"🎉 <b>सभी डॉक्यूमेंट्स प्राप्त हो गए!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧾 <b>Application ID:</b> #{app_id}\n"
            f"💰 <b>कुल राशि:</b> ₹{amt}\n"
            f"📌 <b>UPI ID:</b> <code>{get_setting('upi_id')}</code>\n\n"
            "ऊपर दिए गए QR को स्कैन करके भुगतान करें और नीचे <b>'पेमेंट का स्क्रीनशॉट भेजें'</b> बटन दबाएं।"
        ),
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("sendproof|"))
def handle_send_proof_click(c):
    bot.answer_callback_query(c.id)
    app_id = int(c.data.split("|", 1)[1])

    msg = bot.send_message(
        c.message.chat.id,
        f"📸 <b>Application #{app_id}</b>\n\nकृपया अपने भुगतान का स्क्रीनशॉट फोटो के रूप में भेजें:"
    )
    payment_sessions[f"proof_{c.message.chat.id}"] = {"app_id": app_id}
    bot.register_next_step_handler(msg, process_payment_screenshot)

def process_payment_screenshot(m):
    key = f"proof_{m.chat.id}"
    sess = payment_sessions.pop(key, None)

    if not sess:
        bot.reply_to(m, "पेमेंट सेशन एक्सपायर हो गया। /start करें।")
        return

    if m.content_type != "photo":
        bot.reply_to(m, "❌ यह फोटो नहीं है! कृपया केवल स्क्रीनशॉट फोटो भेजें।")
        return

    app_id = sess["app_id"]

    conn = get_db()
    conn.execute("UPDATE applications SET status='proof_submitted' WHERE id=?", (app_id,))
    conn.commit()
    conn.close()

    # Create Done (Approval) Keyboard for Admin
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("✅ Done (Confirm Payment & Generate File)", callback_data=f"admin_done|{app_id}|{m.chat.id}"))

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"🚨 <b>नया पेमेंट स्क्रीनशॉट प्राप्त हुआ!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧾 <b>Application ID:</b> #{app_id}\n"
                f"👤 <b>User ID:</b> <code>{m.chat.id}</code>\n"
                f"👤 <b>Username:</b> @{m.from_user.username or 'None'}\n\n"
                "नीचे भेजा गया स्क्रीनशॉट देखें और कन्फर्म करने के लिए <b>Done</b> दबाएं:"
            )
            bot.forward_message(admin_id, m.chat.id, m.message_id)
            bot.send_message(admin_id, "👇 **Action ले:**", reply_markup=mk)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    bot.send_message(m.chat.id, "✅ आपका स्क्रीनशॉट एडमिन को सफलतापूर्वक भेज दिया गया है! वेरिफिकेशन के बाद अपडेट मिलेगा।")

# ==============================================================================
# 7. ADMIN DONE ACTION & AUTO HTML GENERATION
# ==============================================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_done|"))
def handle_admin_done_action(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized!", show_alert=True)
        return

    _, app_id_str, user_id_str = c.data.split("|")
    app_id = int(app_id_str)
    user_id = int(user_id_str)

    conn = get_db()
    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()

    if not row:
        bot.answer_callback_query(c.id, "Application not found!", show_alert=True)
        conn.close()
        return

    conn.execute("UPDATE applications SET status='completed', completed_at=? WHERE id=?", (utc_now_iso(), app_id))
    conn.commit()
    conn.close()

    docs_dict = {}
    if row["collected_docs"]:
        try:
            docs_dict = json.loads(row["collected_docs"])
        except Exception:
            docs_dict = {}

    if not docs_dict and user_id in user_sessions:
        docs_dict = user_sessions[user_id].get("docs", {})

    html_file_path = generate_application_html_file(app_id, user_id, docs_dict)

    bot.answer_callback_query(c.id, "Application Approved & HTML Created!")
    bot.edit_message_text(
        f"✅ <b>Application #{app_id} Confirmed!</b>\n\nसारे डॉक्यूमेंट्स एकत्र करके HTML फाइल जनरेट कर दी गई है।",
        c.message.chat.id,
        c.message.message_id
    )

    if html_file_path and os.path.exists(html_file_path):
        with open(html_file_path, "rb") as doc_file:
            bot.send_document(
                c.message.chat.id,
                doc_file,
                caption=f"📄 <b>Application #{app_id} की सम्पूर्ण HTML फाइल</b>"
            )

    # Clean in-memory user session to save RAM
    user_sessions.pop(user_id, None)
    gc.collect()

    try:
        bot.send_message(
            user_id,
            f"🎉 <b>भुगतान स्वीकृत हो गया!</b>\n\nआपकी <b>Application #{app_id}</b> सफलतापूर्वक स्वीकार कर ली गई है। जल्द ही कार्य पूरा कर दिया जाएगा।"
        )
    except Exception as e:
        logging.error(f"Failed to notify user {user_id}: {e}")

# ==============================================================================
# 8. WEB DASHBOARD & HTTP ROUTES
# ==============================================================================

WEB_ADMIN_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Admin Portal - Applications</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background-color: #0b0f19; color: #f3f4f6; margin: 0; padding: 20px; }
        h1 { color: #818cf8; border-bottom: 2px solid #1f2937; padding-bottom: 10px; }
        .table-container { overflow-x: auto; background: #111827; border-radius: 8px; border: 1px solid #1f2937; margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
        th, td { padding: 12px 15px; border-bottom: 1px solid #1f2937; }
        th { background: #1f2937; color: #9ca3af; text-transform: uppercase; font-size: 12px; }
        tr:hover { background: #1f2937; }
        .status-badge { padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }
        .status-completed { background: #065f46; color: #34d399; }
        .status-proof_submitted { background: #854d0e; color: #fde047; }
        .status-awaiting_payment { background: #374151; color: #9ca3af; }
        .btn-download { color: #38bdf8; text-decoration: none; font-weight: bold; background: #0284c722; padding: 5px 10px; border-radius: 4px; border: 1px solid #0284c7; }
        .btn-download:hover { background: #0284c7; color: #fff; }
    </style>
</head>
<body>
    <h1>📋 Applications Dashboard</h1>
    <div class="table-container">
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
                    <th>HTML Report</th>
                </tr>
            </thead>
            <tbody>
                {% for app in applications %}
                <tr>
                    <td><strong>#{{ app.id }}</strong></td>
                    <td><code>{{ app.user_id }}</code></td>
                    <td>{{ app.category }}</td>
                    <td>{{ app.plan }}</td>
                    <td>₹{{ app.amount }}</td>
                    <td><span class="status-badge status-{{ app.status }}">{{ app.status }}</span></td>
                    <td>{{ app.created_at }}</td>
                    <td>
                        <a href="/admin/download/{{ app.id }}" class="btn-download" target="_blank">📄 View HTML</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>"""

@app.route("/")
def index():
    return redirect(url_for("admin_dashboard"))

@app.route("/admin")
def admin_dashboard():
    conn = get_db()
    rows = conn.execute("SELECT * FROM applications ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return render_template_string(WEB_ADMIN_TEMPLATE, applications=rows)

@app.route("/admin/download/<int:app_id>")
def download_html_file(app_id):
    filename = f"application_{app_id}.html"
    return send_from_directory("applications_files", filename)

@app.route("/health")
def health_check():
    return "OK", 200

# ==============================================================================
# 9. RENDER FREE TIER MEMORY CLEANUP WORKER
# ==============================================================================

def start_memory_cleanup_task():
    """Background thread to keep RAM footprint minimal on Render Free Tier"""
    while True:
        try:
            time.sleep(3600)  # Runs every hour
            gc.collect()      # Force RAM garbage cleanup
            logging.info("Render Memory Cleanup Executed.")
        except Exception as e:
            logging.error(f"Cleanup thread error: {e}")

# ==============================================================================
# 10. SERVER INITIALIZATION & MULTI-THREADING
# ==============================================================================

if __name__ == "__main__":
    init_db()

    # Start Memory Cleanup Thread
    cleanup_thread = threading.Thread(target=start_memory_cleanup_task, daemon=True, name="MemoryCleanupThread")
    cleanup_thread.start()

    def run_telegram_polling():
        logging.info("Starting Telegram Bot Polling...")
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception as e:
                logging.error(f"Polling Exception encountered: {e}")
                time.sleep(5)

    poll_thread = threading.Thread(target=run_telegram_polling, daemon=True, name="BotPollingThread")
    poll_thread.start()

    logging.info(f"Starting Web Dashboard Server on Port {SERVER_PORT}...")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
