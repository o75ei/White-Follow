"""
SMM Panel Reseller Bot — Full Backend
Railway-compatible | Flask + PostgreSQL (Supabase) | Telegram WebApp
"""

from flask import Flask, request, jsonify, send_file, redirect, session, make_response
from flask_cors import CORS
import requests, os, threading, json, time, hashlib, hmac, secrets
import psycopg2, psycopg2.extras
import datetime, logging, smtplib, random, string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

# ─────────────────────────────────────────────
# App Init
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
# Use WEBAPP_URL for CORS if set, else allow all origins for dev
_cors_origins = os.environ.get("WEBAPP_URL", "*")
if _cors_origins and _cors_origins != "*":
    CORS(app, origins=[_cors_origins, "http://localhost:3000", "http://localhost:5000"],
         supports_credentials=True)
else:
    CORS(app, origins="*", supports_credentials=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smm")

# ─────────────────────────────────────────────
# Environment Variables
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_USERNAME     = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD     = os.environ.get("ADMIN_PASSWORD", "admin2026%")
ADMIN_PIN          = os.environ.get("ADMIN_PIN", "147258")   # Hardcoded fallback PIN
ADMIN_SECRET_KEY   = os.environ.get("ADMIN_SECRET_KEY", "")
WEBAPP_URL         = os.environ.get("WEBAPP_URL", "https://YOUR-APP.up.railway.app")
SUPPORT_USERNAME   = os.environ.get("SUPPORT_USERNAME", "support")

# Dark Follow provider (auto-registered on startup)
DARKFOLLOW_API_KEY = os.environ.get("DARKFOLLOW_API_KEY", "")
DARKFOLLOW_API_URL = os.environ.get("DARKFOLLOW_API_URL", "https://darkfollow.shop/api/v2")

# SMTP — Email Verification
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)
EMAIL_VERIFY  = os.environ.get("EMAIL_VERIFY", "1")

# ─────────────────────────────────────────────
# Database — PostgreSQL (Supabase)
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

class _PGCursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, sql, params=None):
        self._cur.execute(sql, params or [])
        try:
            row = self._cur.fetchone()
            if row:
                self.lastrowid = list(row.values())[0]
        except Exception:
            self.lastrowid = None
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(r) for r in (self._cur.fetchall() or [])]

    @property
    def rowcount(self):
        return self._cur.rowcount

class _PGConn:
    def __init__(self, conn):
        self._conn = conn
        self._cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._wrap = _PGCursor(self._cur)

    def execute(self, sql, params=None):
        self._wrap.execute(sql, params)
        return self._wrap

    def executescript(self, script):
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    self._wrap.execute(stmt + ";")
                except Exception as e:
                    log.debug(f"[PG] skip stmt: {e}")
                    self._conn.rollback()

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()

    def __enter__(self): return self
    def __exit__(self, *a):
        if a[0]: self._conn.rollback()
        else:    self._conn.commit()
        return False

def get_db():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise Exception("DATABASE_URL غير موجود — تشغيل بدون قاعدة بيانات")
    conn = psycopg2.connect(
        db_url,
        connect_timeout=10,
        cursor_factory=psycopg2.extras.RealDictCursor
    )
    return _PGConn(conn)

def init_db():
    with get_db() as db:
        # Users
        db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            uid           TEXT    UNIQUE,
            telegram_id   TEXT    UNIQUE,
            email         TEXT    UNIQUE,
            username      TEXT,
            password_hash TEXT,
            balance       NUMERIC DEFAULT 0,
            total_charged NUMERIC DEFAULT 0,
            total_spent   NUMERIC DEFAULT 0,
            orders_count  INTEGER DEFAULT 0,
            is_banned     INTEGER DEFAULT 0,
            language      TEXT    DEFAULT 'ar',
            joined_at     TEXT    DEFAULT (NOW()::TEXT),
            last_seen     TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Providers
        db.execute("""
        CREATE TABLE IF NOT EXISTS providers (
            id         SERIAL PRIMARY KEY,
            name       TEXT    NOT NULL,
            api_url    TEXT    NOT NULL,
            api_key    TEXT    NOT NULL,
            balance    NUMERIC DEFAULT 0,
            is_active  INTEGER DEFAULT 1,
            created_at TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Categories
        db.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id           SERIAL PRIMARY KEY,
            name_ar      TEXT    NOT NULL,
            name_en      TEXT    NOT NULL,
            icon         TEXT    DEFAULT '📦',
            image_url    TEXT    DEFAULT '',
            sort_order   INTEGER DEFAULT 0,
            is_active    INTEGER DEFAULT 1,
            markup_type  TEXT    DEFAULT 'percent',
            markup_value NUMERIC DEFAULT 0
        )""")
        # Services
        db.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id                  SERIAL PRIMARY KEY,
            provider_id         INTEGER REFERENCES providers(id),
            provider_service_id TEXT,
            category_id         INTEGER REFERENCES categories(id),
            name_ar             TEXT    NOT NULL,
            name_en             TEXT    NOT NULL,
            description_ar      TEXT    DEFAULT '',
            description_en      TEXT    DEFAULT '',
            type                TEXT    DEFAULT 'Default',
            min_qty             INTEGER DEFAULT 10,
            max_qty             INTEGER DEFAULT 10000,
            provider_price      NUMERIC DEFAULT 0,
            markup_type         TEXT    DEFAULT 'percent',
            markup_value        NUMERIC DEFAULT 0,
            final_price         NUMERIC DEFAULT 0,
            estimated_time      TEXT    DEFAULT '',
            image_url           TEXT    DEFAULT '',
            is_active           INTEGER DEFAULT 1,
            created_at          TEXT    DEFAULT (NOW()::TEXT),
            updated_at          TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Orders
        db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id                SERIAL PRIMARY KEY,
            user_id           INTEGER REFERENCES users(id),
            service_id        INTEGER REFERENCES services(id),
            provider_order_id TEXT,
            link              TEXT    NOT NULL,
            quantity          INTEGER NOT NULL,
            price             NUMERIC NOT NULL,
            status            TEXT    DEFAULT 'pending',
            remains           INTEGER DEFAULT 0,
            start_count       INTEGER DEFAULT 0,
            notes             TEXT    DEFAULT '',
            created_at        TEXT    DEFAULT (NOW()::TEXT),
            updated_at        TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Payments
        db.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER REFERENCES users(id),
            amount         NUMERIC NOT NULL,
            method         TEXT    NOT NULL,
            status         TEXT    DEFAULT 'pending',
            transaction_id TEXT,
            proof_url      TEXT,
            admin_note     TEXT,
            created_at     TEXT    DEFAULT (NOW()::TEXT),
            processed_at   TEXT
        )""")
        # Payment gateways
        db.execute("""
        CREATE TABLE IF NOT EXISTS payment_gateways (
            id          SERIAL PRIMARY KEY,
            name        TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            details_ar  TEXT    DEFAULT '',
            details_en  TEXT    DEFAULT '',
            is_active   INTEGER DEFAULT 1,
            is_auto     INTEGER DEFAULT 0,
            config_json TEXT    DEFAULT '{}'
        )""")
        # Tickets
        db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER REFERENCES users(id),
            subject    TEXT    NOT NULL,
            status     TEXT    DEFAULT 'open',
            priority   TEXT    DEFAULT 'normal',
            created_at TEXT    DEFAULT (NOW()::TEXT),
            updated_at TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Ticket messages
        db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id          SERIAL PRIMARY KEY,
            ticket_id   INTEGER REFERENCES tickets(id),
            sender_type TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (NOW()::TEXT)
        )""")
        # Translations
        db.execute("""
        CREATE TABLE IF NOT EXISTS translations (
            id    SERIAL PRIMARY KEY,
            lang  TEXT NOT NULL,
            key   TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(lang, key)
        )""")
        # Admin sessions
        db.execute("""
        CREATE TABLE IF NOT EXISTS admin_sessions (
            id         SERIAL PRIMARY KEY,
            token      TEXT UNIQUE NOT NULL,
            ip         TEXT,
            user_agent TEXT,
            created_at TEXT DEFAULT (NOW()::TEXT),
            expires_at TEXT NOT NULL
        )""")
        # Security log
        db.execute("""
        CREATE TABLE IF NOT EXISTS security_log (
            id         SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            ip         TEXT,
            details    TEXT,
            created_at TEXT DEFAULT (NOW()::TEXT)
        )""")
        # Settings
        db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )""")
        # Cron log
        db.execute("""
        CREATE TABLE IF NOT EXISTS cron_log (
            id          SERIAL PRIMARY KEY,
            job_name    TEXT,
            status      TEXT,
            details     TEXT,
            duration_ms INTEGER,
            ran_at      TEXT DEFAULT (NOW()::TEXT)
        )""")
        # Email verifications
        db.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            id            SERIAL PRIMARY KEY,
            email         TEXT NOT NULL,
            username      TEXT,
            password_hash TEXT NOT NULL,
            code          TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            created_at    TEXT DEFAULT (NOW()::TEXT)
        )""")
        # User sessions
        db.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id         SERIAL PRIMARY KEY,
            token      TEXT UNIQUE NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at TEXT DEFAULT (NOW()::TEXT),
            expires_at TEXT NOT NULL
        )""")
        db.commit()

    # Default settings (ON CONFLICT DO NOTHING)
    defaults = [
        ('site_name', 'SMM Panel'),
        ('global_markup_type', 'percent'),
        ('global_markup_value', '0'),
        ('currency', 'USD'),
        ('min_deposit', '1'),
        ('maintenance_mode', '0'),
        ('faq_ar', '[]'),
        ('faq_en', '[]'),
        ('tos_ar', ''),
        ('tos_en', ''),
    ]
    with get_db() as db:
        for k, v in defaults:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )
        db.commit()

    # Migration: generate uid for existing users who don't have one
    with get_db() as db:
        users_no_uid = db.execute("SELECT id FROM users WHERE uid IS NULL OR uid=''").fetchall()
        for u in users_no_uid:
            uid = f"WF-{u['id']:06d}"
            db.execute("UPDATE users SET uid=%s WHERE id=%s", (uid, u['id']))
        if users_no_uid:
            db.commit()

    # Migration: reset markup to 0% if still at old default of 20
    with get_db() as db:
        try:
            old_val = db.execute("SELECT value FROM settings WHERE key='global_markup_value'").fetchone()
            if old_val and old_val['value'] == '20':
                db.execute("UPDATE settings SET value='0' WHERE key='global_markup_value'")
                db.commit()
                log.info("[migration] Reset global_markup_value from 20 to 0")
        except Exception:
            pass

    log.info("✅ Database initialized")

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def calc_price(provider_price, markup_type, markup_value):
    if markup_type == 'percent':
        return round(provider_price * (1 + markup_value / 100), 4)
    elif markup_type == 'fixed':
        return round(provider_price + markup_value, 4)
    return provider_price

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_setting(key, default=""):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, value)
        )
        db.commit()

def tg(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                          json=payload, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"[TG] {e}")
        return {}

def notify_admin(text):
    if TELEGRAM_CHAT_ID:
        tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})

def log_security(event_type, ip, details=""):
    with get_db() as db:
        db.execute("INSERT INTO security_log (event_type,ip,details) VALUES (%s,%s,%s)",
                   (event_type, ip, details))
        db.commit()

def get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

# ─────────────────────────────────────────────
# Admin Auth
# ─────────────────────────────────────────────
ADMIN_SESSION_HOURS = 8
MAX_LOGIN_ATTEMPTS = 5
_login_attempts = {}

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Token", "") or request.cookies.get("admin_token", "")
        if not token:
            return jsonify({"error": "unauthorized"}), 401
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT id FROM admin_sessions WHERE token=%s", (token,)
                ).fetchone()
            if not row:
                return jsonify({"error": "session expired"}), 401
        except Exception:
            pass  # DB error - still allow if token exists
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin") or data.get("password") or "").strip()
    correct_pin = "147258"  # Fixed PIN - change here to update

    if pin != correct_pin:
        return jsonify({"error": "الرمز خاطئ"}), 403

    token = secrets.token_hex(32)
    try:
        with get_db() as db:
            db.execute("INSERT INTO admin_sessions (token,ip,user_agent,expires_at) VALUES (%s,%s,%s,%s)",
                       (token, get_client_ip(), request.headers.get("User-Agent",""), "9999-12-31"))
            db.commit()
    except Exception:
        pass

    resp = make_response(jsonify({"ok": True, "token": token}))
    resp.set_cookie("admin_token", token, httponly=True, samesite="Lax", max_age=720*3600)
    return resp

@app.route("/admin/logout", methods=["POST"])
@require_admin
def admin_logout():
    token = request.headers.get("X-Admin-Token","") or request.cookies.get("admin_token","")
    try:
        with get_db() as db:
            db.execute("DELETE FROM admin_sessions WHERE token=%s", (token,))
            db.commit()
    except Exception:
        pass
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("admin_token")
    return resp

@app.route("/admin/verify", methods=["POST"])
def admin_verify():
    token = request.headers.get("X-Admin-Token","") or request.cookies.get("admin_token","")
    if not token:
        return jsonify({"ok": False}), 401
    try:
        with get_db() as db:
            row = db.execute("SELECT id FROM admin_sessions WHERE token=%s", (token,)).fetchone()
        return jsonify({"ok": bool(row)})
    except Exception:
        return jsonify({"ok": False})

# ─────────────────────────────────────────────
# Route: /admin → redirect to admin panel
# ─────────────────────────────────────────────
@app.route("/admin")
def admin_redirect():
    return send_file("admin.html")

@app.route("/")
def home():
    resp = make_response(send_file("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/app")
@app.route("/app2")
def serve_app():
    resp = make_response(send_file("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/bust-cache")
def bust_cache():
    """
    زيارة هذا الرابط تجبر Telegram WebView على مسح الكاش وإعادة تحميل البوت.
    استخدم: https://YOUR-APP.up.railway.app/bust-cache
    """
    import datetime
    ts = int(datetime.datetime.utcnow().timestamp())
    html_page = f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>تحديث البوت...</title>
<style>
  body {{ background:#0f0f0f; color:#fff; font-family:'Tajawal',sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:100vh; margin:0; gap:16px; }}
  .spinner {{ width:48px; height:48px; border:4px solid #333;
             border-top-color:#DAA520; border-radius:50%;
             animation:spin 0.8s linear infinite; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
  p {{ font-size:18px; color:#DAA520; }}
  small {{ color:#666; font-size:13px; }}
</style>
</head>
<body>
<div class="spinner"></div>
<p>جاري مسح الكاش وتحديث البوت...</p>
<small>سيتم إعادة التوجيه تلقائياً</small>
<script>
// مسح كل أنواع الكاش
try {{ localStorage.clear(); }} catch(e){{}}
try {{ sessionStorage.clear(); }} catch(e){{}}

// مسح Service Workers
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.getRegistrations().then(function(regs) {{
    regs.forEach(function(r) {{ r.unregister(); }});
  }});
}}

// مسح Cache API
if ('caches' in window) {{
  caches.keys().then(function(names) {{
    names.forEach(function(name) {{ caches.delete(name); }});
  }});
}}

// إعادة التوجيه بعد ثانية مع timestamp يكسر الكاش
setTimeout(function() {{
  window.location.replace('/%sv={ts}');
}}, 1200);
</script>
</body>
</html>"""
    resp = make_response(html_page)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.datetime.utcnow().isoformat()})

# ─────────────────────────────────────────────
# Telegram Bot — Shared Update Handler
# (used by the polling loop below)
# ─────────────────────────────────────────────
def _handle_tg_update(update):
    """Process a single Telegram update dict (message or callback_query)."""
    try:
        msg     = update.get("message") or update.get("edited_message") or {}
        cb      = update.get("callback_query") or {}

        # ── Callback queries (inline button clicks) ──
        if cb:
            cb_id   = cb.get("id")
            cb_data = cb.get("data", "")
            cb_user = cb.get("from", {})
            cb_chat = (cb.get("message") or {}).get("chat", {})
            chat_id = cb_chat.get("id")
            tg_id   = str(cb_user.get("id", ""))
            # Acknowledge the callback
            tg("answerCallbackQuery", {"callback_query_id": cb_id})
            # Handle known callbacks here if needed
            return

        # ── Regular messages ──
        chat_id = msg.get("chat", {}).get("id")
        text    = msg.get("text", "")
        user    = msg.get("from", {})
        name    = user.get("first_name", "User")
        tg_id   = str(user.get("id", ""))

        if not chat_id:
            return

        # ── /start ──
        if text.startswith("/start"):
            tg("sendMessage", {
                "chat_id": chat_id,
                "text": f"👋 أهلاً <b>{name}</b>!\n\nمرحباً بك في بوت إدارة خدمات SMM.\nاضغط الزر أدناه لفتح التطبيق:",
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "🚀 فتح التطبيق", "web_app": {"url": WEBAPP_URL}}],
                        [{"text": "💬 الدعم الفني", "url": f"https://t.me/{SUPPORT_USERNAME}"}]
                    ]
                }
            })
            # Register user in background
            def _register():
                try:
                    with get_db() as db:
                        db.execute("""
                            INSERT INTO users (telegram_id, username)
                            VALUES (%s, %s)
                            ON CONFLICT (telegram_id) DO NOTHING
                        """, (tg_id, user.get("username", name)))
                        db.execute("UPDATE users SET last_seen=NOW() WHERE telegram_id=%s", (tg_id,))
                        db.commit()
                        u_row = db.execute("SELECT id, uid FROM users WHERE telegram_id=%s", (tg_id,)).fetchone()
                        if u_row and not u_row.get("uid"):
                            new_uid = f"WF-{u_row['id']:06d}"
                            db.execute("UPDATE users SET uid=%s WHERE id=%s", (new_uid, u_row["id"]))
                            db.commit()
                except Exception as e:
                    log.error(f"[polling] /start DB error (non-fatal): {e}")
            if tg_id:
                threading.Thread(target=_register, daemon=True).start()
            return

        # Auto-register for all other commands (non-fatal)
        if tg_id:
            try:
                with get_db() as db:
                    db.execute("""
                        INSERT INTO users (telegram_id, username)
                        VALUES (%s, %s)
                        ON CONFLICT (telegram_id) DO NOTHING
                    """, (tg_id, user.get("username", name)))
                    db.execute("UPDATE users SET last_seen=NOW() WHERE telegram_id=%s", (tg_id,))
                    db.commit()
            except Exception as e:
                log.error(f"[polling] auto-register DB error (non-fatal): {e}")

        # ── /balance ──
        if text.startswith("/balance"):
            with get_db() as db:
                user_data = db.execute(
                    "SELECT balance FROM users WHERE telegram_id=%s", (tg_id,)
                ).fetchone()
            balance = user_data["balance"] if user_data else 0
            tg("sendMessage", {
                "chat_id": chat_id,
                "text": f"💰 رصيدك الحالي: <b>${balance:.2f}</b>",
                "parse_mode": "HTML"
            })

        # ── /orders ──
        elif text.startswith("/orders"):
            with get_db() as db:
                orders = db.execute("""
                    SELECT o.id, o.status, s.name_ar
                    FROM orders o
                    LEFT JOIN services s ON s.id=o.service_id
                    WHERE o.user_id=(SELECT id FROM users WHERE telegram_id=%s)
                    ORDER BY o.created_at DESC LIMIT 5
                """, (tg_id,)).fetchall()
            if not orders:
                text_msg = "📭 لا توجد طلبات"
            else:
                text_msg = "📦 آخر 5 طلبات:\n\n"
                for o in orders:
                    status_emoji = {"pending":"🟡","active":"🟢","completed":"✅","cancelled":"❌","partial":"⚠️"}.get(o["status"],"⚪")
                    text_msg += f"{status_emoji} #{o['id']} - {o['name_ar'] or 'خدمة'}\n"
            tg("sendMessage", {"chat_id": chat_id, "text": text_msg})

        # ── /support ──
        elif text.startswith("/support"):
            tg("sendMessage", {
                "chat_id": chat_id,
                "text": "للدعم الفني راسلنا عبر:",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "💬 تواصل مع الدعم", "url": f"https://t.me/{SUPPORT_USERNAME}"}
                    ]]
                }
            })

    except Exception as e:
        log.error(f"[polling] _handle_tg_update error: {e}")


# ─────────────────────────────────────────────
# Telegram Bot — Threaded Polling Loop
# Replaces the old /webhook endpoint completely.
# ─────────────────────────────────────────────
def _bot_polling_loop():
    """
    Non-blocking background thread that polls Telegram getUpdates
    with offset tracking to avoid reprocessing old messages.
    Retries automatically after any network or server error.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.warning("[polling] TELEGRAM_BOT_TOKEN not set — bot polling disabled")
        return

    log.info("[polling] Telegram polling loop started")
    offset = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": offset, "allowed_updates": ["message", "callback_query"]}
            resp = requests.get(url, params=params, timeout=40)
            data = resp.json()

            if not data.get("ok"):
                log.warning(f"[polling] getUpdates not ok: {data}")
                time.sleep(3)
                continue

            updates = data.get("result", [])
            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id >= offset:
                    offset = update_id + 1
                # Process in a separate thread so one slow handler
                # doesn't delay the polling loop
                threading.Thread(
                    target=_handle_tg_update,
                    args=(update,),
                    daemon=True
                ).start()

        except requests.exceptions.Timeout:
            # Long-poll timeout is normal — just loop again immediately
            continue
        except Exception as e:
            log.error(f"[polling] error — retrying in 3s: {e}")
            time.sleep(3)

# ─────────────────────────────────────────────
# User Auth (Web)
# ─────────────────────────────────────────────
def _send_verification_email(to_email, code, username):
    """Send OTP email via SMTP. Returns True on success."""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("[EMAIL] SMTP not configured — code: %s", code)
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "رمز التحقق — White Follow"
        msg["From"]    = SMTP_FROM or SMTP_USER
        msg["To"]      = to_email
        html = f"""
        <div dir="rtl" style="font-family:Arial;max-width:480px;margin:0 auto;background:#141414;color:#ffffff;padding:32px;border-radius:12px;border:1px solid #2a2a2a;">
          <h2 style="color:#f0a000;letter-spacing:2px;text-align:center;">🌕 White Follow</h2>
          <p>مرحباً <strong>{username}</strong>،</p>
          <p style="color:#8c8c8c;">رمز التحقق الخاص بك:</p>
          <div style="background:#1a1a1a;border:2px solid #f0a000;border-radius:8px;padding:24px;text-align:center;margin:20px 0;">
            <span style="font-size:40px;font-weight:700;letter-spacing:10px;color:#f0a000;font-family:monospace;">{code}</span>
          </div>
          <p style="color:#666666;font-size:12px;text-align:center;">صالح لمدة 10 دقائق. لا تشاركه مع أحد.</p>
          <hr style="border:none;border-top:1px solid #2a2a2a;margin:16px 0;"/>
          <p style="color:#666666;font-size:11px;text-align:center;">White Follow — منصة خدمات التواصل الاجتماعي</p>
        </div>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, to_email, msg.as_string())
        log.info(f"[EMAIL] Sent OTP to {to_email}")
        return True
    except Exception as e:
        log.error("[EMAIL] send failed: %s", e)
        return False

@app.route("/auth/telegram", methods=["POST"])
def auth_telegram():
    """
    مصادقة مستخدم Telegram WebApp عبر initData.
    Frontend يرسل: { initData: "..." }
    """
    import urllib.parse
    data = request.get_json(silent=True) or {}
    init_data_raw = data.get("initData", "").strip()

    if not init_data_raw:
        return jsonify({"error": "initData مطلوب"}), 400

    # ── التحقق من صحة initData ──
    parsed = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
    received_hash = parsed.pop("hash", "")
    # بناء data-check-string
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if TELEGRAM_BOT_TOKEN and received_hash != expected_hash:
        return jsonify({"error": "بيانات تيليجرام غير صالحة"}), 401

    # ── استخراج بيانات المستخدم ──
    try:
        tg_user = json.loads(parsed.get("user", "{}"))
    except Exception:
        return jsonify({"error": "بيانات المستخدم غير صالحة"}), 400

    tg_id    = str(tg_user.get("id", ""))
    username = tg_user.get("username") or tg_user.get("first_name") or f"user_{tg_id}"

    if not tg_id:
        return jsonify({"error": "لم يتم التعرف على المستخدم"}), 400

    # ── إنشاء أو جلب المستخدم ──
    with get_db() as db:
        db.execute("""
            INSERT INTO users (telegram_id, username)
            VALUES (%s, %s)
            ON CONFLICT (telegram_id) DO NOTHING
        """, (tg_id, username))
        db.execute("UPDATE users SET last_seen=NOW() WHERE telegram_id=%s", (tg_id,))
        db.commit()

        user = db.execute("SELECT * FROM users WHERE telegram_id=%s", (tg_id,)).fetchone()
        if not user:
            return jsonify({"error": "خطأ في إنشاء الحساب"}), 500

        # توليد uid إذا كان ناقصاً
        if not user.get("uid"):
            new_uid = f"WF-{user['id']:06d}"
            db.execute("UPDATE users SET uid=%s WHERE id=%s", (new_uid, user["id"]))
            db.commit()
            user = db.execute("SELECT * FROM users WHERE id=%s", (user["id"],)).fetchone()

    user = dict(user)
    if user.get("is_banned"):
        return jsonify({"error": "الحساب محظور"}), 403

    token = _create_user_token(user["id"])
    uid   = user.get("uid") or f"WF-{user['id']:06d}"
    return jsonify({
        "ok": True, "token": token,
        "user": {
            "id": user["id"], "uid": uid,
            "email": user.get("email", ""),
            "username": user.get("username", ""),
            "balance": user.get("balance", 0)
        }
    })

@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    username = (data.get("username") or email.split("@")[0]).strip()

    if not email or not password or len(password) < 6:
        return jsonify({"error": "بيانات غير صالحة"}), 400

    with get_db() as db:
        # Block duplicate email
        if db.execute("SELECT id FROM users WHERE LOWER(email)=%s", (email,)).fetchone():
            return jsonify({"error": "هذا البريد مسجل مسبقاً. سجّل دخولك أو استخدم بريد آخر"}), 409
        # Block duplicate username
        if username and db.execute("SELECT id FROM users WHERE LOWER(username)=%s", (username.lower(),)).fetchone():
            return jsonify({"error": "اسم المستخدم مأخوذ، اختر اسماً آخر"}), 409

    # ── التسجيل الفوري بدون التحقق من البريد الإلكتروني ──
    # send_verification_email is intentionally DISABLED — users are auto-verified
    ph = hash_password(password)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO users (email, username, password_hash) VALUES (%s, %s, %s)",
            (email, username, ph)
        )
        user_id = cur.lastrowid
        uid = f"WF-{user_id:06d}"
        db.execute("UPDATE users SET uid=%s WHERE id=%s", (uid, user_id))
        db.commit()

    notify_admin(f"👤 مستخدم جديد: {email} (#{user_id})")
    token = _create_user_token(user_id)
    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "id": user_id,
            "uid": uid,
            "email": email,
            "username": username,
            "balance": 0
        }
    })

@app.route("/auth/verify-email", methods=["POST"])
def auth_verify_email():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code  = (data.get("code") or "").strip()

    if not email or not code:
        return jsonify({"error": "البريد والرمز مطلوبان"}), 400

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM email_verifications WHERE email=%s AND expires_at > NOW()",
            (email,)
        ).fetchone()

    if not row:
        return jsonify({"error": "الرمز منتهي الصلاحية. أعد التسجيل مرة أخرى"}), 400

    if row["code"] != code:
        return jsonify({"error": "رمز التحقق خاطئ"}), 400

    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()
        if existing:
            db.execute("DELETE FROM email_verifications WHERE email=%s", (email,))
            db.commit()
            return jsonify({"error": "هذا البريد مسجل مسبقاً. سجّل دخولك أو استخدم بريد آخر"}), 409
        cur = db.execute(
            "INSERT INTO users (email,username,password_hash) VALUES (%s,%s,%s)",
            (email, row["username"], row["password_hash"])
        )
        user_id = cur.lastrowid
        uid = f"WF-{user_id:06d}"
        db.execute("UPDATE users SET uid=%s WHERE id=%s", (uid, user_id))
        db.execute("DELETE FROM email_verifications WHERE email=%s", (email,))
        db.commit()

    token = _create_user_token(user_id)
    notify_admin(f"👤 مستخدم جديد (تحقق بريد): {email} (#{user_id})")
    return jsonify({"ok": True, "token": token,
                    "user": {"id": user_id, "uid": uid, "email": email,
                             "username": row["username"], "balance": 0}})

@app.route("/auth/resend-code", methods=["POST"])
def auth_resend_code():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    with get_db() as db:
        row = db.execute("SELECT * FROM email_verifications WHERE email=%s", (email,)).fetchone()
    if not row:
        return jsonify({"error": "لا يوجد طلب تسجيل لهذا البريد"}), 404
    code    = "".join(random.choices(string.digits, k=6))
    expires = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
    with get_db() as db:
        db.execute("UPDATE email_verifications SET code=%s,expires_at=%s WHERE email=%s",
                   (code, expires, email))
        db.commit()
    _send_verification_email(email, code, row["username"])
    return jsonify({"ok": True, "code": code, "message": "تم إعادة إرسال الرمز"})

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("email") or data.get("username") or "").strip().lower()
    password = data.get("password", "")
    ip = get_client_ip()

    with get_db() as db:
        # Accept login by email OR username
        user = db.execute(
            "SELECT * FROM users WHERE (LOWER(email)=%s OR LOWER(username)=%s) AND is_banned=0",
            (identifier, identifier)
        ).fetchone()

    if not user or user["password_hash"] != hash_password(password):
        log_security("user_login_failed", ip, identifier)
        return jsonify({"error": "بيانات خاطئة"}), 401

    with get_db() as db:
        db.execute("UPDATE users SET last_seen=NOW() WHERE id=%s", (user["id"],))
        db.commit()

    token = _create_user_token(user["id"])
    uid = user.get("uid") or f"WF-{user['id']:06d}"
    return jsonify({"ok": True, "token": token, "user": {
        "id": user["id"], "uid": uid,
        "email": user["email"],
        "username": user["username"], "balance": user["balance"]
    }})

@app.route("/auth/logout", methods=["POST"])
def auth_user_logout():
    token = request.headers.get("X-User-Token", "") or request.cookies.get("user_token", "")
    if token:
        with get_db() as db:
            db.execute("DELETE FROM user_sessions WHERE token=%s", (token,))
            db.commit()
    return jsonify({"ok": True})

@app.route("/auth/reset-request", methods=["POST"])
def auth_reset_request():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "البريد مطلوب"}), 400
    with get_db() as db:
        user = db.execute("SELECT id,username FROM users WHERE email=%s", (email,)).fetchone()
    if user:
        code    = "".join(random.choices(string.digits, k=6))
        expires = (datetime.datetime.utcnow() + datetime.timedelta(minutes=15)).isoformat()
        with get_db() as db:
            db.execute("DELETE FROM email_verifications WHERE email=%s", (email,))
            db.execute(
                "INSERT INTO email_verifications (email,username,password_hash,code,expires_at) VALUES (%s,%s,%s,%s,%s)",
                (email, user["username"], "", code, expires)
            )
            db.commit()
        _send_verification_email(email, code, user["username"])
    return jsonify({"ok": True, "message": "إذا كان البريد مسجلاً ستصلك رسالة"})

@app.route("/auth/reset-confirm", methods=["POST"])
def auth_reset_confirm():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    code     = (data.get("code") or "").strip()
    new_pass = data.get("password", "")
    if not email or not code or len(new_pass) < 6:
        return jsonify({"error": "بيانات غير مكتملة"}), 400
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM email_verifications WHERE email=%s AND expires_at > NOW()",
            (email,)
        ).fetchone()
    if not row or row["code"] != code:
        return jsonify({"error": "رمز خاطئ أو منتهي"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=%s WHERE email=%s",
                   (hash_password(new_pass), email))
        db.execute("DELETE FROM email_verifications WHERE email=%s", (email,))
        db.execute("DELETE FROM user_sessions WHERE user_id=(SELECT id FROM users WHERE email=%s)", (email,))
        db.commit()
    return jsonify({"ok": True, "message": "تم تغيير كلمة المرور. يرجى تسجيل الدخول"})

def _create_user_token(user_id):
    token = secrets.token_hex(24)
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat()
    with get_db() as db:
        db.execute("INSERT INTO user_sessions (token,user_id,expires_at) VALUES (%s,%s,%s)",
                   (token, user_id, expires))
        db.commit()
    return token

def get_current_user():
    token = request.headers.get("X-User-Token", "") or request.cookies.get("user_token", "")
    if not token:
        return None
    with get_db() as db:
        sess = db.execute(
            "SELECT user_id FROM user_sessions WHERE token=%s AND expires_at > NOW()",
            (token,)
        ).fetchone()
        if not sess:
            return None
        user = db.execute("SELECT * FROM users WHERE id=%s AND is_banned=0",
                          (sess["user_id"],)).fetchone()
    return dict(user) if user else None

def require_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "يجب تسجيل الدخول"}), 401
        request.current_user = user
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────
# User Endpoints
# ─────────────────────────────────────────────
@app.route("/user/me", methods=["GET"])
@require_user
def user_me():
    u = request.current_user
    return jsonify({
        "id": u["id"], "uid": u.get("uid") or f"WF-{u['id']:06d}",
        "email": u["email"], "username": u["username"],
        "balance": u["balance"], "language": u["language"],
        "joined_at": u["joined_at"], "last_seen": u["last_seen"],
        "orders_count": u["orders_count"],
        "total_charged": u.get("total_charged", 0),
        "total_spent":   u.get("total_spent", 0)
    })

@app.route("/user/change-password", methods=["POST"])
@require_user
def user_change_password():
    u = request.current_user
    data = request.get_json(silent=True) or {}
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not old_pw or not new_pw or len(new_pw) < 6:
        return jsonify({"error": "بيانات غير صالحة"}), 400
    if hash_password(old_pw) != u["password_hash"]:
        return jsonify({"error": "كلمة المرور القديمة خاطئة"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(new_pw), u["id"]))
        db.commit()
    return jsonify({"ok": True, "message": "تم تغيير كلمة المرور"})

@app.route("/user/logout-all", methods=["POST"])
@require_user
def user_logout_all():
    u = request.current_user
    with get_db() as db:
        db.execute("DELETE FROM user_sessions WHERE user_id=%s", (u["id"],))
        db.commit()
    return jsonify({"ok": True, "message": "تم تسجيل الخروج من كل الأجهزة"})

@app.route("/user/orders", methods=["GET"])
@require_user
def user_orders():
    user = request.current_user
    with get_db() as db:
        rows = db.execute("""
            SELECT o.*, s.name_ar as service_name, s.name_en,
                   s.image_url as service_image
            FROM orders o
            LEFT JOIN services s ON s.id=o.service_id
            WHERE o.user_id=%s ORDER BY o.created_at DESC LIMIT 200
        """, (user["id"],)).fetchall()
    orders = []
    for r in rows:
        d = dict(r)
        d["service_name"] = d.get("service_name") or d.get("name_en") or f"خدمة #{d.get('service_id','')}"
        d["charge"] = d.get("price", 0)
        d["start_count"] = d.get("start_count") or 0
        orders.append(d)
    return jsonify({"ok": True, "orders": orders})

@app.route("/user/language", methods=["POST"])
@require_user
def user_set_language():
    data = request.get_json(silent=True) or {}
    lang = data.get("lang", "ar")
    if lang not in ("ar", "en"):
        return jsonify({"error": "invalid lang"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET language=%s WHERE id=%s", (lang, request.current_user["id"]))
        db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# Services (Public)
# ─────────────────────────────────────────────
@app.route("/services", methods=["GET"])
def services_public():
    category  = request.args.get("category", "").strip()
    search    = request.args.get("search", "").strip()
    lang      = request.args.get("lang", "ar")

    where_clauses = ["s.is_active=1"]
    params = []

    if category:
        where_clauses.append("s.category_id=%s")
        params.append(category)

    if search:
        pat = f"%{search}%"
        where_clauses.append(
            "(s.name_ar LIKE %s OR s.name_en LIKE %s OR s.description_ar LIKE %s OR s.description_en LIKE %s)"
        )
        params.extend([pat, pat, pat, pat])

    where_sql = " AND ".join(where_clauses)

    with get_db() as db:
        svcs = db.execute(f"""
            SELECT s.id,
                   CASE WHEN %s = 'ar' THEN s.name_ar ELSE COALESCE(NULLIF(s.name_en,''),s.name_ar) END as name,
                   s.final_price as rate,
                   s.provider_price as original_rate,
                   s.min_qty as min,
                   s.max_qty as max,
                   s.category_id as category,
                   s.type,
                   s.image_url as image,
                   CASE WHEN %s = 'ar' THEN s.description_ar ELSE COALESCE(NULLIF(s.description_en,''),s.description_ar) END as description,
                   s.estimated_time,
                   s.provider_service_id,
                   s.name_ar, s.name_en,
                   s.image_url as image_url
            FROM services s
            WHERE {where_sql}
            ORDER BY s.category_id, s.id
        """, [lang, lang] + params).fetchall()

        if not category and not search:
            cats = db.execute(
                "SELECT * FROM categories WHERE is_active=1 ORDER BY sort_order"
            ).fetchall()
        else:
            cats = []

    result_svcs = []
    for s in svcs:
        d = dict(s)
        d["rate"] = d.get("rate") or 0
        result_svcs.append(d)

    resp = {"ok": True, "services": result_svcs, "data": result_svcs}
    if cats:
        resp["categories"] = [dict(c) for c in cats]
    return jsonify(resp)

# ─────────────────────────────────────────────
# API — Live Prices by provider_service_id
# ─────────────────────────────────────────────
@app.route("/api/prices", methods=["GET"])
def api_prices():
    """
    Returns a dict { provider_service_id: final_price }
    for all active services. Used by frontend to update
    hardcoded prices dynamically without page reload.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT provider_service_id, final_price FROM services WHERE is_active=1 AND provider_service_id IS NOT NULL AND provider_service_id != ''"
        ).fetchall()
    prices = {r["provider_service_id"]: r["final_price"] for r in rows}
    return jsonify({"ok": True, "prices": prices})

# ─────────────────────────────────────────────
# Orders (User)
# ─────────────────────────────────────────────
@app.route("/order/place", methods=["POST"])
@require_user
def place_order():
    user = request.current_user
    data = request.get_json(silent=True) or {}

    # Accept 'service' (provider_service_id) OR 'service_id' (local id)
    service_id        = data.get("service_id")
    provider_svc_id   = data.get("service")   # frontend sends this
    link              = (data.get("link") or "").strip()
    quantity          = int(data.get("quantity", 0))

    # Validate link — only enforce URL format for social media services
    import re as _re
    if not link:
        return jsonify({"error": "الرابط أو المعرف مطلوب"}), 400
    if not quantity:
        return jsonify({"error": "الكمية مطلوبة"}), 400

    with get_db() as db:
        if service_id:
            svc = db.execute("SELECT * FROM services WHERE id=%s AND is_active=1", (service_id,)).fetchone()
        elif provider_svc_id:
            svc = db.execute(
                "SELECT * FROM services WHERE provider_service_id=%s AND is_active=1",
                (str(provider_svc_id),)
            ).fetchone()
            if not svc:
                # Try by local id if it's numeric
                try:
                    svc = db.execute("SELECT * FROM services WHERE id=%s AND is_active=1",
                                     (int(provider_svc_id),)).fetchone()
                except Exception:
                    pass
        else:
            return jsonify({"error": "معرف الخدمة مطلوب"}), 400

        if not svc:
            return jsonify({"error": "الخدمة غير موجودة"}), 404

        if quantity < svc["min_qty"] or quantity > svc["max_qty"]:
            return jsonify({"error": f"الكمية يجب بين {svc['min_qty']} و {svc['max_qty']}"}), 400

        total_cost = round(svc["final_price"] * quantity / 1000, 4)
        if user["balance"] < total_cost:
            return jsonify({"error": f"❌ رصيد غير كافٍ — الرصيد المطلوب: ${total_cost:.3f} | رصيدك الحالي: ${user['balance']:.3f}"}), 402

        # Deduct balance BEFORE placing
        db.execute("UPDATE users SET balance=balance-%s, total_spent=total_spent+%s, orders_count=orders_count+1 WHERE id=%s",
                   (total_cost, total_cost, user["id"]))
        db.commit()

        provider = db.execute("SELECT * FROM providers WHERE id=%s AND is_active=1",
                              (svc["provider_id"],)).fetchone()

    provider_order_id = None
    place_failed = False
    if provider:
        try:
            api_url = provider["api_url"]
            api_key = provider["api_key"]
            if "darkfollow" in api_url.lower():
                base_url = api_url.rstrip("/").split("?")[0]
                encoded_link = requests.utils.quote(link, safe="")
                url = f"{base_url}?action=add&key={api_key}&service={svc['provider_service_id']}&link={encoded_link}&quantity={quantity}"
                log.info(f"[order] Dark Follow detected, using GET request")
                log.info(f"[order] Dark Follow URL: {url[:80]}...")
                resp = requests.get(url, timeout=15)
            else:
                resp = requests.post(api_url, data={
                    "key": api_key, "action": "add",
                    "service": svc["provider_service_id"],
                    "link": link, "quantity": quantity
                }, timeout=15)
            r = resp.json()
            provider_order_id = str(r.get("order", ""))
            if not provider_order_id or "error" in r:
                place_failed = True
                log.warning(f"[order] provider returned error: {r}")
        except Exception as e:
            log.error(f"[order] provider error: {e}")
            place_failed = True

    # Refund if provider failed
    if place_failed and provider:
        with get_db() as db:
            db.execute("UPDATE users SET balance=balance+%s, total_spent=total_spent-%s, orders_count=MAX(0,orders_count-1) WHERE id=%s",
                       (total_cost, total_cost, user["id"]))
            db.commit()
        return jsonify({"error": "فشل إرسال الطلب للمزود، يرجى المحاولة لاحقاً"}), 503

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO orders (user_id,service_id,provider_order_id,link,quantity,price,status,remains,start_count)
            VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,0)
        """, (user["id"], svc["id"], provider_order_id, link, quantity, total_cost, quantity))
        order_id = cur.lastrowid
        db.commit()

    notify_admin(f"🛒 طلب جديد #{order_id}\n👤 المستخدم: {user['email']}\n💰 {total_cost} $")
    return jsonify({"ok": True, "order_id": order_id, "cost": total_cost,
                    "order": {
                        "id": order_id, "service": svc["id"],
                        "service_name": svc["name_ar"],
                        "service_image": svc.get("image_url",""),
                        "quantity": quantity, "link": link,
                        "price": total_cost, "charge": total_cost,
                        "status": "pending", "remains": quantity,
                        "start_count": 0
                    }})

@app.route("/order/status/<int:order_id>", methods=["GET"])
@require_user
def order_status(order_id):
    user = request.current_user
    with get_db() as db:
        order = db.execute(
            "SELECT * FROM orders WHERE id=%s AND user_id=%s", (order_id, user["id"])
        ).fetchone()
    if not order:
        return jsonify({"error": "غير موجود"}), 404
    return jsonify(dict(order))

# ── Frontend alias: GET /orders%suser_id=... ──
@app.route("/orders", methods=["GET"])
@require_user
def orders_alias():
    user = request.current_user
    with get_db() as db:
        rows = db.execute("""
            SELECT o.*, s.name_ar as service_name, s.name_en,
                   s.image_url as service_image
            FROM orders o
            LEFT JOIN services s ON s.id=o.service_id
            WHERE o.user_id=%s ORDER BY o.created_at DESC LIMIT 200
        """, (user["id"],)).fetchall()
    orders = []
    for r in rows:
        d = dict(r)
        d["service_name"] = d.get("service_name") or d.get("name_en") or f"خدمة #{d.get('service_id','')}"
        d["charge"] = d.get("price", 0)
        orders.append(d)
    return jsonify({"ok": True, "orders": orders, "data": orders})

# ── Frontend: GET /orders/<id> ──
@app.route("/orders/<int:order_id>", methods=["GET"])
@require_user
def get_order_detail(order_id):
    user = request.current_user
    with get_db() as db:
        row = db.execute("""
            SELECT o.*, s.name_ar as service_name, s.name_en,
                   s.image_url as service_image
            FROM orders o
            LEFT JOIN services s ON s.id=o.service_id
            WHERE o.id=%s AND o.user_id=%s
        """, (order_id, user["id"])).fetchone()
    if not row:
        return jsonify({"error": "الطلب غير موجود"}), 404
    d = dict(row)
    d["service_name"] = d.get("service_name") or d.get("name_en") or f"خدمة #{d.get('service_id','')}"
    d["charge"] = d.get("price", 0)
    d["start_count"] = d.get("start_count") or 0
    return jsonify({"ok": True, "order": d})

# ── Frontend: POST /orders/<id>/cancel ──
@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@require_user
def cancel_order(order_id):
    user = request.current_user
    with get_db() as db:
        order = db.execute(
            "SELECT o.*, p.api_url, p.api_key FROM orders o "
            "JOIN services s ON s.id=o.service_id "
            "JOIN providers p ON p.id=s.provider_id "
            "WHERE o.id=%s AND o.user_id=%s", (order_id, user["id"])
        ).fetchone()
    if not order:
        return jsonify({"error": "الطلب غير موجود"}), 404
    if order["status"] not in ("pending", "active"):
        return jsonify({"error": "لا يمكن إلغاء هذا الطلب"}), 400
    if (order["start_count"] or 0) > 0:
        return jsonify({"error": "الطلب بدأ التنفيذ، لا يمكن إلغاؤه"}), 400
    # Try to cancel on provider first (best-effort, don't block on failure)
    if order.get("provider_order_id"):
        try:
            api_url = order["api_url"]
            api_key = order["api_key"]
            if "darkfollow" in api_url.lower():
                base_url = api_url.rstrip("/").split("?")[0]
                cancel_url = f"{base_url}?action=cancel&key={api_key}&order={order['provider_order_id']}"
                requests.get(cancel_url, timeout=10)
            else:
                requests.post(api_url, data={"key": api_key, "action": "cancel", "order": order["provider_order_id"]}, timeout=10)
        except Exception as e:
            log.warning(f"[cancel] provider cancel failed (still refunding): {e}")
    with get_db() as db:
        db.execute("UPDATE orders SET status='cancelled', updated_at=NOW() WHERE id=%s", (order_id,))
        db.execute("UPDATE users SET balance=balance+%s, orders_count=MAX(0,orders_count-1) WHERE id=%s",
                   (order["price"], user["id"]))
        db.commit()
    return jsonify({"ok": True, "message": "تم إلغاء الطلب واسترداد الرصيد"})

# ── Frontend alias: POST /orders/create ──
@app.route("/orders/create", methods=["POST"])
@require_user
def orders_create_alias():
    return place_order()

# ─────────────────────────────────────────────
# Payments (User)
# ─────────────────────────────────────────────
@app.route("/payment/gateways", methods=["GET"])
def payment_gateways():
    lang = request.args.get("lang", "ar")
    with get_db() as db:
        gws = db.execute("SELECT * FROM payment_gateways WHERE is_active=1").fetchall()
    result = []
    for g in gws:
        d = dict(g)
        d["details"] = d[f"details_{lang}"] if lang == "ar" else d["details_en"]
        result.append(d)
    return jsonify({"gateways": result})

@app.route("/payment/history", methods=["GET"])
@require_user
def payment_history():
    user = request.current_user
    with get_db() as db:
        rows = db.execute("""
            SELECT id, amount, method, status, created_at
            FROM payments
            WHERE user_id=%s AND status='approved'
            ORDER BY created_at DESC LIMIT 100
        """, (user["id"],)).fetchall()
    return jsonify({"ok": True, "payments": [dict(r) for r in rows]})

@app.route("/payment/submit", methods=["POST"])
@require_user
def payment_submit():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0))
    method = data.get("method", "")
    tx_id  = data.get("transaction_id", "")
    proof  = data.get("proof_url", "")

    min_dep = float(get_setting("min_deposit", "1"))
    if amount < min_dep:
        return jsonify({"error": f"الحد الأدنى للإيداع {min_dep}$"}), 400

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO payments (user_id,amount,method,transaction_id,proof_url)
            VALUES (%s,%s,%s,%s,%s)
        """, (user["id"], amount, method, tx_id, proof))
        pay_id = cur.lastrowid
        db.commit()

    notify_admin(f"💳 طلب دفع جديد #{pay_id}\n👤 {user['email']}\n💰 {amount}$\n📝 {method}")
    return jsonify({"ok": True, "payment_id": pay_id})

# ─────────────────────────────────────────────
# Tickets (User)
# ─────────────────────────────────────────────
@app.route("/tickets", methods=["GET"])
@require_user
def user_tickets():
    user = request.current_user
    with get_db() as db:
        tickets = db.execute(
            "SELECT * FROM tickets WHERE user_id=%s ORDER BY updated_at DESC", (user["id"],)
        ).fetchall()
    return jsonify({"tickets": [dict(t) for t in tickets]})

@app.route("/tickets/create", methods=["POST"])
@require_user
def create_ticket():
    user = request.current_user
    data = request.get_json(silent=True) or {}
    subject = data.get("subject", "").strip()
    message = data.get("message", "").strip()
    if not subject or not message:
        return jsonify({"error": "موضوع ورسالة مطلوبان"}), 400

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO tickets (user_id,subject) VALUES (%s,%s)", (user["id"], subject)
        )
        ticket_id = cur.lastrowid
        db.execute(
            "INSERT INTO ticket_messages (ticket_id,sender_type,message) VALUES (%s,%s,%s)",
            (ticket_id, "user", message)
        )
        db.commit()

    notify_admin(f"🎫 تذكرة جديدة #{ticket_id}\n👤 {user['email']}\n📌 {subject}")
    return jsonify({"ok": True, "ticket_id": ticket_id})

@app.route("/tickets/<int:ticket_id>/messages", methods=["GET"])
@require_user
def ticket_messages(ticket_id):
    user = request.current_user
    with get_db() as db:
        ticket = db.execute(
            "SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, user["id"])
        ).fetchone()
        if not ticket:
            return jsonify({"error": "غير موجود"}), 404
        msgs = db.execute(
            "SELECT id, ticket_id, sender_type, message, created_at FROM ticket_messages WHERE ticket_id=%s ORDER BY created_at",
            (ticket_id,)
        ).fetchall()
    result = []
    for m in msgs:
        d = dict(m)
        # Ensure sender_type is always 'user' or 'admin'
        if d.get("sender_type") not in ("user", "admin"):
            d["sender_type"] = "user"
        result.append(d)
    return jsonify({"ok": True, "ticket": dict(ticket), "messages": result})

@app.route("/tickets/<int:ticket_id>/reply", methods=["POST"])
@require_user
def ticket_reply(ticket_id):
    user = request.current_user
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "رسالة مطلوبة"}), 400

    with get_db() as db:
        ticket = db.execute(
            "SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, user["id"])
        ).fetchone()
        if not ticket:
            return jsonify({"error": "غير موجود"}), 404
        db.execute(
            "INSERT INTO ticket_messages (ticket_id,sender_type,message) VALUES (%s,%s,%s)",
            (ticket_id, "user", message)
        )
        db.execute("UPDATE tickets SET updated_at=NOW(), status='open' WHERE id=%s", (ticket_id,))
        db.commit()

    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# Translations (Public)
# ─────────────────────────────────────────────
@app.route("/translations/<lang>", methods=["GET"])
def get_translations(lang):
    if lang not in ("ar", "en"):
        return jsonify({"error": "invalid lang"}), 400
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM translations WHERE lang=%s", (lang,)).fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})

@app.route("/settings/public", methods=["GET"])
def public_settings():
    keys = ["site_name", "currency", "min_deposit", "maintenance_mode", "faq_ar", "faq_en", "tos_ar", "tos_en", "global_markup_type", "global_markup_value"]
    with get_db() as db:
        rows = db.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('%s'*len(keys))})", keys).fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})

# ─────────────────────────────────────────────
# ADMIN — Dashboard Stats
# ─────────────────────────────────────────────
@app.route("/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    with get_db() as db:
        total_users   = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_orders  = db.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
        total_revenue = db.execute("SELECT SUM(price) as s FROM orders WHERE status!='cancelled'").fetchone()["s"] or 0
        pending_pay   = db.execute("SELECT COUNT(*) as c FROM payments WHERE status='pending'").fetchone()["c"]
        open_tickets  = db.execute("SELECT COUNT(*) as c FROM tickets WHERE status='open'").fetchone()["c"]
        today_orders  = db.execute(
            "SELECT COUNT(*) as c FROM orders WHERE created_at::date = CURRENT_DATE"
        ).fetchone()["c"]
        monthly = db.execute("""
            SELECT TO_CHAR(created_at::timestamp, 'YYYY-MM') as month, SUM(price) as rev
            FROM orders WHERE status!='cancelled'
            GROUP BY month ORDER BY month DESC LIMIT 12
        """).fetchall()
        active_orders = db.execute(
            "SELECT COUNT(*) as c FROM orders WHERE status IN ('pending','active','partial')"
        ).fetchone()["c"]
        total_user_balance = db.execute(
            "SELECT COALESCE(SUM(balance),0) as s FROM users"
        ).fetchone()["s"] or 0
    return jsonify({
        "ok": True,
        "stats": {
            "total_users": total_users,
            "total_orders": total_orders,
            "total_revenue": round(total_revenue, 2),
            "pending_payments": pending_pay,
            "open_tickets": open_tickets,
            "orders_today": today_orders,
            "active_orders": active_orders,
            "total_user_balance": round(total_user_balance, 2),
            "monthly_revenue": [dict(r) for r in monthly]
        }
    })

# ─────────────────────────────────────────────
# ADMIN — Users
# ─────────────────────────────────────────────
@app.route("/admin/users", methods=["GET"])
@require_admin
def admin_users():
    page   = int(request.args.get("page", 1))
    limit  = min(int(request.args.get("limit", 50)), 200)
    offset = (page - 1) * limit
    search = (request.args.get("search") or request.args.get("q", "")).strip()
    with get_db() as db:
        if search:
            pat = f"%{search}%"
            users = db.execute(
                """SELECT * FROM users
                   WHERE email LIKE %s OR username LIKE %s
                      OR CAST(id AS TEXT) = %s
                   ORDER BY joined_at DESC LIMIT %s OFFSET %s""",
                (pat, pat, search, limit, offset)
            ).fetchall()
            total = db.execute(
                """SELECT COUNT(*) as c FROM users
                   WHERE email LIKE %s OR username LIKE %s
                      OR CAST(id AS TEXT) = %s""",
                (pat, pat, search)
            ).fetchone()["c"]
        else:
            users = db.execute(
                "SELECT * FROM users ORDER BY joined_at DESC LIMIT %s OFFSET %s", (limit, offset)
            ).fetchall()
            total = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    result = []
    for u in users:
        d = dict(u)
        if not d.get('uid') and d.get('username') and str(d['username']).startswith('WF-'):
            d['uid'] = d['username']
        elif not d.get('uid'):
            d['uid'] = str(d['id'])
        result.append(d)
    return jsonify({"ok": True, "users": result, "total": total, "page": page})

@app.route("/admin/users/<int:uid>/balance", methods=["POST"])
@require_admin
def admin_add_balance(uid):
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0))
    operation = data.get("operation", "add")
    note = data.get("note", "")
    if amount < 0 and operation == "add":
        operation = "subtract"
        amount = abs(amount)
    with get_db() as db:
        user = db.execute("SELECT balance FROM users WHERE id=%s", (uid,)).fetchone()
        if not user:
            return jsonify({"error": "المستخدم غير موجود"}), 404
        current = float(user["balance"] or 0)
        if operation == "add":
            new_bal = current + amount
            db.execute("UPDATE users SET balance=%s, total_charged=total_charged+%s WHERE id=%s",
                       (new_bal, amount, uid))
        elif operation == "subtract":
            new_bal = max(0, current - amount)
            db.execute("UPDATE users SET balance=%s WHERE id=%s", (new_bal, uid))
        elif operation == "set":
            new_bal = amount
            diff = max(0, amount - current)
            db.execute("UPDATE users SET balance=%s, total_charged=total_charged+%s WHERE id=%s",
                       (new_bal, diff, uid))
        else:
            return jsonify({"error": "عملية غير صحيحة"}), 400
        db.commit()
    log.info(f"[admin] balance {operation} uid={uid} amount={amount} note={note}")
    return jsonify({"ok": True, "new_balance": new_bal})

@app.route("/admin/users/<int:uid>/ban", methods=["POST"])
@require_admin
def admin_ban_user(uid):
    data = request.get_json(silent=True) or {}
    banned = int(data.get("banned", 1))
    with get_db() as db:
        db.execute("UPDATE users SET is_banned=%s WHERE id=%s", (banned, uid))
        db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# ADMIN — Providers
# ─────────────────────────────────────────────
@app.route("/admin/providers", methods=["GET"])
@require_admin
def admin_get_providers():
    with get_db() as db:
        rows = db.execute("SELECT * FROM providers ORDER BY id").fetchall()
    return jsonify({"providers": [dict(r) for r in rows]})

@app.route("/admin/providers", methods=["POST"])
@require_admin
def admin_add_provider():
    data = request.get_json(silent=True) or {}
    name    = data.get("name", "").strip()
    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    if not name or not api_url or not api_key:
        return jsonify({"error": "name, api_url, api_key مطلوبة"}), 400
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO providers (name,api_url,api_key) VALUES (%s,%s,%s)",
            (name, api_url, api_key)
        )
        db.commit()
        provider_id = cur.lastrowid
    return jsonify({"ok": True, "id": provider_id})

@app.route("/admin/providers/<int:pid>", methods=["PUT"])
@require_admin
def admin_update_provider(pid):
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("""
            UPDATE providers SET name=COALESCE(%s,name),
            api_url=COALESCE(%s,api_url), api_key=COALESCE(%s,api_key),
            is_active=COALESCE(%s,is_active) WHERE id=%s
        """, (data.get("name"), data.get("api_url"), data.get("api_key"), data.get("is_active"), pid))
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/providers/<int:pid>/test", methods=["POST"])
@require_admin
def admin_test_provider(pid):
    with get_db() as db:
        p = db.execute("SELECT * FROM providers WHERE id=%s", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "غير موجود"}), 404
    try:
        api_url = p["api_url"]
        api_key = p["api_key"]
        if "darkfollow" in api_url.lower():
            base_url = api_url.rstrip("/").split("?")[0]
            url = f"{base_url}?action=balance&key={api_key}"
            log.info(f"[test_provider] Dark Follow detected, using GET request")
            resp = requests.get(url, timeout=10)
        else:
            resp = requests.post(api_url, data={"key": api_key, "action": "balance"}, timeout=10)
        data = resp.json()
        balance = data.get("balance", data.get("Balance", data.get("funds", 0)))
        with get_db() as db:
            db.execute("UPDATE providers SET balance=%s WHERE id=%s", (float(balance), pid))
            db.commit()
        return jsonify({"ok": True, "balance": balance, "raw": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/admin/providers/<int:pid>/services", methods=["GET"])
@require_admin
def admin_fetch_provider_services(pid):
    with get_db() as db:
        p = db.execute("SELECT * FROM providers WHERE id=%s", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "غير موجود"}), 404
    try:
        api_url = p["api_url"]
        api_key = p["api_key"]
        if "darkfollow" in api_url.lower():
            base_url = api_url.rstrip("/").split("?")[0]
            url = f"{base_url}?action=services&key={api_key}"
            log.info(f"[fetch_services] Dark Follow detected, using GET request")
            log.info(f"[fetch_services] Dark Follow URL: {url[:60]}...")
            resp = requests.get(url, timeout=20)
        else:
            resp = requests.post(api_url, data={"key": api_key, "action": "services"}, timeout=20)
        data = resp.json()
        return jsonify({"ok": True, "services": data[:200] if isinstance(data, list) else data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─────────────────────────────────────────────
# ADMIN — Categories & Services
# ─────────────────────────────────────────────
@app.route("/admin/categories", methods=["GET"])
@require_admin
def admin_categories():
    with get_db() as db:
        rows = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    return jsonify({"categories": [dict(r) for r in rows]})

@app.route("/admin/categories", methods=["POST"])
@require_admin
def admin_add_category():
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO categories (name_ar,name_en,icon,sort_order) VALUES (%s,%s,%s,%s)",
            (data.get("name_ar",""), data.get("name_en",""), data.get("icon","📦"), data.get("sort_order",0))
        )
        db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})

@app.route("/admin/categories/<int:cid>", methods=["PUT"])
@require_admin
def admin_update_category(cid):
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("""UPDATE categories SET name_ar=COALESCE(%s,name_ar),
            name_en=COALESCE(%s,name_en), icon=COALESCE(%s,icon),
            sort_order=COALESCE(%s,sort_order), is_active=COALESCE(%s,is_active) WHERE id=%s""",
            (data.get("name_ar"), data.get("name_en"), data.get("icon"),
             data.get("sort_order"), data.get("is_active"), cid))
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/categories/<int:cid>/markup", methods=["POST"])
@require_admin
def admin_category_markup(cid):
    """Apply markup to all services in a category"""
    data = request.get_json(silent=True) or {}
    markup_type  = data.get("markup_type", "percent")
    markup_value = float(data.get("markup_value", 0))
    with get_db() as db:
        # Save markup on category
        db.execute("UPDATE categories SET markup_type=%s, markup_value=%s WHERE id=%s",
                   (markup_type, markup_value, cid))
        # Apply to all services in this category
        svcs = db.execute("SELECT id, provider_price FROM services WHERE category_id=%s", (cid,)).fetchall()
        for svc in svcs:
            new_price = calc_price(svc["provider_price"], markup_type, markup_value)
            db.execute("UPDATE services SET markup_type=%s, markup_value=%s, final_price=%s WHERE id=%s",
                       (markup_type, markup_value, new_price, svc["id"]))
        db.commit()
    return jsonify({"ok": True, "updated": len(svcs)})

@app.route("/admin/services", methods=["GET"])
@require_admin
def admin_services():
    page = int(request.args.get("page", 1))
    limit = 100
    offset = (page - 1) * limit
    with get_db() as db:
        rows = db.execute("""
            SELECT s.*, c.name_ar as cat_name, p.name as prov_name
            FROM services s
            LEFT JOIN categories c ON c.id=s.category_id
            LEFT JOIN providers p ON p.id=s.provider_id
            ORDER BY s.category_id, s.id LIMIT %s OFFSET %s
        """, (limit, offset)).fetchall()
        total = db.execute("SELECT COUNT(*) as c FROM services").fetchone()["c"]
    return jsonify({"services": [dict(r) for r in rows], "total": total})

@app.route("/admin/services", methods=["POST"])
@require_admin
def admin_add_service():
    data = request.get_json(silent=True) or {}
    provider_price = float(data.get("provider_price", 0))
    markup_type = data.get("markup_type", "percent")
    markup_value = float(data.get("markup_value", 0))
    final_price = calc_price(provider_price, markup_type, markup_value)

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO services (provider_id, provider_service_id, category_id,
            name_ar, name_en, description_ar, description_en,
            min_qty, max_qty, provider_price, markup_type, markup_value,
            final_price, estimated_time, type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.get("provider_id"), data.get("provider_service_id"), data.get("category_id"),
            data.get("name_ar",""), data.get("name_en",""),
            data.get("description_ar",""), data.get("description_en",""),
            data.get("min_qty",10), data.get("max_qty",10000),
            provider_price, markup_type, markup_value, final_price,
            data.get("estimated_time",""), data.get("type","Default")
        ))
        db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "final_price": final_price})

@app.route("/admin/services/<int:sid>", methods=["PUT"])
@require_admin
def admin_update_service(sid):
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        svc = db.execute("SELECT * FROM services WHERE id=%s", (sid,)).fetchone()
        if not svc:
            return jsonify({"error": "غير موجود"}), 404
        provider_price = float(data.get("provider_price", svc["provider_price"]))
        markup_type    = data.get("markup_type", svc["markup_type"])
        markup_value   = float(data.get("markup_value", svc["markup_value"]))
        final_price    = calc_price(provider_price, markup_type, markup_value)
        db.execute("""UPDATE services SET
            category_id=COALESCE(%s,category_id), name_ar=COALESCE(%s,name_ar),
            name_en=COALESCE(%s,name_en), description_ar=COALESCE(%s,description_ar),
            description_en=COALESCE(%s,description_en), min_qty=COALESCE(%s,min_qty),
            max_qty=COALESCE(%s,max_qty), provider_price=%s,
            markup_type=%s, markup_value=%s, final_price=%s,
            estimated_time=COALESCE(%s,estimated_time),
            is_active=COALESCE(%s,is_active), updated_at=NOW()
            WHERE id=%s""", (
            data.get("category_id"), data.get("name_ar"), data.get("name_en"),
            data.get("description_ar"), data.get("description_en"),
            data.get("min_qty"), data.get("max_qty"),
            provider_price, markup_type, markup_value, final_price,
            data.get("estimated_time"), data.get("is_active"), sid
        ))
        db.commit()
    return jsonify({"ok": True, "final_price": final_price})

@app.route("/admin/services/import", methods=["POST"])
@require_admin
def admin_import_services():
    data = request.get_json(silent=True) or {}
    provider_id  = data.get("provider_id")
    markup_type  = data.get("markup_type", "percent")
    markup_value = float(data.get("markup_value", 0))
    services     = data.get("services", [])
    category_id  = data.get("category_id")
    imported = 0

    with get_db() as db:
        for s in services:
            try:
                provider_price = float(s.get("rate", s.get("price", 0)))
                final_price    = calc_price(provider_price, markup_type, markup_value)
                db.execute("""
                    INSERT INTO services
                    (provider_id, provider_service_id, category_id, name_ar, name_en,
                     min_qty, max_qty, provider_price, markup_type, markup_value,
                     final_price, type)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    provider_id, str(s.get("service","")), category_id,
                    s.get("name",""), s.get("name",""),
                    int(s.get("min",10)), int(s.get("max",10000)),
                    provider_price, markup_type, markup_value, final_price,
                    s.get("type","Default")
                ))
                imported += 1
            except Exception as e:
                log.error(f"import service error: {e}")
        db.commit()
    return jsonify({"ok": True, "imported": imported})

@app.route("/admin/services/reprice", methods=["POST"])
@require_admin
def admin_reprice_services():
    """Update all service prices from provider and apply markup"""
    data = request.get_json(silent=True) or {}
    provider_id = data.get("provider_id")
    with get_db() as db:
        p = db.execute("SELECT * FROM providers WHERE id=%s", (provider_id,)).fetchone()
        if not p:
            return jsonify({"error": "Provider not found"}), 404
    try:
        api_url = p["api_url"]
        api_key = p["api_key"]
        if "darkfollow" in api_url.lower():
            base_url = api_url.rstrip("/").split("?")[0]
            url = f"{base_url}?action=services&key={api_key}"
            resp = requests.get(url, timeout=20)
        else:
            resp = requests.post(api_url, data={"key": api_key, "action": "services"}, timeout=20)
        provider_svcs = {str(s["service"]): s for s in resp.json()}
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    updated = 0
    with get_db() as db:
        svcs = db.execute("SELECT * FROM services WHERE provider_id=%s", (provider_id,)).fetchall()
        for svc in svcs:
            ps = provider_svcs.get(str(svc["provider_service_id"]))
            if ps:
                new_provider_price = float(ps.get("rate", ps.get("price", svc["provider_price"])))
                new_final = calc_price(new_provider_price, svc["markup_type"], svc["markup_value"])
                db.execute("""UPDATE services SET provider_price=%s, final_price=%s,
                    updated_at=NOW() WHERE id=%s""",
                    (new_provider_price, new_final, svc["id"]))
                updated += 1
        db.commit()
    return jsonify({"ok": True, "updated": updated})


# ─────────────────────────────────────────────
# ADMIN — مزامنة الخدمات (لوحة التحكم)
# ─────────────────────────────────────────────

@app.route("/admin/sync-prices", methods=["POST"])
@require_admin
def admin_sync_prices():
    """يجيب أحدث أسعار من دارك فولو ويحدث final_price مع الربح"""
    with get_db() as db:
        providers = db.execute("SELECT * FROM providers WHERE is_active=1").fetchall()
    if not providers:
        return jsonify({"error": "لا يوجد provider مفعل"}), 404
    total_updated = 0
    errors = []
    for p in providers:
        try:
            api_url = p["api_url"]
            api_key = p["api_key"]
            if "darkfollow" in api_url.lower():
                base_url = api_url.rstrip("/").split("?")[0]
                url = f"{base_url}?action=services&key={api_key}"
                resp = requests.get(url, timeout=20)
            else:
                resp = requests.post(api_url, data={"key": api_key, "action": "services"}, timeout=20)
            remote_list = resp.json()
            if not isinstance(remote_list, list):
                errors.append(f"Provider #{p['id']}: استجابة غير صالحة")
                continue
            provider_svcs = {str(s.get("service", s.get("id", ""))): s for s in remote_list}
            with get_db() as db:
                svcs = db.execute("SELECT * FROM services WHERE provider_id=%s", (p["id"],)).fetchall()
                updated = 0
                for svc in svcs:
                    ps = provider_svcs.get(str(svc["provider_service_id"]))
                    if ps:
                        new_price = float(ps.get("rate", ps.get("price", svc["provider_price"])))
                        new_final = calc_price(new_price, svc["markup_type"], svc["markup_value"])
                        db.execute(
                            "UPDATE services SET provider_price=%s, final_price=%s, updated_at=NOW() WHERE id=%s",
                            (new_price, new_final, svc["id"])
                        )
                        updated += 1
                db.commit()
                total_updated += updated
        except Exception as e:
            errors.append(f"Provider #{p['id']}: {str(e)}")
    return jsonify({"ok": True, "updated": total_updated, "errors": errors})


@app.route("/admin/sync-catalog", methods=["POST"])
@require_admin
def admin_sync_catalog():
    """يجلب كل خدمات دارك فولو ويضيف الجديدة بقاعدة البيانات"""
    import threading
    threading.Thread(target=_sync_provider_catalog, daemon=True).start()
    # انتظر ثانية وارجع نتيجة أولية
    import time; time.sleep(2)
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) as c FROM services").fetchone()["c"]
        linked = db.execute("SELECT COUNT(*) as c FROM services WHERE provider_service_id IS NOT NULL AND provider_service_id != ''").fetchone()["c"]
    return jsonify({"ok": True, "message": "بدأت المزامنة في الخلفية", "updated": linked, "added": total})


@app.route("/admin/services-status", methods=["GET"])
@require_admin
def admin_services_status():
    """إحصائيات الخدمات"""
    with get_db() as db:
        total   = db.execute("SELECT COUNT(*) as c FROM services").fetchone()["c"]
        active  = db.execute("SELECT COUNT(*) as c FROM services WHERE is_active=1").fetchone()["c"]
        linked  = db.execute("SELECT COUNT(*) as c FROM services WHERE provider_service_id IS NOT NULL AND provider_service_id != ''").fetchone()["c"]
        unlinked = total - linked
        last_sync_row = db.execute("SELECT MAX(updated_at) as t FROM services WHERE provider_service_id IS NOT NULL").fetchone()
        last_sync = last_sync_row["t"] if last_sync_row else None
    return jsonify({
        "ok": True,
        "total": total,
        "active": active,
        "linked": linked,
        "unlinked": unlinked,
        "last_sync": last_sync
    })

# ─────────────────────────────────────────────
# ADMIN — Payments
# ─────────────────────────────────────────────
@app.route("/admin/payments", methods=["GET"])
@require_admin
def admin_payments():
    status = request.args.get("status", "")
    with get_db() as db:
        if status:
            rows = db.execute("""
                SELECT p.*, u.email as user_email, u.username FROM payments p
                LEFT JOIN users u ON u.id=p.user_id
                WHERE p.status=%s ORDER BY p.created_at DESC LIMIT 100
            """, (status,)).fetchall()
        else:
            rows = db.execute("""
                SELECT p.*, u.email as user_email, u.username FROM payments p
                LEFT JOIN users u ON u.id=p.user_id
                ORDER BY p.created_at DESC LIMIT 100
            """).fetchall()
    return jsonify({"ok": True, "payments": [dict(r) for r in rows]})

@app.route("/admin/payments/<int:pid>/approve", methods=["POST"])
@require_admin
def admin_approve_payment(pid):
    with get_db() as db:
        pay = db.execute("SELECT * FROM payments WHERE id=%s", (pid,)).fetchone()
        if not pay:
            return jsonify({"error": "غير موجود"}), 404
        if pay["status"] != "pending":
            return jsonify({"error": "تمت المعالجة مسبقاً"}), 400
        db.execute("""UPDATE payments SET status='approved', processed_at=NOW() WHERE id=%s""", (pid,))
        db.execute("UPDATE users SET balance=balance+%s, total_charged=total_charged+%s WHERE id=%s",
                   (pay["amount"], pay["amount"], pay["user_id"]))
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/payments/<int:pid>/reject", methods=["POST"])
@require_admin
def admin_reject_payment(pid):
    data = request.get_json(silent=True) or {}
    note = data.get("note", "")
    with get_db() as db:
        db.execute("""UPDATE payments SET status='rejected', admin_note=%s, processed_at=NOW() WHERE id=%s""",
                   (note, pid))
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/payment-gateways", methods=["GET"])
@require_admin
def admin_get_gateways():
    with get_db() as db:
        rows = db.execute("SELECT * FROM payment_gateways ORDER BY id").fetchall()
    return jsonify({"gateways": [dict(r) for r in rows]})

@app.route("/admin/payment-gateways", methods=["POST"])
@require_admin
def admin_add_gateway():
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        cur = db.execute("""INSERT INTO payment_gateways (name,type,details_ar,details_en,is_active,is_auto,config_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (
            data.get("name",""), data.get("type","manual"),
            data.get("details_ar",""), data.get("details_en",""),
            data.get("is_active",1), data.get("is_auto",0),
            json.dumps(data.get("config",{}))
        ))
        db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})

@app.route("/admin/payment-gateways/<int:gid>", methods=["PUT"])
@require_admin
def admin_update_gateway(gid):
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        db.execute("""UPDATE payment_gateways SET name=COALESCE(%s,name),
            details_ar=COALESCE(%s,details_ar), details_en=COALESCE(%s,details_en),
            is_active=COALESCE(%s,is_active) WHERE id=%s""",
            (data.get("name"), data.get("details_ar"), data.get("details_en"), data.get("is_active"), gid))
        db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# ADMIN — Orders
# ─────────────────────────────────────────────
@app.route("/admin/orders", methods=["GET"])
@require_admin
def admin_orders():
    status = request.args.get("status", "")
    page = int(request.args.get("page", 1))
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = (page - 1) * limit
    with get_db() as db:
        if status:
            rows = db.execute("""
                SELECT o.*, u.email as user_email, s.name_ar as service_name FROM orders o
                LEFT JOIN users u ON u.id=o.user_id
                LEFT JOIN services s ON s.id=o.service_id
                WHERE o.status=%s ORDER BY o.created_at DESC LIMIT %s OFFSET %s
            """, (status, limit, offset)).fetchall()
        else:
            rows = db.execute("""
                SELECT o.*, u.email as user_email, s.name_ar as service_name FROM orders o
                LEFT JOIN users u ON u.id=o.user_id
                LEFT JOIN services s ON s.id=o.service_id
                ORDER BY o.created_at DESC LIMIT %s OFFSET %s
            """, (limit, offset)).fetchall()
    return jsonify({"ok": True, "orders": [dict(r) for r in rows]})

# ─────────────────────────────────────────────
# ADMIN — Tickets
# ─────────────────────────────────────────────
@app.route("/admin/tickets", methods=["GET"])
@require_admin
def admin_tickets():
    with get_db() as db:
        rows = db.execute("""
            SELECT t.*, u.email as user_email, u.username FROM tickets t
            LEFT JOIN users u ON u.id=t.user_id
            ORDER BY t.updated_at DESC LIMIT 100
        """).fetchall()
    return jsonify({"ok": True, "tickets": [dict(r) for r in rows]})

@app.route("/admin/tickets/<int:tid>/reply", methods=["POST"])
@require_admin
def admin_ticket_reply(tid):
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    close = data.get("close", False)
    if not message:
        return jsonify({"error": "رسالة مطلوبة"}), 400
    new_status = "closed" if close else "answered"
    with get_db() as db:
        db.execute("INSERT INTO ticket_messages (ticket_id,sender_type,message) VALUES (%s,%s,%s)",
                   (tid, "admin", message))
        db.execute("UPDATE tickets SET status=%s, updated_at=NOW() WHERE id=%s",
                   (new_status, tid))
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/tickets/<int:tid>/messages", methods=["GET"])
@require_admin
def admin_ticket_msgs(tid):
    with get_db() as db:
        msgs = db.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id=%s ORDER BY created_at", (tid,)
        ).fetchall()
    return jsonify({"messages": [dict(m) for m in msgs]})

# ─────────────────────────────────────────────
# ADMIN — Translations
# ─────────────────────────────────────────────
@app.route("/admin/translations", methods=["GET"])
@require_admin
def admin_get_translations():
    lang = request.args.get("lang", "ar")
    with get_db() as db:
        rows = db.execute("SELECT * FROM translations WHERE lang=%s ORDER BY key", (lang,)).fetchall()
    return jsonify({"translations": [dict(r) for r in rows]})

@app.route("/admin/translations", methods=["POST"])
@require_admin
def admin_save_translation():
    data = request.get_json(silent=True) or {}
    lang  = data.get("lang", "ar")
    key   = data.get("key", "").strip()
    value = data.get("value", "")
    if not key:
        return jsonify({"error": "key مطلوب"}), 400
    with get_db() as db:
        db.execute(
            "INSERT INTO translations (lang,key,value) VALUES (%s,%s,%s) ON CONFLICT (lang,key) DO UPDATE SET value=EXCLUDED.value",
            (lang, key, value)
        )
        db.commit()
    return jsonify({"ok": True})

@app.route("/admin/translations/bulk", methods=["POST"])
@require_admin
def admin_bulk_translations():
    data = request.get_json(silent=True) or {}
    lang = data.get("lang", "ar")
    items = data.get("items", {})
    with get_db() as db:
        for key, value in items.items():
            db.execute(
                "INSERT INTO translations (lang,key,value) VALUES (%s,%s,%s) ON CONFLICT (lang,key) DO UPDATE SET value=EXCLUDED.value",
                (lang, key, value)
            )
        db.commit()
    return jsonify({"ok": True, "saved": len(items)})

# ─────────────────────────────────────────────
# ADMIN — Settings
# ─────────────────────────────────────────────
@app.route("/admin/settings", methods=["GET"])
@require_admin
def admin_get_settings():
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})

@app.route("/admin/settings", methods=["POST"])
@require_admin
def admin_save_settings():
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        for key, value in data.items():
            db.execute(
                "INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value))
            )
        db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# ADMIN — Security Log
# ─────────────────────────────────────────────
@app.route("/admin/security-log", methods=["GET"])
@require_admin
def admin_security_log():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM security_log ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
    return jsonify({"logs": [dict(r) for r in rows]})

# ─────────────────────────────────────────────
# ADMIN — Cron Log
# ─────────────────────────────────────────────
@app.route("/admin/cron-log", methods=["GET"])
@require_admin
def admin_cron_log():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM cron_log ORDER BY ran_at DESC LIMIT 200"
        ).fetchall()
    return jsonify({"logs": [dict(r) for r in rows]})

@app.route("/admin/cron/run", methods=["POST"])
@require_admin
def admin_run_cron():
    data = request.get_json(silent=True) or {}
    job = data.get("job", "sync_orders")
    if job == "sync_orders":
        threading.Thread(target=_cron_sync_orders, daemon=True).start()
    elif job == "sync_catalog":
        provider_id = data.get("provider_id")
        threading.Thread(target=_sync_provider_catalog, args=(provider_id,), daemon=True).start()
    elif job == "sync_balance":
        threading.Thread(target=_sync_provider_balance, daemon=True).start()
    return jsonify({"ok": True, "job": job})

# ─────────────────────────────────────────────
# ADMIN — Provider Live Balance (Direct)
# ─────────────────────────────────────────────
@app.route("/admin/providers/balances", methods=["GET"])
@require_admin
def admin_provider_balances():
    """Return all provider balances (from DB — updated by cron)"""
    with get_db() as db:
        rows = db.execute("SELECT id, name, balance, api_url FROM providers WHERE is_active=1").fetchall()
    return jsonify({"providers": [dict(r) for r in rows]})

@app.route("/admin/providers/<int:pid>/refresh-balance", methods=["POST"])
@require_admin
def admin_refresh_provider_balance(pid):
    """Force-refresh balance from provider API right now"""
    with get_db() as db:
        p = db.execute("SELECT * FROM providers WHERE id=%s", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "غير موجود"}), 404
    try:
        resp = requests.post(p["api_url"], data={"key": p["api_key"], "action": "balance"}, timeout=10)
        data = resp.json()
        bal = float(data.get("balance", data.get("Balance", data.get("funds", 0))))
        with get_db() as db:
            db.execute("UPDATE providers SET balance=%s WHERE id=%s", (bal, pid))
            db.commit()
        return jsonify({"ok": True, "balance": bal, "name": p["name"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─────────────────────────────────────────────
# ADMIN — User find by username
# ─────────────────────────────────────────────
@app.route("/admin/users/find", methods=["GET"])
@require_admin
def admin_find_user():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q مطلوب"}), 400
    with get_db() as db:
        pat = f"%{q}%"
        users = db.execute("""
            SELECT id, email, username, telegram_id, balance,
                   total_charged, total_spent, orders_count,
                   is_banned, joined_at, last_seen
            FROM users
            WHERE username LIKE %s OR email LIKE %s
               OR telegram_id LIKE %s OR CAST(id AS TEXT) = %s
            ORDER BY joined_at DESC LIMIT 20
        """, (pat, pat, pat, q)).fetchall()
    return jsonify({"users": [dict(u) for u in users]})

# ─────────────────────────────────────────────
# ADMIN — Profit Report
# ─────────────────────────────────────────────
@app.route("/admin/reports/profit", methods=["GET"])
@require_admin
def admin_profit():
    with get_db() as db:
        monthly = db.execute("""
            SELECT TO_CHAR(o.created_at::timestamp, 'YYYY-MM') as month,
                   COUNT(*) as total_orders,
                   SUM(o.price) as revenue,
                   SUM(s.provider_price * o.quantity / 1000.0) as cost
            FROM orders o
            LEFT JOIN services s ON s.id=o.service_id
            WHERE o.status != 'cancelled'
            GROUP BY month ORDER BY month DESC LIMIT 12
        """).fetchall()
    result = []
    for r in monthly:
        d = dict(r)
        cost = d.get("cost") or 0
        rev  = d.get("revenue") or 0
        d["profit"] = round(rev - cost, 2)
        result.append(d)
    return jsonify({"monthly": result})

# ── Admin: PIN check (for admin.html gate) ──
@app.route("/admin/check-pin", methods=["POST"])
def admin_check_pin():
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin","")).strip()
    if not pin:
        return jsonify({"ok": False, "error": "رمز مطلوب"}), 400
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='admin_pin'").fetchone()
    stored = row["value"] if row else os.environ.get("ADMIN_PIN","147258")
    if pin == stored:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "رمز خاطئ"}), 401

# ── Admin: GET single user ──
@app.route("/admin/users/<int:uid>", methods=["GET"])
@require_admin
def admin_get_user(uid):
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    if not u:
        return jsonify({"error": "المستخدم غير موجود"}), 404
    return jsonify({"ok": True, "user": dict(u)})

# ── Admin: Unban user ──
@app.route("/admin/users/<int:uid>/unban", methods=["POST"])
@require_admin
def admin_unban_user(uid):
    with get_db() as db:
        db.execute("UPDATE users SET is_banned=0 WHERE id=%s", (uid,))
        db.commit()
    return jsonify({"ok": True})

# ── Admin: GET single order ──
@app.route("/admin/orders/<int:oid>", methods=["GET"])
@require_admin
def admin_get_order(oid):
    with get_db() as db:
        o = db.execute("""
            SELECT o.*, s.name_ar as service_name, u.email as user_email
            FROM orders o
            LEFT JOIN services s ON s.id=o.service_id
            LEFT JOIN users u ON u.id=o.user_id
            WHERE o.id=%s
        """, (oid,)).fetchone()
    if not o:
        return jsonify({"error": "الطلب غير موجود"}), 404
    return jsonify({"ok": True, "order": dict(o)})

# ── Admin: GET single ticket with messages ──
@app.route("/admin/tickets/<int:tid>", methods=["GET"])
@require_admin
def admin_get_ticket(tid):
    with get_db() as db:
        t = db.execute("""
            SELECT tk.*, u.email as user_email
            FROM tickets tk
            LEFT JOIN users u ON u.id=tk.user_id
            WHERE tk.id=%s
        """, (tid,)).fetchone()
        if not t:
            return jsonify({"error": "التذكرة غير موجودة"}), 404
        msgs = db.execute("""
            SELECT * FROM ticket_messages WHERE ticket_id=%s ORDER BY created_at ASC
        """, (tid,)).fetchall()
    result = dict(t)
    result["messages"] = [dict(m) for m in msgs]
    return jsonify({"ok": True, "ticket": result})

# ── Admin: Bulk settings save ──
@app.route("/admin/settings/bulk", methods=["POST"])
@require_admin
def admin_settings_bulk():
    data = request.get_json(silent=True) or {}
    with get_db() as db:
        for key, value in data.items():
            if value is None:
                continue
            db.execute(
                "INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value))
            )
        db.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# ADMIN PANEL — Missing Routes (compat layer)
# ─────────────────────────────────────────────

@app.route("/admin/darkfollow-balance", methods=["GET"])
@require_admin
def admin_darkfollow_balance():
    """رصيد DarkFollow — تُستخدم من لوحة التحكم"""
    api_key = DARKFOLLOW_API_KEY
    api_url = DARKFOLLOW_API_URL
    if not api_key:
        # محاولة جلبه من قاعدة البيانات
        with get_db() as db:
            prov = db.execute(
                "SELECT api_key, api_url FROM providers WHERE api_url LIKE '%darkfollow%' LIMIT 1"
            ).fetchone()
            if prov:
                api_key = prov["api_key"]
                api_url = prov["api_url"] or api_url
    if not api_key:
        return jsonify({"error": "DARKFOLLOW_API_KEY غير مضبوط"}), 400
    try:
        if "darkfollow" in api_url.lower():
            base_url = api_url.rstrip("/").split("?")[0]
            res = requests.get(f"{base_url}?action=balance&key={api_key}", timeout=10)
        else:
            res = requests.post(api_url, data={"key": api_key, "action": "balance"}, timeout=10)
        data = res.json()
        balance = data.get("balance", "0")
        currency = data.get("currency", "USD")
        return jsonify({"balance": float(balance), "currency": currency})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/admin/markup", methods=["GET"])
@require_admin
def admin_get_markup():
    """جلب هامش الربح العام — تُستخدم من لوحة التحكم"""
    markup_type  = get_setting("global_markup_type", "percent")
    markup_value = float(get_setting("global_markup_value", "0"))
    return jsonify({"markup_type": markup_type, "markup_value": markup_value})


@app.route("/admin/markup", methods=["POST"])
@require_admin
def admin_set_markup():
    """حفظ هامش الربح العام — تُستخدم من لوحة التحكم"""
    data = request.get_json(silent=True) or {}
    markup_type  = data.get("markup_type", "percent")
    markup_value = float(data.get("markup_value", 0))
    if markup_type not in ("percent", "fixed"):
        return jsonify({"error": "markup_type يجب أن يكون percent أو fixed"}), 400
    set_setting("global_markup_type",  markup_type)
    set_setting("global_markup_value", str(markup_value))
    # تطبيق الهامش على جميع الخدمات
    with get_db() as db:
        svcs = db.execute("SELECT id, provider_price FROM services").fetchall()
        for svc in svcs:
            new_price = calc_price(svc["provider_price"], markup_type, markup_value)
            db.execute(
                "UPDATE services SET markup_type=%s, markup_value=%s, final_price=%s WHERE id=%s",
                (markup_type, markup_value, new_price, svc["id"])
            )
        db.commit()
    log.info(f"[admin] global markup updated: {markup_type} {markup_value}")
    return jsonify({"ok": True, "markup_type": markup_type, "markup_value": markup_value})


@app.route("/user/balance", methods=["POST"])
@require_admin
def admin_user_balance_compat():
    """شحن/خصم رصيد مستخدم بالـ uid — compat للوحة التحكم
    Body: { uid: str, amount: float }
    amount موجب = شحن، سالب = خصم
    """
    data   = request.get_json(silent=True) or {}
    uid    = str(data.get("uid", "")).strip()
    amount = float(data.get("amount", 0))
    if not uid:
        return jsonify({"error": "uid مطلوب"}), 400
    with get_db() as db:
        user = db.execute("SELECT id, balance FROM users WHERE uid=%s", (uid,)).fetchone()
        if not user:
            return jsonify({"error": "المستخدم غير موجود"}), 404
        current = float(user["balance"] or 0)
        if amount >= 0:
            new_bal = current + amount
            db.execute(
                "UPDATE users SET balance=%s, total_charged=total_charged+%s WHERE id=%s",
                (new_bal, amount, user["id"])
            )
        else:
            new_bal = max(0, current + amount)
            db.execute("UPDATE users SET balance=%s WHERE id=%s", (new_bal, user["id"]))
        db.commit()
    log.info(f"[admin] user balance updated uid={uid} amount={amount} new={new_bal}")
    return jsonify({"ok": True, "balance": new_bal})


@app.route("/user/find", methods=["GET"])
@require_admin
def admin_user_find_compat():
    """البحث عن مستخدم بالـ uid — compat للوحة التحكم"""
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid مطلوب"}), 400
    with get_db() as db:
        user = db.execute(
            """SELECT uid, email, username, telegram_id, balance,
                      total_charged, total_spent, orders_count,
                      is_banned, joined_at
               FROM users WHERE uid=%s OR email=%s OR telegram_id=%s OR username=%s
               LIMIT 1""",
            (uid, uid, uid, uid)
        ).fetchone()
    if not user:
        return jsonify({}), 404
    u = dict(user)
    # تأكد إن uid موجود — fallback للـ username لو uid فارغ
    if not u.get('uid') and u.get('username'):
        u['uid'] = u['username']
    return jsonify(u)


# ─────────────────────────────────────────────
# Cron Job: Sync Orders
# ─────────────────────────────────────────────
def _cron_sync_orders():
    start = time.time()
    status = "ok"
    details = ""
    try:
        with get_db() as db:
            pending = db.execute("""
                SELECT o.*, p.api_url, p.api_key
                FROM orders o
                JOIN services s ON s.id=o.service_id
                JOIN providers p ON p.id=s.provider_id
                WHERE o.status IN ('pending','active','partial')
                AND o.provider_order_id IS NOT NULL
                AND o.provider_order_id != ''
                LIMIT 100
            """).fetchall()

        if not pending:
            return

        # Group by provider
        by_provider = {}
        for o in pending:
            key = (o["api_url"], o["api_key"])
            by_provider.setdefault(key, []).append(o)

        updated = 0
        for (api_url, api_key), orders in by_provider.items():
            ids = ",".join(o["provider_order_id"] for o in orders)
            try:
                if "darkfollow" in api_url.lower():
                    base_url = api_url.rstrip("/").split("?")[0]
                    url = f"{base_url}?action=status&key={api_key}&order={ids}"
                    log.info(f"[cron] Dark Follow status check, using GET request")
                    resp = requests.get(url, timeout=20)
                else:
                    resp = requests.post(api_url, data={
                        "key": api_key, "action": "status", "order": ids
                    }, timeout=20)
                results = resp.json()

                with get_db() as db:
                    STATUS_MAP = {
                        "pending": "pending", "in progress": "active",
                        "inprogress": "active", "processing": "active",
                        "completed": "completed", "complete": "completed",
                        "canceled": "cancelled", "cancelled": "cancelled",
                        "partial": "partial", "active": "active"
                    }
                    for o in orders:
                        info = results.get(str(o["provider_order_id"]), {})
                        if not info:
                            continue
                        raw_status = str(info.get("status","pending")).lower().strip()
                        new_status = STATUS_MAP.get(raw_status, raw_status)
                        remains = int(info.get("remains", 0))
                        start_count = int(info.get("start_count", o.get("start_count", 0)))
                        db.execute("""UPDATE orders SET status=%s, remains=%s, start_count=%s,
                            updated_at=NOW() WHERE id=%s""",
                            (new_status, remains, start_count, o["id"]))
                        updated += 1
                    db.commit()
            except Exception as e:
                log.error(f"[cron] provider sync error: {e}")

        details = f"updated={updated}"
        ms = int((time.time() - start) * 1000)
        with get_db() as db:
            db.execute("INSERT INTO cron_log (job_name,status,details,duration_ms) VALUES (%s,%s,%s,%s)",
                       ("sync_orders", status, details, ms))
            db.commit()

    except Exception as e:
        log.error(f"[cron] fatal: {e}")
        with get_db() as db:
            db.execute("INSERT INTO cron_log (job_name,status,details,duration_ms) VALUES (%s,%s,%s,%s)",
                       ("sync_orders", "error", str(e), int((time.time()-start)*1000)))
            db.commit()

def _cron_loop():
    _catalog_counter = 0
    while True:
        try:
            time.sleep(60)
            try: _cron_sync_orders()
            except Exception as e: log.error(f"[cron] sync_orders: {e}")
            try: _sync_provider_balance()
            except Exception as e: log.error(f"[cron] sync_balance: {e}")
            _catalog_counter += 1
            if _catalog_counter >= 5:
                _catalog_counter = 0
                try: _sync_provider_catalog()
                except Exception as e: log.error(f"[cron] sync_catalog: {e}")
        except Exception as e:
            log.error(f"[cron] fatal: {e}")
            time.sleep(10)

# ─────────────────────────────────────────────
# Auto-Sync Dark Follow: Categories + Services + Images
# ─────────────────────────────────────────────
def _sync_provider_catalog(provider_id=None):
    """
    Fetches all services from provider API and auto-creates
    categories + services with thumbnail images.
    Runs every 5 minutes via cron loop.
    """
    start = time.time()
    try:
        with get_db() as db:
            if provider_id:
                providers = db.execute("SELECT * FROM providers WHERE id=%s AND is_active=1", (provider_id,)).fetchall()
            else:
                providers = db.execute("SELECT * FROM providers WHERE is_active=1").fetchall()

        if not providers:
            return

        for prov in providers:
            try:
                api_url = prov["api_url"]
                api_key = prov["api_key"]
                if "darkfollow" in api_url.lower():
                    base_url = api_url.rstrip("/").split("?")[0]
                    url = f"{base_url}?action=services&key={api_key}"
                    log.info(f"[catalog] Dark Follow detected, using GET request")
                    log.info(f"[catalog] Dark Follow URL: {url[:60]}...")
                    resp = requests.get(url, timeout=30)
                else:
                    resp = requests.post(api_url, data={"key": api_key, "action": "services"}, timeout=30)
                remote_svcs = resp.json()
                if not isinstance(remote_svcs, list):
                    log.error(f"[catalog] provider #{prov['id']} returned non-list: type={type(remote_svcs).__name__}, content={str(remote_svcs)[:200]}")
                    continue

                # ── Build category map from remote services ──
                cat_map = {}  # category_name → local category id
                with get_db() as db:
                    existing_cats = db.execute("SELECT id, name_en FROM categories").fetchall()
                    for c in existing_cats:
                        cat_map[c["name_en"].strip().lower()] = c["id"]

                # ── Process each remote service ──
                remote_ids = set()
                with get_db() as db:
                    markup_type  = get_setting("global_markup_type", "percent")
                    markup_value = float(get_setting("global_markup_value", "0"))

                    # ── Dark Follow category mapping (name → icon, sort_order, ar_name)
                    DARKFOLLOW_CAT_MAP = {
                        # خدمات دارك / White Follow services
                        "خدمات دارك | دارك": ("🌟", 10, "خدمات وايت فولو"),
                        "عروض العيد | دارك": ("🔥", 11, "عروض العيد"),
                        "خدمات انستا | دارك": ("📸", 12, "خدمات انستا | دارك"),
                        "خدمات فيسبوك | دارك": ("📘", 13, "خدمات فيسبوك | دارك"),
                        "خدمات تليجرام | دارك": ("✈️", 14, "خدمات تليجرام | دارك"),
                        "خدمات التجار | دارك": ("🛍️", 15, "خدمات التجار | دارك"),
                        "خدمات تلجرام مميز | دارك": ("⭐", 16, "خدمات تلجرام مميز | دارك"),
                        "نجوم وهدايه تلجرام | دارك": ("🎁", 17, "نجوم وهدايه تلجرام | دارك"),
                        # قسم البطائق
                        "بطاقات فالورانت | valorant": ("🎮", 20, "بطاقات فالورانت"),
                        "بطاقات ستيم | steam": ("💨", 21, "بطاقات ستيم"),
                        "بطاقات شحن كرانيش رول | crunchyroll": ("🍥", 22, "بطاقات كرانيش رول"),
                        "بطاقات ريوت جيمز | riot games": ("⚔️", 23, "بطاقات ريوت جيمز"),
                        "بطاقات روبلوكس | roblox": ("🟥", 24, "بطاقات روبلوكس"),
                        "بطاقات امزون | amzoum": ("📦", 25, "بطاقات امزون"),
                        "بطاقات ريزر كولد | zgold": ("🔶", 26, "بطاقات ريزر كولد"),
                        "بطاقات ليج اوف ليجند | league of legends": ("⚡", 27, "بطاقات ليج اوف ليجند"),
                        "بطاقات ايتونز | itunes": ("🍎", 28, "بطاقات ايتونز"),
                        "بطاقات نينتيندو | nintendo": ("🎮", 29, "بطاقات نينتيندو"),
                        "بطاقات بليزارد | blizzard": ("🌀", 30, "بطاقات بليزارد"),
                        "بطاقات فورتنايت | fortnite": ("🏗️", 31, "بطاقات فورتنايت"),
                        "بطاقات شحن | playstation": ("🎮", 32, "بطاقات بلايستيشن"),
                        "بطاقات نتفلكس | netflix": ("🎬", 33, "بطاقات نتفلكس"),
                        # قسم شحن الألعاب
                        "أوكسايد موبايل | oxide mobile": ("🔥", 40, "أوكسايد موبايل"),
                        "جينشين إمباكت | genshin impact": ("🌸", 41, "جينشين إمباكت"),
                        "ارينا بريك أوت | arena breakout": ("🎯", 42, "ارينا بريك اوت"),
                        "ببجي موبايل | pubg mobile": ("🪖", 43, "ببجي موبايل"),
                        "دلتا فورس | delta force": ("💥", 44, "دلتا فورس"),
                        "كولف ديوتي موبايل | call of duty": ("🔫", 45, "كولف ديوتي موبايل"),
                        "شحن يلا لودو | ludo": ("🎲", 46, "شحن يلا لودو"),
                        # قسم الاشتراكات
                        "نايترو ديسكورد | nitro": ("🎮", 50, "نايترو ديسكورد"),
                        "العاب ستيم | steam": ("💨", 51, "العاب ستيم"),
                        "نتفلكس | netflix": ("🎬", 52, "نتفلكس"),
                        "كيم بأس | xbox": ("🎮", 53, "كيم بأس Xbox"),
                        "تلجرام مميز | premium": ("⭐", 54, "تلجرام مميز"),
                        "اشتراكات | playstation plus": ("🎮", 55, "PlayStation Plus"),
                        # شحن رصيد الهاتف
                        "العراق": ("🇮🇶", 60, "شحن العراق"),
                        "السعودية": ("🇸🇦", 61, "شحن السعودية"),
                        "الاردن": ("🇯🇴", 62, "شحن الاردن"),
                        "لبنان": ("🇱🇧", 63, "شحن لبنان"),
                        "مصر": ("🇪🇬", 64, "شحن مصر"),
                        "البحرين": ("🇧🇭", 65, "شحن البحرين"),
                        # تليجرام مميز
                        "خدمات مميز عربي": ("⭐", 70, "خدمات مميز عربي"),
                        "خدمات مميزه بدون نزول": ("✅", 71, "خدمات مميزه بدون نزول"),
                        "ستارت بوت رخيصه": ("🚀", 72, "ستارت بوت رخيصه"),
                    }

                    def _get_cat_info(cat_name):
                        """Find best matching category info from map"""
                        name_lower = cat_name.lower().strip()
                        # Exact match first
                        for key, val in DARKFOLLOW_CAT_MAP.items():
                            if key.lower() == name_lower:
                                return val[0], val[1], val[2]
                        # Partial match
                        for key, val in DARKFOLLOW_CAT_MAP.items():
                            key_parts = key.lower().split("|")
                            for part in key_parts:
                                if part.strip() and part.strip() in name_lower:
                                    return val[0], val[1], val[2]
                        # Keyword fallback
                        icon = "📦"
                        kw_map = {
                            "instagram":"📸","انستا":"📸","tiktok":"🎵","تيك توك":"🎵",
                            "youtube":"▶️","يوتيوب":"▶️","twitter":"🐦","تويتر":"🐦",
                            "facebook":"📘","فيسبوك":"📘","snapchat":"👻","سناب":"👻",
                            "telegram":"✈️","تليجرام":"✈️","تلجرام":"✈️",
                            "netflix":"🎬","نتفلكس":"🎬","spotify":"🎧","سبوتيفاي":"🎧",
                            "playstation":"🎮","ps":"🎮","xbox":"🎮","steam":"💨","ستيم":"💨",
                            "بطاقات":"🎫","cards":"🎫","شحن":"⚡","recharge":"⚡",
                            "اشتراك":"📋","subscription":"📋","رصيد":"📱","balance":"📱",
                            "مميز":"⭐","premium":"⭐","games":"🎮","العاب":"🎮",
                            "لودو":"🎲","pubg":"🪖","ببجي":"🪖","valorant":"🎮","فالورانت":"🎮",
                            "roblox":"🟥","روبلوكس":"🟥","fortnite":"🏗️","فورتنايت":"🏗️",
                        }
                        for kw, em in kw_map.items():
                            if kw in name_lower:
                                icon = em
                                break
                        return icon, len(cat_map) * 10, cat_name

                    for svc in remote_svcs:
                        cat_name = str(svc.get("category", "General")).strip()
                        cat_key  = cat_name.lower()

                        # Create category if missing
                        if cat_key not in cat_map:
                            icon, sort_order, ar_name = _get_cat_info(cat_name)
                            cur = db.execute(
                                "INSERT INTO categories (name_ar, name_en, icon, sort_order, is_active) VALUES (%s,%s,%s,%s,1)",
                                (ar_name, cat_name, icon, sort_order)
                            )
                            cat_map[cat_key] = cur.lastrowid
                            log.info(f"[catalog] New category: {cat_name} → {ar_name}")

                        cat_id = cat_map[cat_key]
                        provider_service_id = str(svc.get("service", ""))
                        remote_ids.add(provider_service_id)

                        provider_price = float(svc.get("rate", svc.get("price", 0)))
                        final_price    = calc_price(provider_price, markup_type, markup_value)

                        # Image URL from provider (thumbnail)
                        img_url = str(svc.get("image", svc.get("img", svc.get("icon", ""))))

                        # Save first service image as category image
                        if img_url:
                            cat_img = db.execute("SELECT image_url FROM categories WHERE id=%s", (cat_id,)).fetchone()
                            if cat_img and not cat_img["image_url"]:
                                db.execute("UPDATE categories SET image_url=%s WHERE id=%s", (img_url, cat_id))

                        existing = db.execute(
                            "SELECT id, provider_price FROM services WHERE provider_id=%s AND provider_service_id=%s",
                            (prov["id"], provider_service_id)
                        ).fetchone()

                        if existing:
                            # Always update price + final_price (with markup) + image + category
                            db.execute("""
                                UPDATE services SET
                                    provider_price=%s, final_price=%s,
                                    markup_type=%s, markup_value=%s,
                                    category_id=%s, image_url=%s,
                                    updated_at=NOW()
                                WHERE id=%s
                            """, (provider_price, final_price,
                                  markup_type, markup_value,
                                  cat_id, img_url, existing["id"]))
                        else:
                            svc_name = str(svc.get("name", "")).strip()
                            db.execute("""
                                INSERT INTO services
                                (provider_id, provider_service_id, category_id,
                                 name_ar, name_en, description_ar, description_en,
                                 min_qty, max_qty, provider_price, markup_type, markup_value,
                                 final_price, type, image_url, is_active)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                            """, (
                                prov["id"], provider_service_id, cat_id,
                                svc_name, svc_name, "", "",
                                int(svc.get("min", 10)), int(svc.get("max", 10000)),
                                provider_price, markup_type, markup_value,
                                final_price, str(svc.get("type", "Default")), img_url
                            ))
                            log.info(f"[catalog] New service: {provider_service_id} {svc_name}")

                    # ── Deactivate services no longer in provider ──
                    all_local = db.execute(
                        "SELECT id, provider_service_id FROM services WHERE provider_id=%s", (prov["id"],)
                    ).fetchall()
                    for ls in all_local:
                        if ls["provider_service_id"] not in remote_ids:
                            db.execute("UPDATE services SET is_active=0 WHERE id=%s", (ls["id"],))
                            log.info(f"[catalog] Deactivated service: {ls['provider_service_id']}")

                    db.commit()
                log.info(f"[catalog] Synced {len(remote_svcs)} services from provider #{prov['id']}")

            except Exception as e:
                log.error(f"[catalog] provider #{prov['id']} error: {e}")

        ms = int((time.time() - start) * 1000)
        with get_db() as db:
            db.execute("INSERT INTO cron_log (job_name,status,details,duration_ms) VALUES (%s,%s,%s,%s)",
                       ("sync_catalog", "ok", f"providers={len(providers)}", ms))
            db.commit()

    except Exception as e:
        log.error(f"[catalog] fatal: {e}")


def _sync_provider_balance():
    """Fetch and update provider balances from their APIs"""
    try:
        with get_db() as db:
            providers = db.execute("SELECT * FROM providers WHERE is_active=1").fetchall()
        for prov in providers:
            try:
                api_url = prov["api_url"]
                api_key = prov["api_key"]
                if "darkfollow" in api_url.lower():
                    base_url = api_url.rstrip("/").split("?")[0]
                    url = f"{base_url}?action=balance&key={api_key}"
                    log.info(f"[balance] Dark Follow detected, using GET request")
                    resp = requests.get(url, timeout=10)
                else:
                    resp = requests.post(api_url, data={"key": api_key, "action": "balance"}, timeout=10)
                data = resp.json()
                bal = float(data.get("balance", data.get("Balance", data.get("funds", 0))))
                with get_db() as db:
                    db.execute("UPDATE providers SET balance=%s WHERE id=%s", (bal, prov["id"]))
                    db.commit()
                log.info(f"[balance] Provider #{prov['id']} balance: {bal}")
            except Exception as e:
                log.error(f"[balance] provider #{prov['id']}: {e}")
    except Exception as e:
        log.error(f"[balance] fatal: {e}")

# ─────────────────────────────────────────────
# External cron endpoint (for cron-job.org etc.)
# ─────────────────────────────────────────────
@app.route("/cron/sync", methods=["GET","POST"])
def cron_sync():
    key = request.args.get("key","") or (request.get_json(silent=True) or {}).get("key","")
    if ADMIN_SECRET_KEY and key != ADMIN_SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401
    threading.Thread(target=_cron_sync_orders, daemon=True).start()
    return jsonify({"ok": True, "message": "cron triggered"})

# ─────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────
def _startup():
    # ── DB init (non-fatal: app must start even if DB is temporarily unreachable) ──
    try:
        init_db()
        log.info("[startup] Database initialized OK")
    except Exception as e:
        log.error(f"[startup] DB init failed — will retry lazily: {e}")

    # ── Telegram Bot — delete old webhook and start polling thread ──
    if TELEGRAM_BOT_TOKEN:
        try:
            # Delete any previously registered webhook so polling works correctly
            threading.Thread(target=lambda: tg("deleteWebhook", {"drop_pending_updates": False}), daemon=True).start()
            # Register bot commands
            threading.Thread(target=lambda: tg("setMyCommands", {"commands": [
                {"command": "start",   "description": "فتح التطبيق"},
                {"command": "balance", "description": "عرض رصيدي"},
                {"command": "orders",  "description": "آخر 5 طلبات"},
                {"command": "support", "description": "الدعم الفني"}
            ]}), daemon=True).start()
        except Exception as e:
            log.error(f"[startup] TG setup failed: {e}")
        # Start the polling loop in a persistent background thread
        threading.Thread(target=_bot_polling_loop, daemon=True).start()

    # ── Background cron ──
    threading.Thread(target=_cron_loop, daemon=True).start()

    # ── Auto-register Dark Follow provider ──
    if DARKFOLLOW_API_KEY:
        def _register_provider():
            try:
                clean_url = DARKFOLLOW_API_URL.rstrip("/").split("?")[0]
                with get_db() as db:
                    existing = db.execute(
                        "SELECT id FROM providers WHERE api_url=%s", (clean_url,)
                    ).fetchone()
                    if not existing:
                        existing = db.execute(
                            "SELECT id FROM providers WHERE api_url LIKE %s", (f"{clean_url}%",)
                        ).fetchone()
                    if not existing:
                        db.execute(
                            "INSERT INTO providers (name, api_url, api_key, is_active) VALUES (%s,%s,%s,1)",
                            ("Dark Follow", clean_url, DARKFOLLOW_API_KEY)
                        )
                        db.commit()
                        log.info("✅ Dark Follow provider auto-registered")
                    else:
                        db.execute(
                            "UPDATE providers SET api_key=%s, api_url=%s WHERE id=%s",
                            (DARKFOLLOW_API_KEY, clean_url, existing["id"])
                        )
                        db.commit()
                        log.info(f"✅ Dark Follow provider updated (id={existing['id']})")
            except Exception as e:
                log.error(f"[startup] provider registration failed: {e}")
        threading.Thread(target=_register_provider, daemon=True).start()

    # ── Initial sync (background, non-blocking) ──
    threading.Thread(target=_sync_provider_balance, daemon=True).start()
    threading.Thread(target=_sync_provider_catalog, daemon=True).start()

    log.info("✅ SMM Panel started")

try:
    _startup()
except Exception as e:
    log.error(f"[startup] fatal error (app still running): {e}")

# ─────────────────────────────────────────────
# Gunicorn config hint (add to gunicorn.conf.py):
# workers = 1
# preload_app = True
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
