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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
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

# System Prompt for Priya (AI Assistant) - Optimized for Low Token Usage
PRIYA_SYSTEM_PROMPT = """
आपका नाम 'प्रिया' है। आप संदीप यादव जी की आधिकारिक डिजिटल असिस्टेंट हैं।
आपका काम जाति, आय, निवास प्रमाण पत्र, NCL, और PMS ऑनलाइन सेवाओं से जुड़े सवालों के सही जवाब देना है।
हमेशा एक विनम्र, समझदार और सहायक लड़की/महिला के रूप में बात करें।
यदि कोई पूछे कि आप कौन हैं या संदीप कौन हैं, तो बताएं कि आप संदीप जी के इस ऑनलाइन पोर्टल की सपोर्ट असिस्टेंट हैं।
केवल काम और ऑनलाइन सेवाओं से संबंधित बातें करें।
"""

# Store active Gemini Chat sessions per user

# --- AI COMPANION (FICTIONAL CHARACTER) ---
COMPANION_SYSTEM_PROMPT = """
आप "सिया" नाम की एक काल्पनिक companion हैं।
आप friendly, warm और natural female-friend style में हिंदी/Hinglish में बात करती हैं।
जवाब conversational और context-aware रखें, जरूरत पर हल्का humor और emojis इस्तेमाल करें।
आप कभी यह दावा नहीं करें कि आप असली इंसान या असली लड़की हैं; जरूरत पड़ने पर साफ बताएं कि आप AI हैं।
अश्लील या sexual बातचीत को आगे न बढ़ाएं और जरूरत पड़ने पर विनम्र boundary रखें।
"""
companion_chat_sessions = {}
companion_users = set()
companion_locks = {}
companion_last_request = {}

def clear_companion_session(user_id):
    companion_chat_sessions.pop(user_id, None)

def companion_reply(message):
    if not ai_client:
        bot.reply_to(message, "⚠️ AI Companion अभी उपलब्ध नहीं है।")
        return

    uid = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    lock = companion_locks.setdefault(uid, threading.Lock())
    if not lock.acquire(blocking=False):
        bot.reply_to(message, "⏳ एक सेकंड, आपका पिछला message अभी process हो रहा है।")
        return

    try:
        last = companion_last_request.get(uid, 0.0)
        gap = 1.2 - (time.monotonic() - last)
        if gap > 0:
            time.sleep(gap)
        companion_last_request[uid] = time.monotonic()

        if uid not in companion_chat_sessions:
            companion_chat_sessions[uid] = ai_client.chats.create(
                model=COMPANION_MODEL,
                config=genai_types.GenerateContentConfig(
                    system_instruction=COMPANION_SYSTEM_PROMPT,
                    temperature=0.8
                )
            )

        answer = None
        for attempt in range(1, 4):
            try:
                response = companion_chat_sessions[uid].send_message(user_text)
                answer = getattr(response, "text", None)
                if answer:
                    break
                raise RuntimeError("Empty companion response")
            except Exception as exc:
                logging.warning(
                    "Companion request failed user=%s attempt=%s/3: %s",
                    uid, attempt, exc
                )
                clear_companion_session(uid)
                error_text = str(exc).upper()
                temporary = any(x in error_text for x in (
                    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                    "500", "502", "504", "DEADLINE"
                ))
                if not temporary or attempt >= 3:
                    break
                time.sleep(2 ** (attempt - 1))
                companion_chat_sessions[uid] = ai_client.chats.create(
                    model=COMPANION_MODEL,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=COMPANION_SYSTEM_PROMPT,
                        temperature=0.8
                    )
                )

        if answer:
            bot.reply_to(message, answer)
        else:
            bot.reply_to(
                message,
                "⚠️ अभी AI Companion response नहीं दे पा रही है। थोड़ी देर बाद फिर कोशिश करें।"
            )
    finally:
        lock.release()

ai_chat_sessions = {}
ai_chat_locks = {}
ai_last_request = {}

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
    "discount_percent": "0",
}

# In-Memory Sessions
user_sessions = {}
payment_sessions = {}
admin_sessions = {}
# Active human/admin support chats: user_id -> True
support_active_users = set()
# Admin reply target: admin_id -> user_id
support_admin_targets = {}
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
# 3. HTML GENERATOR FOR COMPLETED APPLICATIONS
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


# ==============================================================================
# 3B. APPLICATION FOLDER / ZIP EXPORT
# ==============================================================================

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
        logging.error(
            "Telegram file download failed for %s: %s",
            file_id,
            exc,
            exc_info=True
        )
        return False


def build_application_folder(app_id, user_id, docs_dict):
    username = get_saved_username(user_id)
    safe_username = sanitize_folder_name(
        username,
        f"user_{user_id}"
    )

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

    html_file_path = generate_application_html_file(
        app_id,
        user_id,
        docs_dict,
        output_dir=report_dir
    )

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
        types.InlineKeyboardButton("📜 Jati / Aawas / Niwas", callback_data="cat|Jati / Aawas / Niwas"),
        types.InlineKeyboardButton("📄 Non-Creamy Layer (NCL)", callback_data="cat|Non-Creamy Layer (NCL)"),
        types.InlineKeyboardButton("🎓 Post Matric Scholarship (PMS)", callback_data="cat|Post Matric Scholarship (PMS)"),
        types.InlineKeyboardButton("🙋‍♂️ हेल्पलाइन सपोर्ट", callback_data="ai_support_start"),
        types.InlineKeyboardButton("💬 दोस्त से बात करें", callback_data="ai_companion_start")
    )
    return mk

# ==============================================================================
# 5. USER, ADMIN & GEMINI AI HANDLERS
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
        "कृपया नीचे दी गई सेवाओं में से चुनें 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=get_main_menu_keyboard())

# --- GEMINI AI ASSISTANT (PRIYA) INTEGRATION ---


@bot.callback_query_handler(func=lambda c: c.data == "ai_companion_start")
def start_ai_companion(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id
    if not ai_client:
        bot.send_message(cid, "⚠️ AI Companion अभी उपलब्ध नहीं है।")
        return

    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "ai_companion"

    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(
        "🔙 मुख्य मेन्यू | Main Menu",
        callback_data="back_to_main_menu"
    ))
    bot.send_message(
        cid,
        "💬 <b>दोस्त से बात करें</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "नमस्ते 😊 मैं <b>सिया</b> हूँ।\n\n"
        "आप मुझसे casual Hindi/Hinglish में बात कर सकते हैं। "
        "बस अपना message भेजिए, मैं बातचीत उसी context में आगे बढ़ाऊँगी।\n\n"
        "😊 आराम से बात कीजिए, मैं आपकी बात सुनूँगी।",
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data == "ai_support_start")
def start_ai_support(c):
    """Start the portal helpline. User messages are also sent to admins for review/reply."""
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id
    user = c.from_user
    db_upsert_user(user)

    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "support_chat"
    support_active_users.add(cid)

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("🔄 START / नई बातचीत", callback_data="support_restart"),
        types.InlineKeyboardButton("🏠 मुख्य मेन्यू | Main Menu", callback_data="back_to_main_menu")
    )

    bot.send_message(
        cid,
        "🆘 <b>हेल्पलाइन सपोर्ट</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "नमस्ते 😊 आप यहाँ पोर्टल की सेवाओं के बारे में सीधे मदद ले सकते हैं।\n\n"
        "अपना सवाल या जरूरी स्क्रीनशॉट/फोटो भेजें। आपकी बातचीत सपोर्ट टीम तक भी पहुँचाई जाएगी।\n\n"
        "जब चाहें <b>START / नई बातचीत</b> दबाकर चैट को फिर से शुरू कर सकते हैं।",
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data == "support_restart")
def support_restart(c):
    bot.answer_callback_query(c.id, "नई बातचीत शुरू हो गई")
    cid = c.message.chat.id
    user_sessions[cid] = user_sessions.get(cid, {})
    user_sessions[cid]["mode"] = "support_chat"
    support_active_users.add(cid)

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("🔄 START / नई बातचीत", callback_data="support_restart"),
        types.InlineKeyboardButton("🏠 मुख्य मेन्यू | Main Menu", callback_data="back_to_main_menu")
    )
    bot.send_message(
        cid,
        "✅ <b>नई बातचीत शुरू हो गई।</b>\n\nअपना सवाल या screenshot/photo भेजें 👇",
        reply_markup=mk
    )

@bot.callback_query_handler(func=lambda c: c.data == "back_to_main_menu")
def back_to_main_menu_handler(c):
    bot.answer_callback_query(c.id)
    cid = c.message.chat.id
    if cid in user_sessions and "mode" in user_sessions[cid]:
        user_sessions[cid].pop("mode", None)
    companion_users.discard(cid)
    support_active_users.discard(cid)
    
    text = f"✨ <b>मुख्य मेन्यू</b>\n━━━━━━━━━━━━━━━━━━━━━\nकृपया नीचे दी गई सेवाओं में से चुनें 👇"
    bot.send_message(cid, text, reply_markup=get_main_menu_keyboard())

# Handle general messages for AI support mode

@bot.message_handler(
    func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "ai_companion"
    and m.content_type == "text"
)
def handle_ai_companion_message(m):
    if (m.text or "").startswith("/"):
        user_sessions.get(m.chat.id, {}).pop("mode", None)
        return
    companion_reply(m)

@bot.message_handler(func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "support_chat")
def handle_support_chat_message(m):
    """Forward helpline messages to admins and keep the user in support mode."""
    cid = m.chat.id
    user = m.from_user
    support_active_users.add(cid)
    db_upsert_user(user)

    # Admins receive a clear header + the original message/screenshot.
    username = f"@{user.username}" if user.username else "(no username)"
    name = " ".join(x for x in [user.first_name, user.last_name] if x) or "Unknown"
    header = (
        "🆘 <b>नया हेल्पलाइन संदेश</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {html.escape(name)}\n"
        f"🔢 <b>User ID:</b> <code>{cid}</code>\n"
        f"🔗 <b>Username:</b> {html.escape(username)}\n"
        f"📎 <b>Type:</b> {html.escape(m.content_type)}"
    )
    reply_mk = types.InlineKeyboardMarkup(row_width=1)
    reply_mk.add(types.InlineKeyboardButton(
        "💬 Reply to User / यूज़र को जवाब दें",
        callback_data=f"support_reply|{cid}"
    ))

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, header, reply_markup=reply_mk)
            # Forward the exact original message so screenshots/photos/documents are preserved.
            bot.forward_message(admin_id, cid, m.message_id)
        except Exception as e:
            logging.error("Support admin notification failed admin=%s user=%s: %s", admin_id, cid, e)

    # Small acknowledgement to user.
    if m.content_type == "photo":
        ack = "📸 आपका screenshot/photo सपोर्ट टीम को भेज दिया गया है।"
    elif m.content_type == "document":
        ack = "📄 आपकी file सपोर्ट टीम को भेज दी गई है।"
    else:
        ack = "✅ आपका message सपोर्ट टीम को भेज दिया गया है।"
    bot.reply_to(m, ack)

@bot.callback_query_handler(func=lambda c: c.data.startswith("support_reply|"))
def support_reply_start(c):
    if not is_admin_user(c.from_user.id):
        bot.answer_callback_query(c.id, "Unauthorized", show_alert=True)
        return
    try:
        target = int(c.data.split("|", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(c.id, "Invalid user", show_alert=True)
        return
    support_admin_targets[c.from_user.id] = target
    bot.answer_callback_query(c.id, "Reply mode ON")
    bot.send_message(
        c.message.chat.id,
        f"💬 <b>Reply mode चालू</b>\nUser ID: <code>{target}</code>\n\nअब अपना text/photo/document भेजें। वह सीधे इसी user को चला जाएगा।\n\nCancel: <code>/admin</code>"
    )

@bot.message_handler(commands=["reply"])
def command_support_reply(m):
    if not is_admin_user(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(m, "उदाहरण: <code>/reply 123456789 आपका message</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        bot.reply_to(m, "❌ सही numeric User ID दें।")
        return
    try:
        bot.send_message(target, "🆘 <b>हेल्पलाइन सपोर्ट</b>\n\n" + html.escape(parts[2]))
        bot.reply_to(m, "✅ जवाब user को भेज दिया गया।")
    except Exception as e:
        logging.error("/reply failed target=%s: %s", target, e)
        bot.reply_to(m, "❌ User को message नहीं भेज पाया।")

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and m.from_user.id in support_admin_targets)
def handle_admin_support_reply(m):
    admin_id = m.from_user.id
    target = support_admin_targets.get(admin_id)
    if not target:
        return
    if m.text and m.text.strip() == "/admin":
        support_admin_targets.pop(admin_id, None)
        send_admin_panel(m.chat.id)
        return
    try:
        if m.content_type == "text":
            bot.send_message(target, "🆘 <b>हेल्पलाइन सपोर्ट</b>\n\n" + html.escape(m.text))
        elif m.content_type == "photo":
            bot.send_photo(target, m.photo[-1].file_id, caption="🆘 <b>हेल्पलाइन सपोर्ट</b>")
        elif m.content_type == "document":
            bot.send_document(target, m.document.file_id, caption="🆘 <b>हेल्पलाइन सपोर्ट</b>")
        else:
            bot.copy_message(target, m.chat.id, m.message_id)
        bot.reply_to(m, f"✅ User <code>{target}</code> को जवाब भेज दिया गया।")
    except Exception as e:
        logging.error("Admin support reply failed admin=%s target=%s: %s", admin_id, target, e)
        bot.reply_to(m, "❌ जवाब भेजने में समस्या आई।")

@bot.message_handler(func=lambda m: user_sessions.get(m.chat.id, {}).get("mode") == "ai_chat" and m.content_type == "text")
def handle_ai_chat_message(m):
    """Resilient Gemini handler with retry/backoff for temporary 503/429 errors."""
    cid = m.chat.id
    user_text = (m.text or "").strip()

    if not user_text:
        return

    if user_text.startswith("/"):
        user_sessions.get(cid, {}).pop("mode", None)
        return

    if not ai_client:
        bot.reply_to(m, "⚠️ AI सेवा अभी एक्टिव नहीं है।")
        return

    # Prevent two Gemini requests from the same user from running at once.
    lock = ai_chat_locks.setdefault(cid, threading.Lock())
    if not lock.acquire(blocking=False):
        bot.reply_to(m, "⏳ आपका पिछला सवाल अभी process हो रहा है। कृपया 2-3 सेकंड रुकें।")
        return

    try:
        bot.send_chat_action(cid, "typing")

        # Gemini free tier has a per-minute request limit. A tiny per-user gap
        # prevents accidental double taps / rapid messages from causing 429s.
        now = time.monotonic()
        previous = ai_last_request.get(cid, 0.0)
        wait_for = 1.2 - (now - previous)
        if wait_for > 0:
            time.sleep(wait_for)
        ai_last_request[cid] = time.monotonic()

        # Create a fresh chat when needed.
        if cid not in ai_chat_sessions:
            ai_chat_sessions[cid] = ai_client.chats.create(
                model=GEMINI_MODEL,
                config=genai_types.GenerateContentConfig(
                    system_instruction=PRIYA_SYSTEM_PROMPT,
                    temperature=0.5
                )
            )

        answer = None
        last_error = None

        # 503/429 are commonly temporary on the Gemini free tier. Retry with
        # exponential backoff instead of immediately showing an error to user.
        for attempt in range(1, 4):
            try:
                response = ai_chat_sessions[cid].send_message(user_text)
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
                    "Gemini request failed for user %s (attempt %s/3): %s",
                    cid, attempt, exc
                )

                if not temporary or attempt >= 3:
                    break

                # If the chat session became unhealthy, recreate it before retry.
                ai_chat_sessions.pop(cid, None)
                time.sleep(2 ** (attempt - 1))
                ai_chat_sessions[cid] = ai_client.chats.create(
                    model=GEMINI_MODEL,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=PRIYA_SYSTEM_PROMPT,
                        temperature=0.5
                    )
                )

        if not answer:
            logging.exception("Gemini API failed for user %s: %s", cid, last_error)
            # Drop a broken session so the next message starts cleanly.
            ai_chat_sessions.pop(cid, None)
            error_text = str(last_error or "").upper()
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                reply = (
                    "⏳ अभी Gemini की request limit थोड़ी देर के लिए पूरी हो गई है।\n"
                    "कृपया 20-30 सेकंड बाद फिर सवाल भेजें।"
                )
            elif "404" in error_text or "NOT_FOUND" in error_text:
                reply = (
                    f"⚠️ Gemini model <code>{html.escape(GEMINI_MODEL)}</code> उपलब्ध नहीं है।\n"
                    "Render में GEMINI_MODEL की value check करें।"
                )
            else:
                reply = (
                    "⚠️ प्रिया को Gemini से अस्थायी server response नहीं मिला।\n"
                    "मैंने automatic retry किया है। कृपया कुछ सेकंड बाद फिर प्रयास करें।"
                )
            bot.reply_to(m, reply)
            return

        # Keep Telegram sending outside the Gemini exception block. If Telegram
        # has a temporary problem, we must not destroy the healthy Gemini chat.
        try:
            bot.reply_to(m, answer)
        except Exception as telegram_error:
            logging.error("Telegram reply failed for user %s: %s", cid, telegram_error)

    except Exception as e:
        logging.exception("Unexpected AI handler error for user %s: %s", cid, e)
        ai_chat_sessions.pop(cid, None)
        try:
            bot.reply_to(m, "⚠️ प्रिया से अभी connection में समस्या है। कुछ सेकंड बाद फिर कोशिश करें।")
        except Exception:
            pass
    finally:
        lock.release()

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
        types.KeyboardButton("🆘 Support Chats"),
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
        f"📦 Applications: <b>{total_apps}</b> | ⏳ Pending: <b>{pending}</b> | ✅ Done: <b>{completed}</b>\n"
        f"🆘 Active support chats: <b>{len(support_active_users)}</b>\n\n"
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

@bot.message_handler(func=lambda m: is_admin_user(m.from_user.id) and m.text in {"📊 User Stats","📢 Broadcast","🤖 Bot Status","💰 Prices","💳 UPI Settings","🆘 Support Chats","🔄 Refresh Admin"})
def admin_menu_actions(m):
    uid = m.from_user.id
    if m.text == "📊 User Stats": send_user_stats(m.chat.id)
    elif m.text == "📢 Broadcast":
        admin_sessions[uid] = "broadcast"
        bot.reply_to(m, "📢 अब अगला text message भेजें। वही सभी registered users को भेजा जाएगा।\nCancel के लिए <code>/admin</code> भेजें।")
    elif m.text == "🤖 Bot Status": send_bot_status(m.chat.id)
    elif m.text == "💰 Prices": send_price_panel(m.chat.id)
    elif m.text == "💳 UPI Settings": bot.send_message(m.chat.id, f"💳 Current UPI: <code>{get_setting('upi_id')}</code>\nबदलने के लिए <code>/setupi yourupi@bank</code>")
    elif m.text == "🆘 Support Chats":
        active = len(support_active_users)
        bot.send_message(m.chat.id, f"🆘 <b>Support Chats</b>\n\n🟢 Active chats: <b>{active}</b>\n\nUser का message आते ही यहाँ alert + Reply button मिलेगा।\n\nDirect reply: <code>/reply USER_ID message</code>", reply_markup=admin_reply_keyboard())
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
# 6. APPLICATION & DOCUMENT COLLECTION FLOW
# ==============================================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def handle_category_selection(c):
    bot.answer_callback_query(c.id)
    cat_name = c.data.split("|", 1)[1]

    sess = user_sessions.setdefault(c.message.chat.id, {"docs": {}, "idx": 0})
    sess.update(category=cat_name, docs={}, idx=0)
    sess.pop("mode", None) # Turn off AI mode during form filling

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
