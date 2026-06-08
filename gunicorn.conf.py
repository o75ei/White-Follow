# gunicorn.conf.py
# ضع هذا الملف بجانب app.py على Railway

workers = 1          # worker واحد فقط — يحل مشكلة DB مزدوجة
preload_app = True   # يحمّل الكود مرة واحدة قبل fork
timeout = 120
bind = "0.0.0.0:8080"
worker_class = "sync"
