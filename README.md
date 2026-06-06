# ⚡ SMM Panel — دليل النشر الكامل

نظام إعادة بيع خدمات SMM مع بوت تيليجرام، لوحة إدارة سايبربانك، ودعم عربي/إنجليزي.

---

## 📁 هيكل الملفات

```
smm-panel/
├── app.py              ← الخادم الرئيسي (Flask)
├── admin.html          ← لوحة الإدارة (سايبربانك)
├── index.html          ← واجهة المستخدم (سايبربانك)
├── requirements.txt    ← مكتبات Python
├── Procfile            ← أمر التشغيل
├── railway.toml        ← إعدادات Railway
└── README.md           ← هذا الملف
```

---

## 🚀 نشر على Railway

### الخطوة 1 — رفع المشروع على GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/USERNAME/smm-panel.git
git push -u origin main
```

### الخطوة 2 — إنشاء مشروع على Railway

1. اذهب إلى [railway.app](https://railway.app)
2. **New Project → Deploy from GitHub repo**
3. اختر المستودع

### الخطوة 3 — إضافة Volume للبيانات

1. في لوحة Railway → **Add Volume**
2. Mount Path: `/data`
3. هذا يضمن بقاء قاعدة البيانات عند إعادة التشغيل

### الخطوة 4 — إعداد متغيرات البيئة

في **Variables** أضف ما يلي:

| المتغير | الوصف | مثال |
|---------|-------|-------|
| `SECRET_KEY` | مفتاح سري عشوائي | `openssl rand -hex 32` |
| `ADMIN_USERNAME` | اسم مستخدم الأدمن | `admin` |
| `ADMIN_PASSWORD` | كلمة مرور الأدمن | كلمة مرور قوية |
| `ADMIN_SECRET_KEY` | مفتاح API الأدمن للكرون | `openssl rand -hex 16` |
| `TELEGRAM_BOT_TOKEN` | توكن بوت تيليجرام | من @BotFather |
| `TELEGRAM_CHAT_ID` | معرف تشات الإشعارات | معرف حسابك |
| `WEBAPP_URL` | رابط التطبيق على Railway | `https://YOUR-APP.up.railway.app` |
| `SUPPORT_USERNAME` | يوزرنيم الدعم | `your_support` |
| `DB_PATH` | مسار قاعدة البيانات | `/data/smm.db` |
| `PORT` | يُضبط تلقائياً من Railway | — |

---

## 🤖 إعداد بوت تيليجرام

### 1. إنشاء البوت
- راسل [@BotFather](https://t.me/BotFather)
- `/newbot` → أدخل الاسم والمعرف
- احفظ **TOKEN**

### 2. تفعيل Web App
```
/newapp → اختر البوت → أدخل رابط Railway
```

### 3. ربط Webhook بعد النشر
افتح هذا الرابط مرة واحدة:
```
https://api.telegram.org/botTOKEN/setWebhook?url=https://YOUR-APP.up.railway.app/webhook
```

---

## 🔗 روابط النظام

| الرابط | الوصف |
|--------|-------|
| `https://YOUR-APP.up.railway.app/` | واجهة المستخدم |
| `https://YOUR-APP.up.railway.app/admin` | لوحة الإدارة |
| `https://YOUR-APP.up.railway.app/health` | فحص الصحة |
| `https://YOUR-APP.up.railway.app/cron/sync?key=KEY` | تشغيل الكرون خارجياً |

---

## ⚙️ إعداد Cron Job الخارجي

لضمان تحديث الطلبات حتى لو كان الخادم نائماً:

1. اذهب إلى [cron-job.org](https://cron-job.org) (مجاني)
2. **Create Cronjob**:
   - URL: `https://YOUR-APP.up.railway.app/cron/sync?key=YOUR_ADMIN_SECRET_KEY`
   - Schedule: كل دقيقة `* * * * *`
   - Method: GET

---

## 🏦 إضافة مزود API

من لوحة الإدارة → **مزودو API**:

1. أضف اسم المزود
2. أدخل رابط API (مثل `https://darkfollow.shop/api/v2`)
3. أدخل مفتاح API
4. اضغط **فحص** للتحقق من الاتصال

---

## 💰 هوامش الربح

لكل خدمة يمكنك تحديد:

| النوع | الوصف | مثال |
|-------|-------|-------|
| `percent` | نسبة مئوية | 20% → سعر المزود × 1.20 |
| `fixed` | مبلغ ثابت | +$0.5 → سعر المزود + 0.5 |

**تحديث الأسعار التلقائي**: زر "تحديث الأسعار" يجلب أسعار المزود الجديدة ويطبق الهامش المحفوظ تلقائياً.

---

## 🛡️ الأمان

- تشفير كلمات المرور بـ SHA-256
- حد أقصى 5 محاولات دخول قبل الحظر 15 دقيقة
- جلسات الأدمن تنتهي بعد 8 ساعات
- تسجيل كامل لجميع محاولات الدخول
- لا يوجد `/admin-panel` أو مسارات مكشوفة

---

## 🌐 دعم اللغات

من لوحة الإدارة → **اللغات والترجمة**:
- عدّل أي نص في الواجهة بدون تعديل الكود
- دعم كامل للعربية والإنجليزية
- المستخدم يختار لغته ويُحفظ

---

## 📊 المتطلبات

- Python 3.10+
- لا حاجة لـ MySQL أو PostgreSQL (SQLite مدمج)
- لا حاجة لـ Redis
- الاستضافة المجانية على Railway كافية للبداية

---

## 🆓 استضافة مجانية بديلة

| المنصة | الخطة المجانية | الملاحظات |
|--------|---------------|-----------|
| **Railway** | $5 رصيد شهرياً | ✅ الأفضل |
| **Render** | 750 ساعة/شهر | ⚠️ ينام بعد 15 دقيقة |
| **Fly.io** | 3 VMs مجانية | ✅ جيد |

**للإنتاج**: Railway Hobby ($5/شهر) أو Render Paid.
