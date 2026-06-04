from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import os
import threading
import json
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ── Firebase Admin SDK ─────────────────────────────────────
_fb_creds_json = os.environ.get("FIREBASE_CREDENTIALS", "")
if _fb_creds_json:
    _fb_creds_dict = json.loads(_fb_creds_json)
    cred = credentials.Certificate(_fb_creds_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
else:
    db = None

# ── إعدادات من Railway Environment Variables ──────────────
DARKFOLLOW_API_URL = "https://darkfollow.shop/api/v2"
DARKFOLLOW_API_KEY = os.environ.get("DARKFOLLOW_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
# رابط موقعك على Railway (يُحدَّث بعد النشر)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://YOUR-APP.up.railway.app/app")
SUPPORT_USERNAME = "o75ei"

# ────────────────────────────────────────────────────────────
# دوال مساعدة
# ────────────────────────────────────────────────────────────
def tg(method, payload):
    """استدعاء Telegram Bot API"""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
            json=payload, timeout=10
        )
        return r.json()
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return {}

def send_notify(text):
    """إشعار لصاحب البوت"""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID,
                           "text": text, "parse_mode": "HTML"})

def set_bot_commands():
    """تفعيل أوامر البوت"""
    tg("setMyCommands", {"commands": [
        {"command": "start", "description": "فتح التطبيق"},
        {"command": "support", "description": "الدعم الفني"}
    ]})

# ────────────────────────────────────────────────────────────
# Webhook — يستقبل رسائل تلغرام
# ────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    if not update:
        return "ok"

    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "")
    user    = msg.get("from", {})
    name    = user.get("first_name", "")

    if not chat_id:
        return "ok"

    # ── /start ─────────────────────────────────────────
    if text.startswith("/start"):
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"أهلاً {name}! 👋\n\n"
                "مرحباً بك في <b>White Follow</b> 🌟\n"
                "اختر من القائمة:"
            ),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "🚀 فتح التطبيق",
                            "web_app": {"url": WEBAPP_URL}
                        }
                    ],
                    [
                        {
                            "text": "💬 الدعم الفني",
                            "url": f"https://t.me/{SUPPORT_USERNAME}"
                        }
                    ]
                ]
            }
        })

    # ── /support ───────────────────────────────────────
    elif text.startswith("/support"):
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": "للتواصل مع الدعم الفني اضغط الزر أدناه 👇",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "💬 تواصل مع الدعم",
                     "url": f"https://t.me/{SUPPORT_USERNAME}"}
                ]]
            }
        })

    return "ok"

# ────────────────────────────────────────────────────────────
# يخدم ملف الموقع داخل تلغرام WebApp
# ────────────────────────────────────────────────────────────
@app.route("/app")
def serve_app():
    """يفتح الموقع كـ WebApp داخل تلغرام"""
    return send_file("index.html")

# ────────────────────────────────────────────────────────────
# API الطلبات
# ────────────────────────────────────────────────────────────
@app.route("/order", methods=["POST"])
def place_order():
    data     = request.get_json()
    service  = data.get("service")
    link     = data.get("link")
    quantity = data.get("quantity", 1)

    if not service or not link:
        return jsonify({"error": "service و link مطلوبان"}), 400

    try:
        resp   = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY, "action": "add",
            "service": service, "link": link, "quantity": quantity
        }, timeout=15)
        result = resp.json()

        if result.get("order"):
            send_notify(
                f"✅ <b>طلب جديد!</b>\n"
                f"📦 الخدمة: <code>{service}</code>\n"
                f"🔗 الرابط: {link}\n"
                f"🔢 الكمية: {quantity}\n"
                f"🆔 رقم الطلب: <b>{result['order']}</b>"
            )
            return jsonify({"order": result["order"]})
        else:
            return jsonify({"error": result.get("error", "خطأ")}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/balance")
def get_balance():
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY, "action": "balance"
        }, timeout=10)
        return jsonify(resp.json())
    except:
        return jsonify({"error": "فشل"}), 500

@app.route("/")
def home():
    return jsonify({"status": "✅ السيرفر شغال"})

# ────────────────────────────────────────────────────────────
# User Endpoints — إدارة المستخدمين عبر Firestore
# ────────────────────────────────────────────────────────────

@app.route("/user/sync", methods=["POST"])
def user_sync():
    """يُستدعى عند تسجيل دخول أي مستخدم — يحفظ بياناته في Firestore"""
    if not db:
        return jsonify({"error": "Firebase غير مفعّل"}), 503
    data = request.get_json()
    uid  = data.get("uid")
    if not uid:
        return jsonify({"error": "uid مطلوب"}), 400
    ref = db.collection("users").document(uid)
    doc = ref.get()
    if doc.exists:
        # حدّث فقط الحقول القابلة للتغيير (لا تمسح الرصيد)
        ref.update({
            "email":        data.get("email", ""),
            "provider":     data.get("provider", ""),
            "totalCharged": data.get("totalCharged", 0),
            "totalSpent":   data.get("totalSpent", 0),
            "orders":       data.get("orders", 0),
        })
    else:
        # مستخدم جديد — أنشئ الوثيقة برصيد 0
        ref.set({
            "uid":          uid,
            "email":        data.get("email", ""),
            "provider":     data.get("provider", ""),
            "balance":      0,
            "totalCharged": 0,
            "totalSpent":   0,
            "orders":       0,
            "joinedAt":     data.get("joinedAt", ""),
        })
    return jsonify({"ok": True})

@app.route("/user/find", methods=["GET"])
def user_find():
    """بحث عن مستخدم بالـ uid"""
    if not db:
        return jsonify({"error": "Firebase غير مفعّل"}), 503
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid مطلوب"}), 400
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return jsonify({"error": "المستخدم غير موجود"}), 404
    return jsonify(doc.to_dict())

@app.route("/user/balance", methods=["POST"])
def user_balance():
    """شحن رصيد مستخدم — للأدمن فقط"""
    if not db:
        return jsonify({"error": "Firebase غير مفعّل"}), 503
    data    = request.get_json()
    uid     = data.get("uid")
    amount  = data.get("amount")
    if not uid or amount is None:
        return jsonify({"error": "uid و amount مطلوبان"}), 400
    ref = db.collection("users").document(uid)
    doc = ref.get()
    if not doc.exists:
        return jsonify({"error": "المستخدم غير موجود"}), 404
    current = doc.to_dict().get("balance", 0)
    new_bal = current + float(amount)
    ref.update({
        "balance":      new_bal,
        "totalCharged": firestore.Increment(float(amount))
    })
    return jsonify({"ok": True, "newBalance": new_bal})

@app.route("/user/check", methods=["GET"])
def user_check():
    """تحقق إذا البريد مسجل مسبقاً"""
    if not db:
        return jsonify({"exists": False})
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"exists": False})
    docs = db.collection("users").where("email", "==", email).limit(1).get()
    return jsonify({"exists": len(docs) > 0})

@app.route("/user/list", methods=["GET"])
def user_list():
    """قائمة كل المستخدمين — للأدمن"""
    if not db:
        return jsonify([])
    docs = db.collection("users").order_by("joinedAt", direction=firestore.Query.DESCENDING).limit(200).get()
    return jsonify([d.to_dict() for d in docs])



# ────────────────────────────────────────────────────────────
@app.route("/services/list", methods=["GET"])
def services_list():
    """جلب أسعار الخدمات من دارك فولو"""
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "services"
        })
        data = resp.json()
        services = {str(s["service"]): s for s in data}
        return jsonify({"ok": True, "services": services})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ────────────────────────────────────────────────────────────
# رصيد الأدمن من دارك فولو — للأدمن فقط
# ────────────────────────────────────────────────────────────
@app.route("/admin/darkfollow-balance", methods=["GET"])
def admin_darkfollow_balance():
    """جلب رصيد حساب دارك فولو — للأدمن فقط"""
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "balance"
        }, timeout=10)
        data = resp.json()
        balance = data.get("balance", data.get("funds", 0))
        return jsonify({"balance": balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # تفعيل أوامر البوت عند بدء التشغيل
    threading.Thread(target=set_bot_commands, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
