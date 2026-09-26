import os, sqlite3, threading, logging
from io import BytesIO
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import types
import qrcode
from flask import Flask, request, redirect, url_for, render_template_string, session

DB = "bot.db"
BOT_TOKEN = os.getenv("8714712105:AAF1utPqGbpVI7etpHt8LxjrGxdJlNRjw1Q")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6874667015,7771292960"))
DEFAULT_UPI = os.getenv("UPI_ID", "yourupi@bank")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me")
PORT = int(os.getenv("PORT", "10000"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", str(PORT)))

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-this")

DOCS = {
    "Jati / Aawas / Niwas": ["Aadhar Card", "Photo", "Mobile Number", "Gmail ID"],
    "NCL": ["Aadhar Card", "Photo", "Mobile Number", "Gmail ID"],
    "PMS": ["Jati Certificate", "Aay Certificate", "Aadhar Card", "10th Certificate",
            "Bonafide Certificate", "Fee Receipt", "Photo"],
}
sessions = {}

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS applications(
      id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,username TEXT,category TEXT,
      plan TEXT,amount INTEGER,status TEXT DEFAULT 'pending',created_at TEXT);
    CREATE TABLE IF NOT EXISTS users(
      user_id INTEGER PRIMARY KEY,username TEXT,banned INTEGER DEFAULT 0,
      fancoin INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS broadcasts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS fancoin_tx(
      id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,delta INTEGER,
      reason TEXT,created_at TEXT);
    """)
    defaults = {
        "upi_id": DEFAULT_UPI,
        "jati_48h": "300",
        "jati_3d": "200",
        "jati_online": "120",
        "ncl": "50",
        "pms": "20"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    c.commit()
    c.close()

def setting(k):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    c.close()
    return r["value"] if r else ""

def set_setting(k, v):
    c = db()
    c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (k, str(v)))
    c.commit()
    c.close()

def user_upsert(m):
    c = db()
    c.execute("""INSERT INTO users(user_id,username) VALUES(?,?)
                 ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
              (m.from_user.id, m.from_user.username or ""))
    c.commit()
    c.close()

def get_fancoin(uid):
    c = db()
    r = c.execute("SELECT fancoin FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return int(r["fancoin"]) if r else 0

def change_fancoin(uid, delta, reason="Admin adjustment"):
    c = db()
    c.execute("""INSERT INTO users(user_id,fancoin) VALUES(?,?)
                 ON CONFLICT(user_id) DO UPDATE SET fancoin=fancoin+excluded.fancoin""",
              (uid, delta))
    c.execute("""INSERT INTO fancoin_tx(user_id,delta,reason,created_at)
                 VALUES(?,?,?,?)""", (uid, delta, reason, now()))
    c.commit()
    r = c.execute("SELECT fancoin FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return int(r["fancoin"])

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
    return int(setting({"48h":"jati_48h", "3d":"jati_3d", "online":"jati_online"}[key]))

def make_qr(amount):
    upi = f"upi://pay?pa={setting('upi_id')}&pn=DocumentService&am={amount}&cu=INR"
    q = qrcode.QRCode(version=1, box_size=8, border=4)
    q.add_data(upi)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "payment_qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio, upi

@bot.message_handler(commands=["start"])
def start(m):
    user_upsert(m)
    if banned(m.from_user.id):
        bot.reply_to(m, "Aapka account restricted hai.")
        return
    sessions[m.chat.id] = {"docs": {}}
    mk = types.InlineKeyboardMarkup(row_width=1)
    for cat in DOCS:
        mk.add(types.InlineKeyboardButton(cat, callback_data="cat|" + cat))
    mk.add(types.InlineKeyboardButton("🪙 Fancoin Balance", callback_data="coin|balance"))
    bot.send_message(m.chat.id, "Namaskar! Service chuniye:", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "coin|balance")
def coin_balance(c):
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, f"🪙 Aapka Fancoin Balance: {get_fancoin(c.from_user.id)}")

@bot.message_handler(commands=["fancoin"])
def fancoin_cmd(m):
    user_upsert(m)
    bot.reply_to(m, f"🪙 Aapka Fancoin Balance: {get_fancoin(m.from_user.id)}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def category(c):
    bot.answer_callback_query(c.id)
    category_name = c.data.split("|", 1)[1]
    s = sessions.setdefault(c.message.chat.id, {"docs": {}})
    s.update(category=category_name, docs={}, idx=0)
    mk = types.InlineKeyboardMarkup(row_width=1)

    if category_name == "Jati / Aawas / Niwas":
        for key, label in [("48h","48 Ghante Me"),("3d","3 Din Me"),("online","Sirf Online")]:
            mk.add(types.InlineKeyboardButton(
                f"{label} (₹{price(category_name,key)})", callback_data="plan|" + key))
        text = f"{category_name}\nDelivery plan chuniye:"
    else:
        amount = price(category_name)
        mk.add(types.InlineKeyboardButton(
            f"Continue (₹{amount})", callback_data="fixed|go"))
        text = f"{category_name}\nService fee: ₹{amount}"

    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan|") or c.data == "fixed|go")
def plan(c):
    bot.answer_callback_query(c.id)
    s = sessions[c.message.chat.id]
    category_name = s["category"]
    key = c.data.split("|", 1)[1]
    amount = price(category_name, key if category_name == "Jati / Aawas / Niwas" else None)
    label = {"48h":"48 Ghante Me","3d":"3 Din Me","online":"Sirf Online"}.get(key, "Standard")
    s.update(plan=label, amount=amount)
    bot.edit_message_text(
        f"Plan: {label}\nFee: ₹{amount}\n\nDocuments collect honge.",
        c.message.chat.id, c.message.message_id)
    ask(c.message.chat.id)

def ask(cid):
    s = sessions[cid]
    idx = s["idx"]
    req = DOCS[s["category"]]
    if idx >= len(req):
        payment(cid)
        return
    msg = bot.send_message(
        cid, f"Step {idx+1}/{len(req)}: {req[idx]} bhejiye (photo/document/text).")
    bot.register_next_step_handler(msg, doc_input)

def doc_input(m):
    cid = m.chat.id
    if cid not in sessions:
        bot.reply_to(m, "Session expire. /start")
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
        bot.reply_to(m, "Sirf photo/document/text bhejiye.")
        ask(cid)
        return
    s["docs"][name] = value
    s["idx"] += 1
    ask(cid)

def payment(cid):
    s = sessions[cid]
    img, upi = make_qr(s["amount"])
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(f"Pay ₹{s['amount']}", url=upi))
    bot.send_photo(
        cid, img,
        caption=f"Payment QR\nAmount: ₹{s['amount']}\nPayment ke baad screenshot/UTR bhejiye.",
        reply_markup=mk)
    msg = bot.send_message(cid, "Payment screenshot ya UTR message bhejiye:")
    bot.register_next_step_handler(msg, payment_proof)

def payment_proof(m):
    cid = m.chat.id
    s = sessions.get(cid)
    if not s:
        bot.reply_to(m, "Session expire. /start")
        return
    c = db()
    cur = c.execute("""INSERT INTO applications
        (user_id,username,category,plan,amount,created_at)
        VALUES(?,?,?,?,?,?)""",
        (m.from_user.id, m.from_user.username or "", s["category"],
         s["plan"], s["amount"], now()))
    appid = cur.lastrowid
    c.commit()
    c.close()

    bot.send_message(
        ADMIN_ID,
        f"NEW APPLICATION #{appid}\nUser: {m.from_user.id}\n"
        f"Category: {s['category']}\nPlan: {s['plan']}\nAmount: ₹{s['amount']}")
    for name, (typ, val) in s["docs"].items():
        if typ == "photo":
            bot.send_photo(ADMIN_ID, val, caption=f"#{appid} {name}")
        elif typ == "document":
            bot.send_document(ADMIN_ID, val, caption=f"#{appid} {name}")
        else:
            bot.send_message(ADMIN_ID, f"#{appid} {name}: {val}")
    bot.forward_message(ADMIN_ID, cid, m.message_id)
    bot.send_message(cid, f"Application #{appid} submit ho gaya. Admin review karega.")
    sessions.pop(cid, None)

# ---------------- HEALTH CHECK ----------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthCheckHandler)
    logging.info("Health Check Server Running on Port %s", HEALTH_PORT)
    server.serve_forever()

# ---------------- ADMIN PANEL ----------------

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
        return """<form method="post">
        <input name="secret" type="password" placeholder="Admin password">
        <button>Login</button></form>"""

    c = db()
    rows = c.execute("SELECT * FROM applications ORDER BY id DESC LIMIT 100").fetchall()
    users = c.execute("SELECT user_id,username,fancoin,banned FROM users ORDER BY user_id DESC LIMIT 100").fetchall()
    tx = c.execute("SELECT * FROM fancoin_tx ORDER BY id DESC LIMIT 30").fetchall()
    c.close()

    html = """
    <h2>Document Service Admin</h2>
    <h3>Pricing / UPI</h3>
    <form method=post action=/settings>
    UPI <input name=upi value="{{upi}}"><br>
    Jati 48h <input name=j48 value="{{j48}}">
    3d <input name=j3 value="{{j3}}">
    Online <input name=jo value="{{jo}}"><br>
    NCL <input name=ncl value="{{ncl}}">
    PMS <input name=pms value="{{pms}}">
    <button>Save</button></form>

    <h3>Fancoin Management</h3>
    <form method=post action=/fancoin>
      User ID <input name=user_id required>
      Amount (+ add / - deduct) <input name=delta required>
      Reason <input name=reason value="Admin adjustment">
      <button>Update Fancoin</button>
    </form>

    <h3>Applications</h3>
    <table border=1 cellpadding=5>
    <tr><th>ID</th><th>User</th><th>Category</th><th>Plan</th><th>Amount</th><th>Status</th></tr>
    {% for r in rows %}
    <tr><td>{{r.id}}</td><td>{{r.user_id}}</td><td>{{r.category}}</td>
    <td>{{r.plan}}</td><td>{{r.amount}}</td><td>{{r.status}}</td></tr>
    {% endfor %}</table>

    <h3>Users / Fancoin</h3>
    <table border=1 cellpadding=5>
    <tr><th>User ID</th><th>Username</th><th>Fancoin</th><th>Banned</th></tr>
    {% for u in users %}
    <tr><td>{{u.user_id}}</td><td>{{u.username}}</td><td>{{u.fancoin}}</td><td>{{u.banned}}</td></tr>
    {% endfor %}</table>

    <h3>Fancoin History</h3>
    <table border=1 cellpadding=5>
    <tr><th>ID</th><th>User</th><th>Change</th><th>Reason</th><th>Time</th></tr>
    {% for t in tx %}
    <tr><td>{{t.id}}</td><td>{{t.user_id}}</td><td>{{t.delta}}</td>
    <td>{{t.reason}}</td><td>{{t.created_at}}</td></tr>
    {% endfor %}</table>

    <h3>Broadcast</h3>
    <form method=post action=/broadcast>
      <textarea name=message rows=4 cols=50></textarea>
      <button>Send Broadcast</button>
    </form>

    <p><b>Health:</b> <a href="/health">/health</a></p>
    """
    return render_template_string(
        html, rows=rows, users=users, tx=tx,
        upi=setting("upi_id"), j48=setting("jati_48h"),
        j3=setting("jati_3d"), jo=setting("jati_online"),
        ncl=setting("ncl"), pms=setting("pms"))

@app.route("/settings", methods=["POST"])
def settings():
    if not session.get("admin"):
        return "Unauthorized", 403
    values = {
        "upi_id": request.form["upi"],
        "jati_48h": request.form["j48"],
        "jati_3d": request.form["j3"],
        "jati_online": request.form["jo"],
        "ncl": request.form["ncl"],
        "pms": request.form["pms"],
    }
    for k, v in values.items():
        set_setting(k, v)
    return redirect(url_for("admin"))

@app.route("/fancoin", methods=["POST"])
def fancoin():
    if not session.get("admin"):
        return "Unauthorized", 403
    uid = int(request.form["user_id"])
    delta = int(request.form["delta"])
    reason = request.form.get("reason", "Admin adjustment")
    balance = change_fancoin(uid, delta, reason)
    try:
        bot.send_message(uid, f"🪙 Fancoin update: {delta:+d}\nBalance: {balance}\nReason: {reason}")
    except Exception:
        pass
    return redirect(url_for("admin"))

@app.route("/broadcast", methods=["POST"])
def broadcast():
    if not session.get("admin"):
        return "Unauthorized", 403
    msg = request.form.get("message", "").strip()
    if not msg:
        return redirect(url_for("admin"))
    c = db()
    users = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    c.execute("INSERT INTO broadcasts(message,created_at) VALUES(?,?)", (msg, now()))
    c.commit()
    c.close()
    sent = 0
    for r in users:
        try:
            bot.send_message(r["user_id"], msg)
            sent += 1
        except Exception:
            pass
    return f"Broadcast sent: {sent}. <a href='/admin'>Back</a>"

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    logging.info("Flask Admin running on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
