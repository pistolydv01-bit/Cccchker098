import os
import sys
import json
import gc
import time
import sqlite3
import logging
import threading
import re
import zipfile
import html
import shutil
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot
from telebot import types
import qrcode
from google import genai
from google.genai import types as genai_types

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
COMPANION_MODEL = os.getenv("COMPANION_MODEL", GEMINI_MODEL).strip()

# ⚠️ अपनी न्यूमेरिक टेलीग्राम यूज़र आईडी यहाँ डालें
ADMIN_IDS = [7771292960, 6874667015]

# ⚠️ यहाँ अपनी चालू UPI ID डालें
DEFAULT_UPI = "flipkshop@axl"

PAYMENT_TIMEOUT_MINUTES = 10

if not BOT_TOKEN:
    logging.critical("FATAL: BOT_TOKEN environment variable is missing!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Initialize Gemini Client if API key is present
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("Gemini AI Client initialized successfully. Model: %s", GEMINI_MODEL)
        logging.info("Gemini retry protection: enabled (3 attempts, exponential backoff)")
    except Exception as e:
        logging.error(f"Failed to initialize Gemini Client: {e}")
else:
    logging.warning("GEMINI_API_KEY is not set. AI Support feature will be disabled.")

HELP_SUPPORT_SYSTEM_PROMPT = """
आप संदीप यादव जी के डिजिटल सेवा पोर्टल की आधिकारिक हेल्पलाइन सपोर्ट हैं।
आप केवल इसी पोर्टल की जाति, आय, निवास, NCL, PMS और आवेदन/पेमेंट से जुड़ी मदद करें।
यूज़र इसी पोर्टल पर आया है, इसलिए उसे अनावश्यक रूप से किसी दूसरे पोर्टल, साइबर कैफे या एजेंट के पास न भेजें।
जवाब सामान्यतः 2-6 छोटी लाइनों में दें; जरूरत हो तभी विस्तार करें।
सरल हिंदी रखें और जरूरी English term को brackets में साथ दें।
अगर यूज़र application delay, payment, verification, status या गलत जानकारी की शिकायत करे:
- पहले उसकी समस्या को समझें,
- Application ID/नाम पूछें अगर उपलब्ध नहीं है,
- जरूरत हो तो कहें: "कृपया स्क्रीनशॉट भेज दें, मैं इसे Admin तक पहुंचाने के लिए नोट कर रही हूँ।"
कभी भी payment success, government approval या document approval का झूठा दावा न करें।
यदि कोई पूछे कि आप कौन हैं, कहें: "मैं इस पोर्टल की हेल्पलाइन सपोर्ट हूँ।"

महत्वपूर्ण निर्देश:
आप अपने प्रत्येक उत्तर/जवाब के अंत में अनिवार्य रूप से एक खाली लाइन छोड़कर यह पंक्ति शामिल करें:
🏠 जरूरत हो तो नीचे /start दबाकर शुरुआत पर लौट सकते हैं।
"""

COMPANION_SYSTEM_PROMPT = """
आप "सिया" नाम की एक काल्पनिक डिजिटल दोस्त हैं।
आप friendly, warm और natural Hindi/Hinglish में बातचीत करती हैं।
जवाब छोटे, casual और context-aware रखें; जरूरत पर हल्के emojis इस्तेमाल करें।
आप कभी यह दावा न करें कि आप असली इंसान या असली लड़की हैं; पूछे जाने पर साफ बताएं कि आप एक डिजिटल/AI साथी हैं।
अश्लील या sexual बातचीत को आगे न बढ़ाएं और जरूरत पर विनम्र सीमा रखें।
"""

# Active chat sessions per user
ai_chat_sessions = {}
ai_chat_locks = {}
ai_last_request = {}
companion_chat_sessions = {}
companion_locks = {}
companion_last_request = {}

DOCUMENT_REQUIREMENTS = {
    "Jati / Aawas / Niwas": [
        "आधार कार्ड (Identity Proof / Aadhaar Card)",
        "पासपोर्ट साइज फोटो (Passport Size Photo)",
        "मोबाइल नंबर (Mobile Number)",
        "ईमेल पता (Email Address)",
        "पिता का पूरा नाम व पता (Father's Full Name & Address)"
    ],
    "Non-Creamy Layer (NCL)": [
        "आधार कार्ड (Identity Proof / Aadhaar Card)",
        "पासपोर्ट साइज फोटो (Passport Size Photo)",
        "आय विवरण / प्रमाण पत्र (Income Details / Certificate)",
        "जाति विवरण / प्रमाण पत्र (Caste Details / Certificate)",
        "मोबाइल नंबर व ईमेल (Mobile Number & Email)"
    ],
    "Post Matric Scholarship (PMS)": [
        "जाति प्रमाण पत्र विवरण (Caste Certificate Details)",
        "आय प्रमाण पत्र विवरण (Income Certificate Details)",
        "निवास प्रमाण पत्र विवरण (Residence Certificate Details)",
        "आधार कार्ड (Identity Proof / Aadhaar Card)",
        "10वीं/12वीं मार्कशीट विवरण (10th/12th Marksheet Details)",
        "बोनाफाइड प्रमाण पत्र विवरण (Bonafide Certificate Details)",
        "फीस रसीद विवरण (Fee Receipt Details)",
        "पासपोर्ट फोटो (Passport Photo)",
        "मोबाइल नंबर (Mobile Number)"
    ],
}

DEFAULT_SETTINGS = {
    "upi_id": DEFAULT_UPI,
    "jati_48h": "300",
    "jati_3d": "200",
    "jati_online": "120",
    "ncl": "150",
    "pms": "250",
    "discount_percent": "0",
}

# In-Memory Sessions
user_sessions = {}
payment_sessions = {}
admin_sessions = {}
BOT_STARTED_AT = datetime.now(timezone.utc)
polling_heartbeat = {"last_ok": None, "running": False}

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
        CREATE TABLE IF NOT EXISTS user_activity (
            user_id INTEGER NOT NULL,
            activity_date TEXT NOT NULL,
            PRIMARY KEY (user_id, activity_date)
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
    conn.execute(
        "INSERT OR IGNORE INTO user_activity (user_id, activity_date) VALUES (?, ?)",
        (tg_user.id, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    )
    conn.commit()
    conn.close()

def is_admin_user(user_id):
    return int(user_id) in ADMIN_IDS

def get_base_price(category, plan_key=None):
    if category == "Non-Creamy Layer (NCL)":
        return int(get_setting("ncl") or 150)
    if category == "Post Matric Scholarship (PMS)":
        return int(get_setting("pms") or 250)
    mapping = {"48h": "jati_48h", "3d": "jati_3d", "online": "jati_online"}
    return int(get_setting(mapping.get(plan_key, "jati_online")) or 120)

def calculate_price(category, plan_key=None):
    base = get_base_price(category, plan_key)
    discount = max(0, min(100, int(get_setting("discount_percent") or 0)))
    return max(0, int(round(base * (100 - discount) / 100)))

def price_label(category, plan_key=None):
    base = get_base_price(category, plan_key)
    final = calculate_price(category, plan_key)
    discount = int(get_setting("discount_percent") or 0)
    if discount:
        return f"₹{final} (₹{base} - {discount}% OFF)"
    return f"₹{base}"


# ==============================================================================
# 3. HTML & ZIP GENERATOR FOR COMPLETED APPLICATIONS
# ==============================================================================

def generate_application_html_file(app_id, user_id, docs_dict, output_dir='applications_files'):
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

    os.makedirs(output_dir, exist_ok=True)
    out_file_path = os.path.join(output_dir, f"application_{app_id}.html")

    with open(out_file_path, "w", encoding="utf-8") as f:
        f.write(html_markup)

    gc.collect()
    return out_file_path


def sanitize_folder_name(value, fallback="user"):
    value = (value or "").strip().lstrip("@")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.strip(" .")
    return value[:80] or fallback


def get_saved_username(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT username, first_name FROM users WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    conn.close()

    if row:
        username = (row["username"] or "").strip()
        first_name = (row["first_name"] or "").strip()

        if username:
            return username

        if first_name:
            return f"{first_name}_{user_id}"

    return f"user_{user_id}"


def download_telegram_file(file_id, destination_path):
    try:
        file_info = bot.get_file(file_id)
        file_bytes = bot.download_file(file_info.file_path)

        os.makedirs(os.path.dirname(destination_path), exist_ok=True)

        with open(destination_path, "wb") as out_file:
            out_file.write(file_bytes)

        return True
    except Exception as exc:
        logging.error("Telegram file download failed for %s: %s", file_id, exc, exc_info=True)
        return False


def build_application_folder(app_id, user_id, docs_dict):
    username = get_saved_username(user_id)
    safe_username = sanitize_folder_name(username, f"user_{user_id}")

    root_dir = os.path.join("applications_files", safe_username)
    app_dir = os.path.join(root_dir, f"Application_{app_id}")
    documents_dir = os.path.join(app_dir, "Documents")
    report_dir = os.path.join(app_dir, "Report")

    os.makedirs(documents_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)

    conn = get_db()
    app_record = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    conn.close()

    if not app_record:
        return None

    html_file_path = generate_application_html_file(app_id, user_id, docs_dict, output_dir=report_dir)

    download_errors = []
    saved_files = []

    if isinstance(docs_dict, dict):
        for index, (doc_name, doc_value) in enumerate(docs_dict.items(), 1):
            try:
                d_type, d_val = doc_value
            except Exception:
                download_errors.append(f"{doc_name}: invalid stored document value")
                continue

            safe_doc_name = sanitize_folder_name(doc_name, f"Document_{index}")

            if d_type == "text":
                text_path = os.path.join(documents_dir, f"{safe_doc_name}.txt")
                with open(text_path, "w", encoding="utf-8") as out_file:
                    out_file.write(str(d_val))
                saved_files.append(text_path)
                continue

            if d_type not in ("document", "photo"):
                download_errors.append(f"{doc_name}: unsupported document type {d_type}")
                continue

            extension = ".bin"
            try:
                file_info = bot.get_file(d_val)
                original_path = file_info.file_path or ""
                _, original_ext = os.path.splitext(original_path)

                if original_ext:
                    extension = original_ext.lower()
            except Exception as exc:
                logging.warning("Could not inspect Telegram file %s: %s", d_val, exc)

            if d_type == "photo" and extension == ".bin":
                extension = ".jpg"

            file_path = os.path.join(documents_dir, f"{safe_doc_name}{extension}")

            if download_telegram_file(d_val, file_path):
                saved_files.append(file_path)
            else:
                download_errors.append(f"{doc_name}: Telegram file could not be downloaded")

    summary_path = os.path.join(app_dir, "README.txt")

    with open(summary_path, "w", encoding="utf-8") as summary:
        clean_username = username.lstrip("@") or "None"
        summary.write(
            f"Application #{app_id}\n"
            f"Telegram User ID: {user_id}\n"
            f"Telegram Username: @{clean_username}\n"
            f"Folder Name: {safe_username}\n"
            f"Category: {app_record['category']}\n"
            f"Plan: {app_record['plan']}\n"
            f"Amount: INR {app_record['amount']}\n"
            f"Created: {format_readable_time(app_record['created_at'])}\n"
            f"Completed: {format_readable_time(app_record['completed_at'])}\n\n"
            f"Downloaded documents: {len(saved_files)}\n"
            f"Download errors: {len(download_errors)}\n"
        )

        if download_errors:
            summary.write("\nErrors:\n")
            for error in download_errors:
                summary.write(f"- {error}\n")

    return {
        "username": safe_username,
        "app_dir": app_dir,
        "html_file_path": html_file_path,
        "saved_files": saved_files,
        "download_errors": download_errors,
    }


def create_application_zip(app_id, user_id, docs_dict):
    folder_info = build_application_folder(app_id, user_id, docs_dict)

    if not folder_info:
        return None

    username = folder_info["username"]
    app_dir = folder_info["app_dir"]

    os.makedirs("applications_files", exist_ok=True)
    zip_path = os.path.join("applications_files", f"{username}_Application_{app_id}.zip")

    if os.path.exists(zip_path):
        os.remove(zip_path)

    base_dir = os.path.dirname(os.path.dirname(app_dir))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
        for current_root, dirnames, filenames in os.walk(app_dir):
            if not dirnames and not filenames:
                archive_dir = os.path.relpath(current_root, base_dir)
                zip_file.writestr(archive_dir.rstrip("/") + "/", "")

            for filename in filenames:
                full_path = os.path.join(current_root, filename)
                archive_name = os.path.relpath(full_path, base_dir)
                zip_file.write(full_path, archive_name)

    return zip_path, folder_info


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
        types.InlineKeyboardButton("📜 जाति / आवास / निवास", callback_data="cat|Jati / Aawas / Niwas"),
        types.InlineKeyboardButton("📄 नॉन-क्रीमी लेयर (NCL)", callback_data="cat|Non-Creamy Layer (NCL)"),
        types.InlineKeyboardButton("🎓 पोस्ट मैट्रिक छात्रवृत्ति (PMS)", callback_data="cat|Post Matric Scholarship (PMS)"),
        types.InlineKeyboardButton("🙋‍♂️ हेल्पलाइन सपोर्ट", callback_data="support_start"),
        types.InlineKeyboardButton("💬 दोस्त से बात करें", callback_data="companion_start")
    )
    return mk

# ==============================================================================
# 5. USER, HELPLINE & COMPANION HANDLERS
# ==============================================================================

@bot.message_handler(commands=["start"])
def command_start(m):
    db_upsert_user(m.from_user)
    user_sessions[m.chat.id] = {
        "docs": {},
        "idx": 0,
        "username": m.from_user.username or ""
    }
    text = (
        f"✨ <b>नमस्ते {m.from_user.first_name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "डिजिटल सेवा पोर्टल पर आपका स्वागत है।\n\n"
        "कृपया नीचे दी गई सेवाओं में से कोई सेवा चुनें या हेल्पलाइन सपोर्ट से बात करें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=get_main_menu_keyboard())

# --- हेल्पलाइन सपोर्ट + दोस्त से बात करें ---

@bot.callback_query_handler(func=lambda c: c.data == "support_start")
def start_support(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id

    if not ai_client:
        bot.send_message(cid, "⚠️ हेल्पलाइन अभी उपलब्ध नहीं है। कृपया थोड़ी देर बाद फिर कोशिश करें।")
        return

    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "support"

    msg_text = (
        "🙋‍♂️ <b>हेल्पलाइन सपोर्ट</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "नमस्ते! मैं इस पोर्टल की हेल्पलाइन सपोर्ट हूँ।\n\n"
        "आप अपनी समस्या सीधे लिखें। अगर आवेदन, पेमेंट या स्टेटस में दिक्कत है "
        "तो Application ID के साथ स्क्रीनशॉट भी भेज सकते हैं।"
    )

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("📸 स्क्रीनशॉट भेजें", callback_data="support_screenshot"),
        types.InlineKeyboardButton("🔙 मुख्य मेन्यू", callback_data="back_to_main_menu")
    )
    bot.send_message(cid, msg_text, reply_markup=mk)


@bot.callback_query_handler(func=lambda c: c.data == "support_screenshot")
def support_screenshot_prompt(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id
    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "support_photo"

    msg = bot.send_message(
        cid,
        "📸 कृपया अपनी समस्या का साफ़ स्क्रीनशॉट भेजें।\n"
        "मैं उसे Admin तक पहुंचाने के लिए नोट कर दूँगी।"
    )
    bot.register_next_step_handler(msg, process_support_screenshot)


def process_support_screenshot(m):
    cid = m.chat.id
    sess = user_sessions.get(cid, {})
    if not sess or sess.get("mode") not in ("support_photo", "support"):
        bot.reply_to(m, "सेशन समाप्त हो गया है। कृपया हेल्पलाइन सपोर्ट दोबारा खोलें।")
        return

    if m.content_type != "photo":
        bot.reply_to(m, "❌ कृपया केवल फोटो/स्क्रीनशॉट भेजें।")
        return

    caption = (
        "🚨 <b>Helpline Screenshot Alert</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User ID: <code>{m.chat.id}</code>\n"
        f"👤 Username: @{m.from_user.username or 'None'}\n"
        f"📝 Name: {html.escape((m.from_user.first_name or '') + ' ' + (m.from_user.last_name or ''))}\n"
        "यूज़र ने हेल्पलाइन में समस्या का स्क्रीनशॉट भेजा है।"
    )

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, caption)
            bot.forward_message(admin_id, m.chat.id, m.message_id)
            sent += 1
        except Exception as exc:
            logging.error("Support screenshot admin alert failed for %s: %s", admin_id, exc)

    user_sessions[cid]["mode"] = "support"
    if sent:
        bot.send_message(
            cid,
            "✅ स्क्रीनशॉट Admin तक पहुंचा दिया गया है।\n"
            "अब अपनी समस्या 1-2 लाइन में लिख दें, ताकि मैं सही जानकारी दे सकूँ।",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🔙 मुख्य मेन्यू", callback_data="back_to_main_menu")
            )
        )
    else:
        bot.send_message(
            cid,
            "⚠️ अभी Admin तक स्क्रीनशॉट नहीं पहुंच पाया। कृपया थोड़ी देर बाद फिर भेजें।"
        )


@bot.callback_query_handler(func=lambda c: c.data == "companion_start")
def start_companion(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id

    if not ai_client:
        bot.send_message(cid, "⚠️ दोस्त से बात करने वाली सेवा अभी उपलब्ध नहीं है।")
        return

    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "companion"

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("🔙 मुख्य मेन्यू", callback_data="back_to_main_menu"))

    bot.send_message(
        cid,
        "💬 <b>दोस्त से बात करें</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "नमस्ते 😊 मैं <b>सिया</b> हूँ।\n\n"
        "आप मुझसे आराम से Hindi/Hinglish में बात कर सकते हैं। "
        "अपना message भेजिए, मैं उसी बातचीत के context में जवाब दूँगी।",
        reply_markup=mk
    )


@bot.callback_query_handler(func=lambda c: c.data == "back_to_main_menu")
def back_to_main_menu_handler(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id
    if cid in user_sessions:
        user_sessions[cid].pop("mode", None)
    ai_chat_sessions.pop(cid, None)
    companion_chat_sessions.pop(cid, None)

    text = "✨ <b>मुख्य मेन्यू</b>\n━━━━━━━━━━━━━━━━━━━━━\nकृपया नीचे दी गई सेवाओं में से चुनें 👇"
    bot.send_message(cid, text, reply_markup=get_main_menu_keyboard())


def _send_gemini_chat(chat_store, locks, last_request_store, message, mode_key, system_prompt, model, temperature=0.5):
    cid = message.chat.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    if user_text.startswith("/"):
        user_sessions.get(cid, {}).pop("mode", None)
        return

    if not ai_client:
        bot.reply_to(message, "⚠️ यह सेवा अभी उपलब्ध नहीं है।")
        return

    lock = locks.setdefault(cid, threading.Lock())
    if not lock.acquire(blocking=False):
        bot.reply_to(message, "⏳ आपका पिछला message अभी process हो रहा है। कृपया 2-3 सेकंड रुकें।")
        return

    try:
        bot.send_chat_action(cid, "typing")

        now = time.monotonic()
        previous = last_request_store.get(cid, 0.0)
        wait_for = 1.2 - (now - previous)
        if wait_for > 0:
            time.sleep(wait_for)
        last_request_store[cid] = time.monotonic()

        if cid not in chat_store:
            chat_store[cid] = ai_client.chats.create(
                model=model,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature
                )
            )

        answer = None
        last_error = None

        for attempt in range(1, 4):
            try:
                response = chat_store[cid].send_message(user_text)
                answer = getattr(response, "text", None)
                if not answer:
                    raise RuntimeError("Gemini returned an empty response.")
                break
            except Exception as exc:
                last_error = exc
                error_text = str(exc).upper()
                temporary = any(code in error_text for code in (
                    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                    "500", "502", "504", "DEADLINE"
                ))

                logging.warning(
                    "%s request failed for user %s (attempt %s/3): %s",
                    mode_key, cid, attempt, exc
                )

                if not temporary or attempt >= 3:
                    break

                chat_store.pop(cid, None)
                time.sleep(2 ** (attempt - 1))
                chat_store[cid] = ai_client.chats.create(
                    model=model,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=temperature
                    )
                )

        if not answer:
            chat_store.pop(cid, None)
            error_text = str(last_error or "").upper()
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                reply = "⏳ अभी सेवा की request limit थोड़ी देर के लिए पूरी हो गई है। 20-30 सेकंड बाद फिर भेजें।"
            elif "404" in error_text or "NOT_FOUND" in error_text:
                reply = f"⚠️ चुना हुआ मॉडल <code>{html.escape(model)}</code> उपलब्ध नहीं है। Render में model setting check करें।"
            else:
                reply = "⚠️ अभी server response नहीं मिला। Automatic retry किया गया है; कुछ सेकंड बाद फिर कोशिश करें।"
            bot.reply_to(message, reply)
            return

        bot.reply_to(message, answer)

    except Exception as exc:
        logging.exception("%s handler error for user %s: %s", mode_key, cid, exc)
        chat_store.pop(cid, None)
        try:
            bot.reply_to(message, "⚠️ अभी connection में समस्या है। कुछ सेकंड बाद फिर कोशिश करें।")
        except Exception:
            pass
    finally:
        lock.release()


@bot.message_handler(func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "support" and m.content_type == "text")
def handle_support_message(m):
    _send_gemini_chat(
        ai_chat_sessions, ai_chat_locks, ai_last_request,
        m, "Helpline", HELP_SUPPORT_SYSTEM_PROMPT, GEMINI_MODEL, 0.5
    )


@bot.message_handler(func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "support" and m.content_type == "photo")
def handle_support_photo(m):
    process_support_screenshot(m)


@bot.message_handler(func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "companion" and m.content_type == "text")
def handle_companion_message(m):
    _send_gemini_chat(
        companion_chat_sessions, companion_locks, companion_last_request,
        m, "Companion", COMPANION_SYSTEM_PROMPT, COMPANION_MODEL, 0.8
    )

# 👑 ADMIN COMMAND: /admin
@bot.message_handler(commands=["admin"])
def command_admin_panel(m):
    if not is_admin_user(m.from_user.id):
        bot.reply_to(m, "⛔ आपके पास एडमिन एक्सेस नहीं है।")
        return
    send_admin_panel(m.chat.id)

def admin_reply_keyboard():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        types.KeyboardButton("📊 User Stats"),
        types.KeyboardButton("📢 Broadcast"),
        types.KeyboardButton("🤖 Bot Status"),
        types.KeyboardButton("💰 Prices"),
        types.KeyboardButton("💳 UPI Settings"),
        types.KeyboardButton("🔄 Refresh Admin")
    )
    return mk

def get_user_stats():
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) c FROM users WHERE banned=0").fetchone()["c"]
    today_users = conn.execute("SELECT COUNT(*) c FROM user_activity WHERE activity_date=?", (today,)).fetchone()["c"]
    month_users = conn.execute("SELECT COUNT(DISTINCT user_id) c FROM user_activity WHERE activity_date>=?", (month_start,)).fetchone()["c"]
    rows = conn.execute("SELECT substr(activity_date,1,7) month, COUNT(DISTINCT user_id) users FROM user_activity GROUP BY substr(activity_date,1,7) ORDER BY month DESC LIMIT 12").fetchall()
    conn.close()
    return total, today_users, month_users, [(r["month"], r["users"]) for r in rows]

def send_admin_panel(chat_id):
    total, today_users, month_users, _ = get_user_stats()
    conn = get_db()
    total_apps = conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) c FROM applications WHERE status='proof_submitted'").fetchone()["c"]
    completed = conn.execute("SELECT COUNT(*) c FROM applications WHERE status='completed'").fetchone()["c"]
    conn.close()
    text = (
        "👑 <b>Admin Control Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total users: <b>{total}</b>\n"
        f"📅 इस महीने active users: <b>{month_users}</b>\n"
        f"📆 आज active users: <b>{today_users}</b>\n"
        f"📦 Applications: <b>{total_apps}</b> | ⏳ Pending: <b>{pending}</b> | ✅ Done: <b>{completed}</b>\n\n"
        f"💳 UPI: <code>{get_setting('upi_id')}</code>\n"
        f"🏷️ Discount: <b>{get_setting('discount_percent')}%</b>\n"
        "नीचे के बटन से सब कुछ जल्दी manage करें।"
    )
    bot.send_message(chat_id, text, reply_markup=admin_reply_keyboard())

def send_price_panel(chat_id):
    d = int(get_setting("discount_percent") or 0)
    text = (
        "💰 <b>Price Control</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"Jati 48h: <b>{price_label('Jati / Aawas / Niwas','48h')}</b>\n"
        f"Jati 3d: <b>{price_label('Jati / Aawas / Niwas','3d')}</b>\n"
        f"Jati Online: <b>{price_label('Jati / Aawas / Niwas','online')}</b>\n"
        f"NCL: <b>{price_label('Non-Creamy Layer (NCL)')}</b>\n"
        f"PMS: <b>{price_label('Post Matric Scholarship (PMS)')}</b>\n"
        f"Global discount: <b>{d}%</b>\n\n"
        "किसी price को +/− ₹10 करें, या direct amount भेजें।"
    )
    mk = types.InlineKeyboardMarkup(row_width=3)
    for key, label in [("jati_48h","Jati 48h"),("jati_3d","Jati 3d"),("jati_online","Jati Online"),("ncl","NCL"),("pms","PMS")]:
        mk.add(types.InlineKeyboardButton(f"➖ {label}", callback_data=f"price_dec|{key}"), types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"price_set|{key}"), types.InlineKeyboardButton(f"➕ {label}", callback_data=f"price_inc|{key}"))
    mk.add(types.InlineKeyboardButton("🏷️ Discount −5%", callback_data="discount|-5"), types.InlineKeyboardButton("🏷️ Discount +5%", callback_data="discount|5"), types.InlineKeyboardButton("✏️ Set Discount", callback_data="discount|set"))
    bot.send_message(chat_id, text, reply_markup=mk)

def send_bot_status(chat_id):
    conn = get_db()
    try:
        conn.execute("SELECT 1").fetchone()
        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        db_ok = True
    except Exception:
        total, db_ok = 0, False
    conn.close()
    uptime = datetime.now(timezone.utc) - BOT_STARTED_AT
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)
    ai_status = f"🟢 Connected ({GEMINI_MODEL})" if ai_client else "🔴 Disabled / Missing Key"
    bot.send_message(chat_id, "🤖 <b>Bot Status</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Process: <b>Running</b>\n📡 Polling thread: <b>{'Running' if polling_heartbeat.get('running') else 'Starting/Unknown'}</b>\n"
        f"💓 Last polling cycle: <b>{polling_heartbeat.get('last_ok') or 'N/A'}</b>\n🗄️ Database: <b>{'OK' if db_ok else 'ERROR'}</b>\n"
        f"🤖 Gemini AI: <b>{ai_status}</b>\n"
        f"👥 Registered users: <b>{total}</b>\n⏱️ Uptime: <b>{hours}h {mins}m</b>\n💳 UPI: <code>{get_setting('upi_id')}</code>")

def send_user_stats(chat_id):
    total, today, month, rows = get_user_stats()
    lines = "\n".join(f"• {m}: {u} active users" for m, u in rows) or "कोई monthly activity नहीं।"
    bot.send_message(chat_id, "📊 <b>User Analytics</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total registered: <b>{total}</b>\n📆 Today active: <b>{today}</b>\n🗓️ This month active: <b>{month}</b>\n\n<b>Last 12 months:</b>\n" + lines)

def broadcast_to_users(text):
    conn = get_db()
    users = conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    conn.close()
    sent = failed = 0
    for row in users:
        try:
            bot.send_message(int(row["user_id"]), text)
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed

@bot.message_handler(commands=["broadcast"])
def command_broadcast(m):
    if not is_admin_user(m.from_user.id):
        return
    payload = m.text.partition(" ")[2].strip()
    if not payload:
        admin_sessions[m.from_user.id] = "broadcast"
        bot.reply_to(m, "📢 Broadcast mode चालू है। अब अगला message भेजें।", reply_markup=admin_reply_keyboard())
        return
    sent, failed = broadcast_to_users(payload)
    bot.reply_to(m, f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}")

@bot.message_handler(commands=["setupi"])
def command_set_upi(m):
    if not is_admin_user(m.from_user.id):
        bot.reply_to(m, "⛔ आपके पास एडमिन एक्सेस नहीं है।")
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(m, "❌ उदाहरण: <code>/setupi flipkshop@axl</code>")
        return
    new_upi = args[1].strip()
    set_setting("upi_id", new_upi)
    bot.reply_to(m, f"✅ UPI ID अपडेट हो गई:\n<code>{new_upi}</code>", reply_markup=admin_reply_keyboard())

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and m.text in {"📊 User Stats","📢 Broadcast","🤖 Bot Status","💰 Prices","💳 UPI Settings","🔄 Refresh Admin"})
def admin_menu_actions(m):
    uid = m.from_user.id
    if m.text == "📊 User Stats": send_user_stats(m.chat.id)
    elif m.text == "📢 Broadcast":
        admin_sessions[uid] = "broadcast"
        bot.reply_to(m, "📢 अब अगला text message भेजें। वही सभी registered users को भेजा जाएगा।\nCancel के लिए <code>/admin</code> भेजें।")
    elif m.text == "🤖 Bot Status": send_bot_status(m.chat.id)
    elif m.text == "💰 Prices": send_price_panel(m.chat.id)
    elif m.text == "💳 UPI Settings": bot.send_message(m.chat.id, f"💳 Current UPI: <code>{get_setting('upi_id')}</code>\nबदलने के लिए <code>/setupi yourupi@bank</code>")
    else: send_admin_panel(m.chat.id)

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and admin_sessions.get(m.from_user.id) == "broadcast")
def admin_broadcast_message(m):
    if m.text and m.text.strip() == "/admin":
        admin_sessions.pop(m.from_user.id, None)
        send_admin_panel(m.chat.id)
        return
    admin_sessions.pop(m.from_user.id, None)
    if m.content_type != "text":
        bot.reply_to(m, "❌ अभी केवल text broadcast supported है।")
        return
    sent, failed = broadcast_to_users(m.text.strip())
    bot.reply_to(m, f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}", reply_markup=admin_reply_keyboard())

@bot.callback_query_handler(func=lambda c: c.data.startswith("price_"))
def handle_price_controls(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized", show_alert=True); return
    action, key = c.data.split("|", 1)
    current = int(get_setting(key) or 0)
    if action == "price_inc": set_setting(key, current + 10)
    elif action == "price_dec": set_setting(key, max(0, current - 10))
    else:
        admin_sessions[c.from_user.id] = f"set_price:{key}"
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, f"✏️ <b>{key}</b> की नई base price ₹ में भेजें।")
        return
    bot.answer_callback_query(c.id, "Price updated")
    send_price_panel(c.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("discount|"))
def handle_discount(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized", show_alert=True); return
    value = c.data.split("|", 1)[1]
    if value == "set":
        admin_sessions[c.from_user.id] = "set_discount"
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "✏️ Discount percentage भेजें (0-100)।")
        return
    current = int(get_setting("discount_percent") or 0)
    set_setting("discount_percent", max(0, min(100, current + int(value))))
    bot.answer_callback_query(c.id, "Discount updated")
    send_price_panel(c.message.chat.id)

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and str(admin_sessions.get(m.from_user.id, "")).startswith("set_price:"))
def admin_set_price(m):
    key = str(admin_sessions.pop(m.from_user.id)).split(":", 1)[1]
    try:
        value = int(m.text.strip())
        if value < 0 or value > 1000000: raise ValueError
        set_setting(key, value)
        bot.reply_to(m, f"✅ {key} base price = ₹{value}")
        send_price_panel(m.chat.id)
    except Exception:
        bot.reply_to(m, "❌ 0 से 1000000 के बीच valid amount भेजें।")

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and admin_sessions.get(m.from_user.id) == "set_discount")
def admin_set_discount(m):
    admin_sessions.pop(m.from_user.id, None)
    try:
        value = int(m.text.strip())
        if not 0 <= value <= 100: raise ValueError
        set_setting("discount_percent", value)
        bot.reply_to(m, f"✅ Global discount = {value}%")
        send_price_panel(m.chat.id)
    except Exception:
        bot.reply_to(m, "❌ Discount 0 से 100 के बीच होना चाहिए।")


# ==============================================================================
# 5B. ADMIN REPLY SYSTEM (ALL MEDIA SUPPORT & USER PROMPT)
# ==============================================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_msg_prompt|"))
def handle_admin_msg_prompt(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized!", show_alert=True)
        return

    try:
        user_id = int(c.data.split("|", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(c.id, "Invalid User ID!", show_alert=True)
        return

    bot.answer_callback_query(c.id)
    
    msg_text = (
        f"💬 <b>User ID: <code>{user_id}</code></b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "नीचे इस मैसेज पर <b>Reply</b> करके अपना मैसेज, फोटो, PDF या वॉइस भेजें।\n"
        "बोट सीधे इस यूज़र को डिलीवर कर देगा।"
    )
    bot.send_message(c.message.chat.id, msg_text)


@bot.message_handler(content_types=['text', 'photo', 'document', 'voice', 'audio', 'video', 'video_note', 'sticker'], func=lambda m: is_admin_user(m.from_user.id) and m.reply_to_message is not None)
def handle_admin_reply_to_user(m):
    orig_msg = m.reply_to_message
    target_user_id = None

    search_text = (orig_msg.text or "") + " " + (orig_msg.caption or "")
    match = re.search(r"User ID:\s*<code>?(\d+)</code>?", search_text, re.IGNORECASE)
    if match:
        target_user_id = int(match.group(1))
    elif orig_msg.forward_from:
        target_user_id = orig_msg.forward_from.id

    if not target_user_id:
        bot.reply_to(m, "❌ इस मैसेज से यूज़र आईडी की पहचान नहीं हो सकी। कृपया सुनिश्चित करें कि आप नोटीफिकेशन वाले मैसेज पर ही Reply कर रहे हैं।")
        return

    try:
        content_type = m.content_type

        if content_type == "text":
            bot.send_message(target_user_id, m.text)

        elif content_type == "photo":
            bot.send_photo(target_user_id, m.photo[-1].file_id, caption=m.caption)

        elif content_type == "document":
            bot.send_document(target_user_id, m.document.file_id, caption=m.caption)

        elif content_type == "voice":
            bot.send_voice(target_user_id, m.voice.file_id, caption=m.caption)

        elif content_type == "audio":
            bot.send_audio(target_user_id, m.audio.file_id, caption=m.caption)

        elif content_type == "video":
            bot.send_video(target_user_id, m.video.file_id, caption=m.caption)

        elif content_type == "video_note":
            bot.send_video_note(target_user_id, m.video_note.file_id)

        elif content_type == "sticker":
            bot.send_sticker(target_user_id, m.sticker.file_id)

        else:
            bot.copy_message(target_user_id, m.chat.id, m.message_id)

        bot.reply_to(m, f"✅ संदेश सफल रूप से यूज़र (ID: <code>{target_user_id}</code>) को भेज दिया गया है।")

    except Exception as e:
        logging.error(f"Failed to deliver admin reply to user {target_user_id}: {e}")
        bot.reply_to(m, f"❌ संदेश भेजने में त्रुटि हुई:\n<code>{html.escape(str(e))}</code>")


# ==============================================================================
# 6. APPLICATION & DOCUMENT COLLECTION FLOW
# ==============================================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def handle_category_selection(c):
    bot.answer_callback_query(c.id)
    cat_name = c.data.split("|", 1)[1]

    sess = user_sessions.setdefault(c.message.chat.id, {"docs": {}, "idx": 0})
    sess.update(category=cat_name, docs={}, idx=0)
    sess.pop("mode", None)

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

# ==============================================================================
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
                "QR बनाने की dependency/configuration में समस्या है।"
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

    # Admin buttons updated with "💬 इस यूज़र को मैसेज भेजें"
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton(
            "✅ Done (Confirm Payment & Generate File)",
            callback_data=f"admin_done|{app_id}|{m.chat.id}"
        ),
        types.InlineKeyboardButton(
            "💬 इस यूज़र को मैसेज भेजें",
            callback_data=f"admin_msg_prompt|{m.chat.id}"
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
                f"👇 <b>Payment Action (User ID: <code>{m.chat.id}</code>):</b>",
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

    zip_result = create_application_zip(
        app_id,
        user_id,
        docs_dict
    )

    bot.answer_callback_query(
        c.id,
        "Payment approved & ZIP created!"
    )

    bot.edit_message_text(
        f"✅ <b>Application #{app_id} Confirmed!</b>\n\n"
        "Payment verify कर दिया गया है और "
        "username वाला ZIP folder तैयार हो गया है।",
        c.message.chat.id,
        c.message.message_id
    )

    if zip_result:
        zip_file_path, folder_info = zip_result

        if zip_file_path and os.path.exists(zip_file_path):
            with open(
                zip_file_path,
                "rb"
            ) as zip_file:
                bot.send_document(
                    c.message.chat.id,
                    zip_file,
                    caption=(
                        f"📦 <b>Application #{app_id} ZIP</b>\n"
                        f"📁 Folder: <code>{html.escape(folder_info['username'])}</code>\n"
                        "Documents + Report + README शामिल हैं।"
                    )
                )

        if folder_info.get("download_errors"):
            bot.send_message(
                c.message.chat.id,
                "⚠️ कुछ files ZIP में download नहीं हो पाईं। "
                "README.txt में details दी गई हैं।"
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
# 8. HEALTH CHECK & BACKGROUND THREADS
# ==============================================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/health":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        if self.path.rstrip("/") != "/health":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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
        polling_heartbeat["running"] = True
        while True:
            try:
                polling_heartbeat["last_ok"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception as e:
                polling_heartbeat["running"] = False
                logging.error(f"Polling Exception: {e}")
                time.sleep(5)
                polling_heartbeat["running"] = True

    poll_thread = threading.Thread(target=run_telegram_polling, daemon=True, name="BotPollingThread")
    poll_thread.start()

    # Lightweight HTTP Health Check Server
    run_dummy_server()
