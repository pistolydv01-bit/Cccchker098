import os
import sys
import json
import gc
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot
from telebot import types
import qrcode

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

# ⚠️ अपनी न्यूमेरिक टेलीग्राम यूज़र आईडी यहाँ डालें (उदा: [123456789])
ADMIN_IDS = [7771292960, 6874667015]

# ⚠️ यहाँ अपनी चालू UPI ID डालें (ताकि QR कोड तुरंत बन सके)
DEFAULT_UPI = "flipkshop@axl"

PAYMENT_TIMEOUT_MINUTES = 10

if not BOT_TOKEN:
    logging.critical("FATAL: BOT_TOKEN environment variable is missing!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

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
}

# In-Memory Sessions
user_sessions = {}
payment_sessions = {}

# ==============================================================================
# 2. DATABASE MANAGEMENT
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
# 3. HTML GENERATOR FOR COMPLETED APPLICATIONS
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
        body {{ font-family: sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #1e293b; border-radius: 10px; padding: 20px; }}
        .header {{ text-align: center; border-bottom: 2px solid #334155; padding-bottom: 10px; margin-bottom: 20px; }}
        .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; background: #0f172a; padding: 10px; border-radius: 6px; }}
        .doc-card {{ background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 10px; margin-bottom: 10px; }}
        .doc-val {{ word-break: break-all; font-family: monospace; color: #38bdf8; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📄 Application File #{app_id}</h1>
        </div>
        <div class="info-grid">
            <div>User ID: <strong>{user_id}</strong></div>
            <div>Category: <strong>{cat}</strong></div>
            <div>Plan: <strong>{plan}</strong></div>
            <div>Amount: <strong>₹{amt}</strong></div>
        </div>
        <h3>Submitted Documents</h3>
"""

    if isinstance(docs_dict, dict) and docs_dict:
        for doc_k, (d_type, d_val) in docs_dict.items():
            val_str = f"TEXT: {d_val}" if d_type == "text" else f"[{d_type.upper()} FILE ID]: {d_val}"
            html_markup += f"""
        <div class="doc-card">
            <strong>📌 {doc_k}</strong>
            <div class="doc-val">{val_str}</div>
        </div>"""

    html_markup += f"""
    </div>
</body>
</html>"""

    os.makedirs("applications_files", exist_ok=True)
    out_file_path = os.path.join("applications_files", f"application_{app_id}.html")

    with open(out_file_path, "w", encoding="utf-8") as f:
        f.write(html_markup)

    gc.collect()
    return out_file_path

# ==============================================================================
# 4. UPI QR GENERATOR & UI KEYBOARD
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
# 5. USER & ADMIN COMMAND HANDLERS
# ==============================================================================

@bot.message_handler(commands=["start"])
def command_start(m):
    db_upsert_user(m.from_user)
    user_sessions[m.chat.id] = {"docs": {}, "idx": 0}
    text = (
        f"✨ <b>नमस्ते {m.from_user.first_name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "डिजिटल सेवा पोर्टल पर आपका स्वागत है।\n\n"
        "कृपया नीचे दी गई सेवाओं में से चुनें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=get_main_menu_keyboard())

# 👑 ADMIN COMMAND: /admin
@bot.message_handler(commands=["admin"])
def command_admin_panel(m):
    if not is_admin_user(m.from_user.id):
        bot.reply_to(m, "⛔ आपके पास एडमिन एक्सेस नहीं है।")
        return

    conn = get_db()
    total_apps = conn.execute("SELECT COUNT(*) as count FROM applications").fetchone()["count"]
    pending_apps = conn.execute("SELECT COUNT(*) as count FROM applications WHERE status='proof_submitted'").fetchone()["count"]
    completed_apps = conn.execute("SELECT COUNT(*) as count FROM applications WHERE status='completed'").fetchone()["count"]
    conn.close()

    current_upi = get_setting("upi_id")

    text = (
        "👑 <b>Admin Control Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Total Applications:</b> {total_apps}\n"
        f"⏳ <b>Pending Approval:</b> {pending_apps}\n"
        f"✅ <b>Completed:</b> {completed_apps}\n\n"
        f"💳 <b>Current UPI ID:</b> <code>{current_upi}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "UPI ID बदलने के लिए कमांड भेजें:\n"
        "<code>/setupi yourname@upi</code>"
    )
    bot.reply_to(m, text)

# 💳 ADMIN COMMAND: /setupi <new_upi>
@bot.message_handler(commands=["setupi"])
def command_set_upi(m):
    if not is_admin_user(m.from_user.id):
        bot.reply_to(m, "⛔ आपके पास एडमिन एक्सेस नहीं है।")
        return

    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(m, "❌ कृपया नई UPI ID दें!\nउदाहरण: <code>/setupi flipkshop@axl</code>")
        return

    new_upi = args[1].strip()
    set_setting("upi_id", new_upi)
    bot.reply_to(m, f"✅ <b>UPI ID सफलतापूर्वक अपडेट हो गई:</b>\n<code>{new_upi}</code>")

# ==============================================================================
# 6. APPLICATION & DOCUMENT COLLECTION FLOW
# ==============================================================================

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

def prompt_next_document_step(chat_id):
    sess = user_sessions.get(chat_id)
    if not sess:
        return

    req_list = DOCUMENT_REQUIREMENTS.get(sess["category"], [])
    idx = sess["idx"]

    # सभी डॉक्यूमेंट मिलने के बाद पहले Confirmation/Summary दिखाएँ।
    # QR/UPI payment तभी दिखेगा जब यूज़र "Pay Now" दबाएगा।
    if idx >= len(req_list):
        show_application_summary_before_payment(chat_id)
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

# ==============================================================================\n# ==============================================================================
# 7. CONFIRMATION STEP, PAYMENT QR & ADMIN DONE WORKFLOW
# ==============================================================================

def show_application_summary_before_payment(cid):
    sess = user_sessions.get(cid)
    if not sess:
        return

    cat = sess.get("category", "N/A")
    plan = sess.get("plan", "Standard")
    amt = int(sess.get("amount", 0) or 0)

    if "48 घंटे" in plan:
        time_info = "⚡ 48 घंटे के अंदर"
    elif "3 दिन" in plan:
        time_info = "📅 3 कार्य दिवसों (Days) के अंदर"
    elif "Direct Digital" in plan:
        time_info = "💻 24 घंटे के अंदर"
    else:
        time_info = "⏳ सेवा के अनुसार"

    text = (
        "🎉 <b>आपके सभी डॉक्यूमेंट्स सफलतापूर्वक प्राप्त हो गए हैं!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>सर्विस:</b> {cat}\n"
        f"⚡ <b>चुना गया प्लान:</b> {plan}\n"
        f"⏳ <b>कार्य पूरा होने का समय:</b> {time_info}\n"
        f"💰 <b>कुल शुल्क:</b> ₹{amt}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "भुगतान पूरा करने और ऑर्डर कन्फर्म करने के लिए नीचे "
        "<b>Pay Now</b> बटन दबाएँ।"
    )

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton(
            f"💳 Pay Now (₹{amt} भुगतान करें)",
            callback_data="proceed_to_payment"
        )
    )
    bot.send_message(cid, text, reply_markup=mk)


@bot.callback_query_handler(func=lambda c: c.data == "proceed_to_payment")
def handle_pay_now_click(c):
    cid = c.message.chat.id

    # Callback को तुरंत acknowledge करें ताकि Telegram में button loading पर अटका न रहे।
    try:
        bot.answer_callback_query(c.id, "Payment details तैयार हो रहे हैं...")
    except Exception:
        pass

    try:
        sess = user_sessions.get(cid)

        if not sess or "category" not in sess or "amount" not in sess:
            bot.send_message(
                cid,
                "❌ आपका session expire हो गया है। कृपया /start करके दोबारा application शुरू करें।"
            )
            return

        # एक ही session में बार-बार Pay Now दबाने पर duplicate application न बने।
        app_id = sess.get("application_id")

        if app_id:
            conn = get_db()
            existing = conn.execute(
                "SELECT id, status FROM applications WHERE id=? AND user_id=?",
                (int(app_id), cid)
            ).fetchone()
            conn.close()

            if existing:
                app_id = int(existing["id"])
            else:
                app_id = None

        if not app_id:
            app_id = create_db_application_entry(cid)
            sess["application_id"] = app_id

        amt = int(sess["amount"])
        if amt <= 0:
            raise ValueError("Invalid payment amount")

        qr_buf, upi_url = generate_upi_qr_code(amt, app_id)

        if not upi_url or not upi_url.startswith("upi://pay?"):
            raise ValueError("Invalid UPI payment URL generated")

        upi_id = (get_setting("upi_id") or "").strip()
        if not upi_id or "@" not in upi_id:
            raise ValueError("UPI ID is not configured correctly")

        mk = types.InlineKeyboardMarkup(row_width=1)
        mk.add(
            types.InlineKeyboardButton(
                f"📲 Direct App से Pay करें (₹{amt})",
                url=upi_url
            ),
            types.InlineKeyboardButton(
                "📸 पेमेंट कर दिया, स्क्रीनशॉट भेजें",
                callback_data=f"sendproof|{app_id}"
            )
        )

        bot.send_photo(
            cid,
            qr_buf,
            caption=(
                f"💳 <b>पेमेंट / UPI QR कोड</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧾 <b>Application ID:</b> #{app_id}\n"
                f"💰 <b>देय राशि:</b> ₹{amt}\n"
                f"📌 <b>UPI ID:</b> <code>{upi_id}</code>\n\n"
                "1️⃣ QR कोड स्कैन करके पेमेंट करें।\n"
                "2️⃣ पेमेंट पूरा होने के बाद "
                "<b>'पेमेंट कर दिया, स्क्रीनशॉट भेजें'</b> दबाएँ।"
            ),
            reply_markup=mk
        )

    except Exception as e:
        logging.exception("Pay Now / UPI payment flow failed for user %s", cid)
        try:
            bot.send_message(
                cid,
                "⚠️ <b>Payment QR अभी जनरेट नहीं हो पाया।</b>\n\n"
                "QR बनाने की dependency/configuration में समस्या है। "
                "Admin को <code>requirements.txt</code> install करके bot restart करना होगा।"
            )
        except Exception:
            pass


def create_db_application_entry(cid):
    sess = user_sessions[cid]
    conn = get_db()
    cursor = conn.cursor()

    docs_json = json.dumps(sess.get("docs", {}))

    cursor.execute("""
        INSERT INTO applications
        (user_id, username, category, plan, amount, status, collected_docs, created_at)
        VALUES (?, ?, ?, ?, ?, 'awaiting_payment', ?, ?);
    """, (
        cid,
        sess.get("username", ""),
        sess["category"],
        sess["plan"],
        int(sess["amount"]),
        docs_json,
        utc_now_iso()
    ))

    app_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return app_id


@bot.callback_query_handler(func=lambda c: c.data.startswith("sendproof|"))
def handle_send_proof_click(c):
    bot.answer_callback_query(c.id)

    try:
        app_id = int(c.data.split("|", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(c.message.chat.id, "❌ Invalid Application ID.")
        return

    conn = get_db()
    row = conn.execute(
        "SELECT id, user_id, status FROM applications WHERE id=?",
        (app_id,)
    ).fetchone()
    conn.close()

    if not row or int(row["user_id"]) != c.message.chat.id:
        bot.send_message(c.message.chat.id, "❌ Application नहीं मिली।")
        return

    if row["status"] not in ("awaiting_payment", "proof_submitted"):
        bot.send_message(
            c.message.chat.id,
            "ℹ️ इस Application का payment step पहले ही process हो चुका है।"
        )
        return

    msg = bot.send_message(
        c.message.chat.id,
        f"📸 <b>Application #{app_id}</b>\n\n"
        "कृपया अपने भुगतान का स्क्रीनशॉट फोटो के रूप में भेजें:"
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
        bot.reply_to(
            m,
            "❌ यह फोटो नहीं है! कृपया केवल पेमेंट स्क्रीनशॉट फोटो भेजें।"
        )
        return

    app_id = sess["app_id"]

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM applications WHERE id=? AND user_id=?",
        (app_id, m.chat.id)
    ).fetchone()

    if not row:
        conn.close()
        bot.reply_to(m, "❌ Application नहीं मिली।")
        return

    conn.execute(
        "UPDATE applications SET status='proof_submitted' WHERE id=?",
        (app_id,)
    )
    conn.commit()
    conn.close()

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton(
            "✅ Done (Confirm Payment & Generate File)",
            callback_data=f"admin_done|{app_id}|{m.chat.id}"
        )
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                "🚨 <b>नया पेमेंट स्क्रीनशॉट प्राप्त हुआ!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧾 <b>Application ID:</b> #{app_id}\n"
                f"👤 <b>User ID:</b> <code>{m.chat.id}</code>\n"
                f"👤 <b>Username:</b> @{m.from_user.username or 'None'}\n\n"
                "स्क्रीनशॉट देखकर payment verify करें और "
                "<b>Done</b> दबाएँ:"
            )
            bot.forward_message(admin_id, m.chat.id, m.message_id)
            bot.send_message(
                admin_id,
                "👇 <b>Payment Action:</b>",
                reply_markup=mk
            )
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    bot.send_message(
        m.chat.id,
        "✅ आपका पेमेंट स्क्रीनशॉट एडमिन को भेज दिया गया है।\n"
        "Payment verification के बाद आपको confirmation मिल जाएगा।"
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_done|"))
def handle_admin_done_action(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized!", show_alert=True)
        return

    try:
        _, app_id_str, user_id_str = c.data.split("|")
        app_id = int(app_id_str)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        bot.answer_callback_query(c.id, "Invalid data!", show_alert=True)
        return

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM applications WHERE id=?",
        (app_id,)
    ).fetchone()

    if not row:
        bot.answer_callback_query(c.id, "Application not found!", show_alert=True)
        conn.close()
        return

    if row["status"] == "completed":
        bot.answer_callback_query(c.id, "Already completed.", show_alert=True)
        conn.close()
        return

    conn.execute(
        "UPDATE applications SET status='completed', completed_at=? WHERE id=?",
        (utc_now_iso(), app_id)
    )
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

    html_file_path = generate_application_html_file(
        app_id, user_id, docs_dict
    )

    bot.answer_callback_query(c.id, "Payment approved & HTML created!")
    bot.edit_message_text(
        f"✅ <b>Application #{app_id} Confirmed!</b>\n\n"
        "Payment verify कर दिया गया है और HTML file generate हो गई है।",
        c.message.chat.id,
        c.message.message_id
    )

    if html_file_path and os.path.exists(html_file_path):
        with open(html_file_path, "rb") as doc_file:
            bot.send_document(
                c.message.chat.id,
                doc_file,
                caption=f"📄 <b>Application #{app_id} HTML Report</b>"
            )

    user_sessions.pop(user_id, None)
    gc.collect()

    try:
        bot.send_message(
            user_id,
            f"🎉 <b>भुगतान स्वीकृत हो गया!</b>\n\n"
            f"आपकी <b>Application #{app_id}</b> स्वीकार कर ली गई है।"
        )
    except Exception as e:
        logging.error(f"Failed to notify user {user_id}: {e}")

# ==============================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"🌐 Health Check Server Running on Port {port}")
    server.serve_forever()

def start_memory_cleanup_task():
    while True:
        try:
            time.sleep(3600)
            gc.collect()
            logging.info("Render Free Tier RAM Cleanup Completed.")
        except Exception as e:
            logging.error(f"Cleanup thread error: {e}")

# ==============================================================================
# 9. MAIN SERVER ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    init_db()

    # Memory Cleanup Thread (RAM Optimization)
    cleanup_thread = threading.Thread(target=start_memory_cleanup_task, daemon=True, name="MemoryCleanupThread")
    cleanup_thread.start()

    # Telegram Bot Polling Thread
    def run_telegram_polling():
        logging.info("Starting Telegram Bot Polling...")
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception as e:
                logging.error(f"Polling Exception: {e}")
                time.sleep(5)

    poll_thread = threading.Thread(target=run_telegram_polling, daemon=True, name="BotPollingThread")
    poll_thread.start()

    # Lightweight HTTP Health Check Server
    run_dummy_server()
