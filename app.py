from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import os
import threading
import json
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
# السماح لجميع المصادر — الموقع يُفتح من المتصفح العادي وتلغرام WebApp
CORS(app, origins="*", supports_credentials=False)

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
ADMIN_SECRET_KEY   = os.environ.get("ADMIN_SECRET_KEY", "")   # ⚠️ يجب تعيينه في Railway
# رابط موقعك على Railway (يُحدَّث بعد النشر)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://YOUR-APP.up.railway.app/app2")
SUPPORT_USERNAME = "o75ei"

def _check_admin(req):
    """يتحقق من مفتاح الأدمن في Header أو JSON"""
    key = req.headers.get("X-Admin-Key", "") or (req.get_json(silent=True) or {}).get("admin_key", "")
    return ADMIN_SECRET_KEY and key == ADMIN_SECRET_KEY

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
                            "url": WEBAPP_URL
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
@app.route("/app2")
def serve_app():
    """يفتح الموقع — يعمل على /app و /app2"""
    from flask import make_response
    response = make_response(send_file("index.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

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
        print(f"[/order ERROR] {e}")
        return jsonify({"error": "خطأ في السيرفر"}), 500

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

@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "running"})

@app.route("/admin-panel")
def serve_admin_panel():
    from flask import make_response
    response = make_response(send_file("admin-panel.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

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
    if not _check_admin(request):
        return jsonify({"error": "غير مصرح"}), 403
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

@app.route("/user/find", methods=["GET"])
def user_find():
    """جلب بيانات مستخدم واحد بالـ uid — للأدمن وللمستخدم نفسه"""
    if not db:
        return jsonify({"error": "Firebase غير مفعّل"}), 503
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"error": "uid مطلوب"}), 400
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return jsonify({"error": "المستخدم غير موجود"}), 404
    return jsonify(doc.to_dict())

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
@app.route("/admin/users", methods=["GET"])
def user_list():
    """قائمة كل المستخدمين — للأدمن (يعمل على /user/list و /admin/users)"""
    if not _check_admin(request):
        return jsonify({"error": "غير مصرح"}), 403
    if not db:
        return jsonify({"users": [], "total": 0})
    docs = db.collection("users").order_by("joinedAt", direction=firestore.Query.DESCENDING).limit(200).get()
    users = [d.to_dict() for d in docs]
    return jsonify({"users": users, "total": len(users)})



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
        print(f"[services_list ERROR] {e}")
        return jsonify({"ok": False, "error": "خطأ في جلب الخدمات"})

@app.route("/admin/markup", methods=["GET"])
def admin_markup_get():
    """جلب هامش الربح من Firestore — للأدمن فقط"""
    if not _check_admin(request):
        return jsonify({"error": "غير مصرح"}), 403
    if not db:
        return jsonify({"markup": 1.0})
    doc = db.collection("settings").document("admin").get()
    if doc.exists:
        return jsonify({"markup": doc.to_dict().get("markup", 1.0)})
    return jsonify({"markup": 1.0})

@app.route("/admin/markup", methods=["POST"])
def admin_markup_set():
    """حفظ هامش الربح في Firestore — للأدمن فقط"""
    if not _check_admin(request):
        return jsonify({"error": "غير مصرح"}), 403
    if not db:
        return jsonify({"error": "Firebase غير مفعّل"}), 503
    data = request.get_json(silent=True) or {}
    markup = data.get("markup")
    if markup is None or not isinstance(markup, (int, float)) or float(markup) < 0:
        return jsonify({"error": "markup يجب أن يكون رقماً ≥ 0"}), 400
    db.collection("settings").document("admin").set({"markup": float(markup)}, merge=True)
    return jsonify({"ok": True, "markup": float(markup)})

@app.route("/admin/verify", methods=["POST"])
def admin_verify():
    """التحقق من مفتاح الأدمن — لا يكشف المفتاح، فقط يرد بـ ok/false"""
    if not ADMIN_SECRET_KEY:
        return jsonify({"ok": False, "error": "ADMIN_SECRET_KEY غير مضبوط"}), 503
    data = request.get_json(silent=True) or {}
    key  = data.get("admin_key", "")
    if key == ADMIN_SECRET_KEY:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 403

# ────────────────────────────────────────────────────────────
# رصيد الأدمن من دارك فولو — للأدمن فقط
# ────────────────────────────────────────────────────────────
@app.route("/admin/darkfollow-balance", methods=["GET"])
def admin_darkfollow_balance():
    """جلب رصيد حساب دارك فولو — للأدمن فقط"""
    if not _check_admin(request):
        return jsonify({"error": "غير مصرح"}), 403
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "balance"
        }, timeout=10)
        data = resp.json()
        # دارك فولو قد يرجع balance أو funds
        balance = data.get("balance", data.get("funds", data.get("Balance", 0)))
        return jsonify({"balance": balance, "raw": data})
    except Exception as e:
        print(f"[admin_darkfollow_balance ERROR] {e}")
        return jsonify({"error": "خطأ في جلب الرصيد", "balance": 0}), 200

@app.route("/order/status", methods=["GET"])
def order_status():
    """استعلام حالة طلب واحد من دارك فولو"""
    order_id = request.args.get("order_id", "").strip()
    if not order_id:
        return jsonify({"error": "order_id مطلوب"}), 400
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "status",
            "order": order_id
        }, timeout=10)
        return jsonify(resp.json())
    except Exception as e:
        print(f"[API ERROR] {e}")
        return jsonify({"error": "خطأ في السيرفر"}), 500

@app.route("/orders/status-bulk", methods=["POST"])
def orders_status_bulk():
    """استعلام حالة طلبات متعددة دفعة واحدة — يُستدعى من الكرون"""
    data = request.get_json()
    order_ids = data.get("orders", [])
    if not order_ids or not isinstance(order_ids, list):
        return jsonify({"error": "orders مطلوب (array)"}), 400
    # دارك فولو يقبل حتى 100 طلب في استعلام واحد
    ids_str = ",".join(str(i) for i in order_ids[:100])
    try:
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "status",
            "order": ids_str
        }, timeout=15)
        return jsonify(resp.json())
    except Exception as e:
        print(f"[API ERROR] {e}")
        return jsonify({"error": "خطأ في السيرفر"}), 500

# ────────────────────────────────────────────────────────────
# Cron Job — تحديث حالة الطلبات المعلقة في Firestore
# ────────────────────────────────────────────────────────────
def _cron_sync_orders():
    """
    يُشغَّل كل 60 ثانية.
    يجلب الطلبات pending/active من Firestore ويستعلم عن حالتها من دارك فولو،
    ثم يحدّث status و remains و progress في Firestore.
    """
    if not db:
        return
    try:
        # جلب الطلبات غير المكتملة فقط
        pending_docs = (
            db.collection("orders")
            .where("status", "in", ["pending", "active", "partial"])
            .limit(100)
            .get()
        )
        if not pending_docs:
            return

        order_map = {doc.to_dict().get("darkfollow_id"): doc
                     for doc in pending_docs
                     if doc.to_dict().get("darkfollow_id")}

        if not order_map:
            return

        ids_str = ",".join(str(k) for k in order_map.keys())
        resp = requests.post(DARKFOLLOW_API_URL, data={
            "key": DARKFOLLOW_API_KEY,
            "action": "status",
            "order": ids_str
        }, timeout=20)
        results = resp.json()  # { "order_id": { "status": ..., "remains": ..., "charge": ... } }

        for order_id_str, info in results.items():
            doc_ref = order_map.get(order_id_str)
            if not doc_ref:
                continue
            new_status  = str(info.get("status", "pending")).lower()
            remains     = info.get("remains", 0)
            charge      = info.get("charge", 0)
            doc_ref.reference.update({
                "status":    new_status,
                "remains":   remains,
                "charge":    charge,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
    except Exception as e:
        print(f"[CRON ERROR] order sync: {e}")

def _start_cron():
    """يبدأ خيط الكرون في الخلفية"""
    import time
    while True:
        time.sleep(60)
        _cron_sync_orders()

# ────────────────────────────────────────────────────────────
# تشغيل الكرون وأوامر البوت — يعمل مع Flask المباشر وGunicorn
# ────────────────────────────────────────────────────────────
def _startup():
    threading.Thread(target=set_bot_commands, daemon=True).start()
    threading.Thread(target=_start_cron, daemon=True).start()

_startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
