import io
import qrcode
import os
import sqlite3
import json
import time
import pandas as pd
import requests
import threading
import csv
import logging
import traceback
import sys
import psycopg2
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, g, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from dotenv import load_dotenv
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

# ==========================================
# TELEGRAM НАСТРОЙКИ
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = 'schools.db'

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# ==========================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ==========================================
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==========================================
# БЫСТРАЯ ЗАГРУЗКА И БЕЗОПАСНОСТЬ
# ==========================================
Compress(app)

app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 2592000
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',
)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000 per day", "500 per hour"],
    storage_uri="memory://"
)

MAIN_SITE_URL = os.getenv("MAIN_SITE_URL", "http://10.154.20.114")
MAIN_SITE_LOGIN_URL = f'{MAIN_SITE_URL}/login/'
MAIN_SITE_BOOKS_URL = f'{MAIN_SITE_URL}/books_rental_by_kh'

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'xls', 'xlsx', 'csv'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 МБ
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

login_attempts = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ==========================================
# АДАПТЕР ЗАПРОСОВ ДЛЯ РАЗНЫХ БАЗ ДАННЫХ
# ==========================================
def adapt_query(query):
    """Заменяет ? на %s, если используется PostgreSQL"""
    if 'DATABASE_URL' in os.environ:
        return query.replace('?', '%s')
    return query

# ==========================================
# УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ
# ==========================================
def get_db():
    if 'DATABASE_URL' in os.environ:
        # PostgreSQL на Render
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        return conn
    else:
        # SQLite локально
        if 'db' not in g:
            g.db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
            g.db.execute('PRAGMA journal_mode=WAL;')
            g.db.execute('PRAGMA synchronous=NORMAL;')
            g.db.execute('PRAGMA busy_timeout=30000;')
        return g.db

@app.teardown_appcontext
def close_db(exception):
    if 'DATABASE_URL' not in os.environ:
        db = g.pop('db', None)
        if db is not None:
            db.close()

def get_setting(key, default_val='on'):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(adapt_query("SELECT value FROM settings WHERE key = ?"), (key,))
        row = cursor.fetchone()
        if 'DATABASE_URL' in os.environ:
            conn.close()
        else:
            conn.close()
        return row[0] if row and row[0] else default_val
    except Exception:
        return default_val

def log_error_and_telegram(school_name, error, context=None):
    error_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    tb = traceback.extract_tb(error.__traceback__)
    last_call = tb[-1] if tb else None
    if last_call:
        error_details = f"""
📍 Файл: {last_call.filename}
📝 Функция: {last_call.name}
📌 Строка: {last_call.lineno}
❌ Ошибка: {str(error)}
🔍 Код: {last_call.line.strip() if last_call.line else 'Нет'}
📂 Контекст: {context if context else 'Не указан'}
⏰ Время: {error_time}
        """
    else:
        error_details = f"❌ Ошибка: {str(error)}\n⏰ Время: {error_time}"
    logger.error(error_details)
    send_telegram_alert(school_name, error_details, "system_error")
    return error_details

def send_telegram_alert(school_name, text, event_type="info"):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        if event_type == "issue" and get_setting('alert_issue') == 'off': return
        elif event_type == "book_error" and get_setting('alert_book_error') == 'off': return
        elif event_type == "system_error" and get_setting('alert_system_error') == 'off': return
        elif event_type == "student_error" and get_setting('alert_student_error') == 'off': return
        elif event_type == "upload_success" and get_setting('alert_upload_success') == 'off': return

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if event_type == "system_error":
            message = f"🚨 *ОШИБКА В СИСТЕМЕ*\n\n📍 Школа: {school_name}\n❌ Ошибка: {text}\n⏰ Дата: {now}"
        elif event_type == "book_error":
            message = f"📚❌ *ОШИБКА СИНХРОНИЗАЦИИ КНИГ*\n\n📍 Школа: {school_name}\n⚠️ Детали: {text}\n⏰ Дата: {now}"
        elif event_type == "student_error":
            message = f"⚠️ *ОШИБКА ДОБАВЛЕНИЯ (УЧЕНИКИ/ШКОЛЫ)*\n\n📍 Школа: {school_name}\n❌ Причина: {text}\n⏰ Дата: {now}"
        elif event_type == "upload_success":
            message = f"✅ *УСПЕШНОЕ ДОБАВЛЕНИЕ*\n\n📍 Школа: {school_name}\n📊 Результат: {text}\n⏰ Дата: {now}"
        elif event_type == "issue":
            message = f"📦 *УСПЕШНАЯ ВЫДАЧА КНИГ*\n\n📍 Школа: {school_name}\n✅ Действие: {text}\n⏰ Дата: {now}"
        else:
            message = f"ℹ️ *УВЕДОМЛЕНИЕ*\n\n📍 Школа: {school_name}\n📝 Сообщение: {text}\n⏰ Дата: {now}"

        if len(message) > 4000:
            message = message[:4000] + "\n\n... (сообщение обрезано)"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}

        def send_async():
            try:
                requests.post(url, json=payload, timeout=5)
            except Exception as e:
                print(f"⚠️ Ошибка отправки Telegram: {e}")

        threading.Thread(target=send_async).start()
    except Exception as e:
        print(f"⚠️ Не удалось сформировать Telegram уведомление: {e}")

def init_db():
    if 'DATABASE_URL' in os.environ:
        # PostgreSQL
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        cursor = conn.cursor()
        # Создание таблиц
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                name TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                time TEXT,
                school TEXT,
                student_name TEXT,
                books TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                school TEXT,
                student_id TEXT,
                name TEXT,
                grade TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS school_status (
                username TEXT PRIMARY KEY,
                last_seen TEXT
            )
        ''')
        # Индексы
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_students_school ON students (school)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_students_student_id ON students (student_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_school ON history (school)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)')

        # Настройки
        for key in ['alert_issue', 'alert_book_error', 'alert_system_error', 'alert_student_error', 'alert_upload_success']:
            cursor.execute("INSERT INTO settings (key, value) VALUES (%s, 'on') ON CONFLICT (key) DO NOTHING", (key,))

        # Суперадмин и тестовая школа
        cursor.execute("""
            INSERT INTO users (username, password, role, name) 
            VALUES (%s, %s, %s, %s) 
            ON CONFLICT (username) DO UPDATE SET password=EXCLUDED.password, role=EXCLUDED.role, name=EXCLUDED.name
        """, ('superadmin', generate_password_hash('AdminMaster2026!'), 'admin', 'Сармуҳаррири система'))

        cursor.execute("""
            INSERT INTO users (username, password, role, name) 
            VALUES (%s, %s, %s, %s) 
            ON CONFLICT (username) DO NOTHING
        """, ('devashtich-muassisa-14', generate_password_hash('123456'), 'school', 'Муассисаи №14 (Деваштич)'))

        conn.commit()
        conn.close()
        logger.info("✅ База данных PostgreSQL инициализирована")
    else:
        # SQLite
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, school TEXT, student_name TEXT, books TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY AUTOINCREMENT, school TEXT, student_id TEXT, name TEXT, grade TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS school_status (username TEXT PRIMARY KEY, last_seen TEXT)''')

        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_students_school ON students (school)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_students_student_id ON students (student_id)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_history_school ON history (school)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)''')

        for key in ['alert_issue', 'alert_book_error', 'alert_system_error', 'alert_student_error', 'alert_upload_success']:
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, 'on')", (key,))

        try:
            cursor.execute("ALTER TABLE students ADD COLUMN student_id TEXT")
        except sqlite3.OperationalError:
            pass

        cursor.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?)", ('superadmin', generate_password_hash('AdminMaster2026!'), 'admin', 'Сармуҳаррири система'))
        cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?)", ('devashtich-muassisa-14', generate_password_hash('123456'), 'school', 'Муассисаи №14 (Деваштич)'))
        conn.commit()
        conn.close()
        logger.info("✅ База данных SQLite инициализирована")

init_db()

# ==========================================
# HTML ШАБЛОНЫ (ПОЛНЫЕ)
# ==========================================
LOGIN_TEMPLATE = """<!DOCTYPE html><html lang="tj"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Воридшавӣ | Системаи Китобхона</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"><style>body { background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); height: 100vh; display: flex; align-items: center; justify-content: center; font-family: system-ui, sans-serif; }.login-card { width: 100%; max-width: 410px; padding: 30px; border-radius: 16px; background: rgba(255, 255, 255, 0.98); box-shadow: 0 10px 25px rgba(0,0,0,0.3); animation: slideUp 0.5s ease-out; }@keyframes slideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }.btn-custom { background: #2563eb; color: white; font-weight: 600; padding: 12px; border-radius: 8px; transition: all 0.3s; }.btn-custom:hover { background: #1d4ed8; transform: translateY(-2px); box-shadow: 0 5px 15px rgba(37,99,235,0.4); }.form-control { transition: all 0.3s; }.form-control:focus { border-color: #2563eb; box-shadow: 0 0 0 0.2rem rgba(37,99,235,0.25); }@keyframes shake { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-10px); } 75% { transform: translateX(10px); } }</style></head><body><div class="login-card text-center"><div class="mb-3"><i class="bi bi-shield-lock-fill text-primary display-4"></i><h4 class="fw-bold mt-2 text-dark">Воридшавӣ ба барнома</h4><p class="text-muted small">Логин ва рамзи кабинети худро ворид кунед</p></div>{% if error %}<div class="alert alert-danger py-2 small text-center" style="animation: shake 0.5s;">{{ error }}</div>{% endif %}<form method="POST" class="text-start"><div class="mb-3"><label class="form-label text-secondary small fw-bold">Логин</label><input type="text" name="username" class="form-control" placeholder="Логин" autocomplete="off" required></div><div class="mb-4"><label class="form-label text-secondary small fw-bold">Рамз</label><input type="password" name="password" class="form-control" placeholder="Рамз" required></div><button type="submit" class="btn btn-custom w-100">ВОРИДШАВӢ</button></form></div></body></html>"""

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="tj">
<head>
    <meta charset="UTF-8">
    <title>Панели Супер-админ</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    <style>
        body { background-color: #f8f9fa; font-family: system-ui, -apple-system, sans-serif; }
        .card { border: none; border-radius: 16px; box-shadow: 0 4px 15px rgba(0,0,0,0.03); }
        .table-rounded { border-radius: 12px; overflow: hidden; }
        .form-switch .form-check-input { width: 3em; height: 1.5em; cursor: pointer; }
    </style>
</head>
<body class="p-4">
    <div class="container" style="max-width: 950px;">
        <div class="d-flex justify-content-between align-items-center mb-4 bg-white p-3 rounded-4 shadow-sm">
            <h3 class="fw-bold text-primary mb-0"><i class="bi bi-shield-fill-check me-2"></i>Панели Сармуҳаррир</h3>
            <a href="/logout" class="btn btn-outline-danger fw-bold px-4"><i class="bi bi-box-arrow-right me-1"></i> Баромадан</a>
        </div>

        {% if msg %}<div class="alert alert-success py-3 rounded-3 shadow-sm fw-semibold"><i class="bi bi-check-circle-fill me-2"></i>{{ msg }}</div>{% endif %}
        {% if err_msg %}<div class="alert alert-danger py-3 rounded-3 shadow-sm fw-semibold"><i class="bi bi-exclamation-triangle-fill me-2"></i>{{ err_msg }}</div>{% endif %}

        <div class="card p-4 mb-4" style="background: linear-gradient(to right, #fffdf2, #ffffff); border-left: 5px solid #ffc107;">
            <h5 class="fw-bold mb-4 text-dark"><i class="bi bi-bell-fill text-warning me-2"></i>Танзимоти Telegram-огоҳиномаҳо</h5>
            <div class="d-flex justify-content-between align-items-center mb-3 p-3 bg-white rounded-3 shadow-sm border">
                <div>
                    <h6 class="mb-1 fw-bold text-dark">1. Успешная выдача книг</h6>
                    <small class="text-muted">Уведомления о том, что книги были успешно выданы</small>
                </div>
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input setting-toggle" type="checkbox" data-key="alert_issue" {% if alert_issue == 'on' %}checked{% endif %}>
                </div>
            </div>
            <div class="d-flex justify-content-between align-items-center mb-3 p-3 bg-white rounded-3 shadow-sm border">
                <div>
                    <h6 class="mb-1 fw-bold text-dark">2. Ошибки книг (не найдены на сайте)</h6>
                    <small class="text-muted">Ошибки синхронизации или отправки книг на сервер</small>
                </div>
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input setting-toggle" type="checkbox" data-key="alert_book_error" {% if alert_book_error == 'on' %}checked{% endif %}>
                </div>
            </div>
            <div class="d-flex justify-content-between align-items-center mb-3 p-3 bg-white rounded-3 shadow-sm border">
                <div>
                    <h6 class="mb-1 fw-bold text-dark">3. Системные ошибки</h6>
                    <small class="text-muted">Ошибки в системе и проблемы со входом</small>
                </div>
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input setting-toggle" type="checkbox" data-key="alert_system_error" {% if alert_system_error == 'on' %}checked{% endif %}>
                </div>
            </div>
            <div class="d-flex justify-content-between align-items-center mb-3 p-3 bg-white rounded-3 shadow-sm border">
                <div>
                    <h6 class="mb-1 fw-bold text-dark">4. Ошибки при добавлении учеников/школ</h6>
                    <small class="text-muted">Ошибки заполнения данных, дубликаты или неверный Excel</small>
                </div>
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input setting-toggle" type="checkbox" data-key="alert_student_error" {% if alert_student_error == 'on' %}checked{% endif %}>
                </div>
            </div>
            <div class="d-flex justify-content-between align-items-center p-3 bg-white rounded-3 shadow-sm border">
                <div>
                    <h6 class="mb-1 fw-bold text-dark">5. Успешно добавленные ученики и школы</h6>
                    <small class="text-muted">Уведомления об успешной загрузке баз и новых школ</small>
                </div>
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input setting-toggle" type="checkbox" data-key="alert_upload_success" {% if alert_upload_success == 'on' %}checked{% endif %}>
                </div>
            </div>
            <div class="text-end mt-2 text-success fw-bold small" id="save-toast" style="opacity: 0; transition: 0.3s;"><i class="bi bi-check-circle me-1"></i> Танзимот сабт шуд!</div>
        </div>

        <div class="row g-4 mb-4">
            <div class="col-md-6">
                <div class="card p-4 h-100">
                    <h5 class="fw-bold mb-3"><i class="bi bi-plus-circle text-success me-2"></i>Илова кардани 1 мактаб</h5>
                    <form method="POST">
                        <input type="hidden" name="action" value="add_single">
                        <div class="mb-2"><input type="text" name="new_username" class="form-control" placeholder="Логин" required></div>
                        <div class="mb-2"><input type="text" name="new_password" class="form-control" placeholder="Рамз" required></div>
                        <div class="mb-3"><input type="text" name="new_name" class="form-control" placeholder="Номи муассиса" required></div>
                        <button type="submit" class="btn btn-success w-100 fw-bold"><i class="bi bi-check-lg me-1"></i> Сабт кардан</button>
                    </form>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card p-4 h-100">
                    <h5 class="fw-bold mb-3 text-primary"><i class="bi bi-file-earmark-excel me-2"></i>Боргузорӣ аз Excel/CSV</h5>
                    <div class="alert alert-info py-2 small mb-3">Файл бояд форматҳои <b>.xls</b>, <b>.xlsx</b> ё <b>.csv</b> дошта бошад. Ҳаҷм: макс. 5 МБ. Сутунҳо: Логин | Рамз | Ном</div>
                    <form method="POST" enctype="multipart/form-data">
                        <input type="hidden" name="action" value="upload_excel">
                        <div class="mb-3"><input type="file" name="file" class="form-control" accept=".xls,.xlsx,.csv" required></div>
                        <button type="submit" class="btn btn-primary w-100 fw-bold"><i class="bi bi-upload me-1"></i> Боргузорӣ</button>
                    </form>
                </div>
            </div>
        </div>

        <div class="card p-4">
            <h5 class="fw-bold mb-3"><i class="bi bi-list-ul text-secondary me-2"></i>Рӯйхати мактабҳо</h5>
            <div class="table-responsive table-rounded border">
                <table class="table table-hover table-striped align-middle mb-0">
                    <thead class="table-dark"><tr><th>Логин</th><th>Номи мактаб</th><th>Статус</th><th>Охирин бор</th><th class="text-center">Амал</th></tr></thead>
                    <tbody>
                        {% for school in schools %}
                        <tr>
                            <td><span class="badge bg-secondary fs-6">{{ school[0] }}</span></td>
                            <td class="fw-semibold">{{ school[1] }}</td>
                            <td><span class="badge {% if 'Онлайн' in school[3] %}bg-success{% else %}bg-danger{% endif %}">{{ school[3] }}</span></td>
                            <td><small class="text-muted">{{ school[4] }}</small></td>
                            <td class="text-center">
                                {% if school[0] != 'superadmin' %}
                                <a href="/delete_school/{{ school[0] }}" class="btn btn-danger btn-sm fw-bold" onclick="return confirm('Шумо мутмаин ҳастед?');"><i class="bi bi-trash-fill"></i></a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        document.querySelectorAll('.setting-toggle').forEach(toggle => {
            toggle.addEventListener('change', function() {
                const key = this.getAttribute('data-key');
                const value = this.checked ? 'on' : 'off';

                fetch('/toggle_setting', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: key, value: value })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        const toast = document.getElementById('save-toast');
                        toast.style.opacity = '1';
                        setTimeout(() => { toast.style.opacity = '0'; }, 2000);
                    } else {
                        alert('Хатогӣ ҳангоми сабт!');
                        this.checked = !this.checked;
                    }
                })
                .catch(error => {
                    alert('Хатогии интернет!');
                    this.checked = !this.checked;
                });
            });
        });
    </script>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="tj">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Супоридани китобҳо</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/html5-qrcode"></script>
    <style>
        body { background-color: #f8fafc; font-family: system-ui, sans-serif; -webkit-tap-highlight-color: transparent; }
        .navbar { background: #0f172a; }
        .card { border: none; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .student-btn { border: none; border-bottom: 1px solid #f1f5f9; padding: 14px 16px; cursor: pointer; transition: 0.2s; }
        .student-btn:hover { background-color: #f1f5f9; }
        .student-btn.active { background-color: #2563eb !important; color: white !important; border-radius: 8px; }
        .student-btn.active .badge { background-color: white !important; color: #2563eb !important; }
        .student-btn.active .text-muted { color: #e2e8f0 !important; }
        .book-item { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; margin-bottom: 8px; }
        #qr-reader { width: 100%; border-radius: 10px; overflow: hidden; background: #000; }
        * { transition: all 0.3s ease; }
        .card { animation: fadeIn 0.5s ease-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .btn { transition: all 0.3s; }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
        .btn-success:active { animation: pulse 0.5s; }
        @keyframes pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }
        .student-btn:hover { transform: translateX(5px); }
        .student-btn.active { transform: scale(1.02); }
        .book-item { animation: slideInLeft 0.3s ease-out; }
        @keyframes slideInLeft { from { opacity: 0; transform: translateX(-20px); } to { opacity: 1; transform: translateX(0); } }

        .chat-toggle-btn {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 55px;
            height: 55px;
            border-radius: 50%;
            background-color: #2563eb;
            color: white;
            border: none;
            box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4);
            z-index: 1050;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 26px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .chat-toggle-btn:hover {
            transform: scale(1.1);
            background-color: #1d4ed8;
        }
        .chat-box {
            position: fixed;
            bottom: 85px;
            right: 20px;
            width: 340px;
            max-width: 90vw;
            height: 480px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
            z-index: 1050;
            display: none;
            flex-direction: column;
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }
        .chat-header {
            background: #0f172a;
            color: white;
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: bold;
        }
        .chat-body {
            flex: 1;
            padding: 12px;
            overflow-y: auto;
            background: #f8fafc;
            font-size: 14px;
        }
        .chat-quick-replies {
            padding: 10px;
            background: #ffffff;
            border-top: 1px solid #e2e8f0;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }
        .quick-btn {
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            color: #334155;
            border-radius: 12px;
            padding: 6px 12px;
            font-size: 12px;
            cursor: pointer;
            transition: 0.2s;
        }
        .quick-btn:hover {
            background: #e2e8f0;
            border-color: #94a3b8;
        }
        .chat-footer {
            padding: 10px;
            background: white;
            border-top: 1px solid #e2e8f0;
            display: flex;
            gap: 6px;
        }
        .chat-msg {
            margin-bottom: 10px;
            padding: 8px 12px;
            border-radius: 12px;
            max-width: 85%;
            word-wrap: break-word;
        }
        .chat-msg.bot {
            background: #e2e8f0;
            color: #0f172a;
            align-self: flex-start;
        }
        .chat-msg.user {
            background: #2563eb;
            color: white;
            align-self: flex-end;
            margin-left: auto;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark mb-3 py-2 px-3">
        <div class="container-fluid d-flex justify-content-between align-items-center">
            <span class="navbar-brand mb-0 h1 fs-6 text-truncate" style="max-width: 40%;"><i class="bi bi-building me-1 text-warning"></i> {{ school_name }}</span>
            <div class="d-flex gap-1 align-items-center">
                <a href="#offlineSection" class="btn btn-outline-danger btn-sm position-relative me-1" id="offlineNavBtn" style="display:none;">
                    <i class="bi bi-cloud-slash"></i>
                    <span class="d-none d-sm-inline ms-1">Ирсолнашуда</span>
                    <span class="position-absolute top-0 start-100 translate-middle badge rounded-pill bg-danger" id="offlineBadge">0</span>
                </a>
                <a href="/upload_students_page" class="btn btn-outline-warning btn-sm"><i class="bi bi-file-earmark-excel"></i> <span class="d-none d-sm-inline">База</span></a>
                <a href="/history" class="btn btn-outline-info btn-sm"><i class="bi bi-clock-history"></i> <span class="d-none d-sm-inline">Таърих</span></a>
                <a href="/logout" class="btn btn-outline-light btn-sm"><i class="bi bi-box-arrow-right"></i></a>
            </div>
        </div>
    </nav>

    <div class="container-fluid px-2 px-md-3">
        <div class="row g-3">
            <div class="col-lg-4">
                <div class="card p-3">
                    <h6 class="fw-bold mb-2"><i class="bi bi-people me-2 text-primary"></i>1. Хонандаро интихоб кунед</h6>
                    <input type="text" id="searchStudent" class="form-control mb-3 py-2" placeholder="Ҷустуҷӯ аз рӯи ном ё ID..." autocomplete="off">
                    <div class="list-group overflow-auto" id="studentsList" style="max-height: 400px;">
                        {% for student in students %}
                        <div class="student-btn d-flex justify-content-between align-items-center" data-name="{{ student.name }}" data-grade="{{ student.grade }}" data-id="{{ student.id }}">
                            <div>
                                <div class="fw-semibold student-name">{{ student.name }}</div>
                                <small class="text-muted" style="font-size: 0.75rem;">ID: {{ student.id }}</small>
                            </div>
                            <span class="badge bg-secondary rounded-pill">{{ student.grade }}</span>
                        </div>
                        {% else %}
                        <div class="text-center text-muted p-3">Хонандагон ёфт нашудаанд! Ба база файл бор кунед.</div>
                        {% endfor %}
                    </div>
                </div>
            </div>
            <div class="col-lg-8">
                <div class="card p-3 mb-3">
                    <div class="d-flex justify-content-between align-items-center pb-2 mb-3 border-bottom">
                        <h6 class="fw-bold mb-0"><i class="bi bi-camera me-2 text-primary"></i>2. Скан кардани китобҳо</h6>
                        <span id="studentBadge" class="badge bg-primary fs-6" style="display:none;"></span>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <div id="qr-reader"></div>
                            <button id="startScanBtn" class="btn btn-primary w-100 mt-2 py-3 fw-semibold fs-6"><i class="bi bi-camera-video me-1"></i> Камераро фаъол кунед</button>
                        </div>
                        <div class="col-md-6">
                            <h6 class="fw-bold mb-2">Китобҳои сканшуда:</h6>
                            <div id="scannedBooksList" class="p-2 bg-light rounded border mb-3" style="min-height: 180px; max-height: 250px; overflow-y: auto;">
                                <small class="text-muted">QR-кодро ба камера наздик кунед...</small>
                            </div>
                            <div class="alert alert-info py-2" id="statusBox" style="display:none;"></div>
                            <button class="btn btn-success btn-lg w-100 shadow-sm fw-bold py-3" id="sendBtn" disabled><i class="bi bi-check-circle-fill me-2"></i> СУПОРИДАНИ КИТОБҲО</button>
                        </div>
                    </div>
                </div>

                <div class="card p-3 border-danger mb-4" id="offlineSection" style="display: none; background: #fff5f5;">
                    <div class="d-flex justify-content-between align-items-center mb-3 border-bottom pb-2">
                        <h6 class="fw-bold text-danger mb-0"><i class="bi bi-wifi-off me-2"></i>Китобҳои ирсолнашуда (Офлайн-очередь)</h6>
                        <button class="btn btn-sm btn-outline-danger" onclick="retryAllOffline()"><i class="bi bi-arrow-clockwise me-1"></i> Ҳамаро ирсол кардан</button>
                    </div>
                    <p class="small text-muted mb-2">Ин китобҳо бинобар хатогии интернет ирсол нашудаанд. Ҳангоми пайваст шудан ба интернет тугмаи повторро пахш кунед.</p>
                    <div id="offlineCardsList"></div>
                </div>
            </div>
        </div>
    </div>

    <div class="modal fade" id="bookCategoryModal" tabindex="-1" aria-hidden="true" data-bs-backdrop="static">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header bg-primary text-white">
                    <h5 class="modal-title fw-bold"><i class="bi bi-book me-2"></i>Категорияи китобро интихоб кунед</h5>
                </div>
                <div class="modal-body">
                    <p class="mb-2">Китоби сканшуда: <strong id="modalBookTitle" class="text-primary"></strong></p>
                    <label class="form-label fw-bold">Ҳолат / Соли иҷора:</label>
                    <select id="modalCategorySelect" class="form-select form-select-lg mb-3">
                        <option value="Соли якуми иҷора" selected>Соли якуми иҷора (34% - Нав)</option>
                        <option value="Соли дуюми иҷора">Соли дуюми иҷора (33%)</option>
                        <option value="Соли сеюми иҷора">Соли сеюми иҷора (33%)</option>
                        <option value="Соли чоруми иҷора">Соли чоруми иҷора (16.5%)</option>
                        <option value="Соли панҷум ва минбаъда">Соли панҷум ва минбаъда (8.25%)</option>
                        <option value="Яклухт">Яклухт (100%)</option>
                    </select>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary py-2" id="cancelBookBtn">Ислоҳ / Нест кардан</button>
                    <button type="button" class="btn btn-success fw-bold px-4 py-2" id="confirmBookBtn">Тасдиқ кардан</button>
                </div>
            </div>
        </div>
    </div>

    <audio id="beepSound" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>

    <button class="chat-toggle-btn" id="openChatBtn" onclick="toggleChat()">
        <i class="bi bi-robot"></i>
    </button>

    <div class="chat-box" id="chatBox">
        <div class="chat-header">
            <span><i class="bi bi-robot me-2 text-warning"></i>Ёрдамч (Помощник)</span>
            <button type="button" class="btn-close btn-close-white" onclick="toggleChat()"></button>
        </div>

        <div class="chat-body d-flex flex-column" id="chatMessages">
            <div class="chat-msg bot">
                Салом! Ман ёрдамчии шумо ҳастам. Кадом савол доред?
            </div>
        </div>

        <div class="chat-quick-replies">
            <button class="quick-btn" onclick="sendQuickMsg('Чӣ тавр базаро бор кунем?')">📊 Боркунии база</button>
            <button class="quick-btn" onclick="sendQuickMsg('Китобро чӣ тавр супорем?')">📖 Скан ва супоридан</button>
            <button class="quick-btn" onclick="sendQuickMsg('Камера кор намекунад')">📷 Камера хатогӣ</button>
            <button class="quick-btn" onclick="sendQuickMsg('Таърих')">📜 Таърих</button>
            <button class="quick-btn" onclick="sendQuickMsg('Тамос бо таҳиягар')">👨‍💻 Тамос бо таҳиягар</button>
        </div>

        <div class="chat-footer">
            <input type="text" id="chatInput" class="form-control form-control-sm" placeholder="Саволи худро нависед..." onkeypress="if(event.key==='Enter') sendChatMessage()">
            <button class="btn btn-primary btn-sm px-3" onclick="sendChatMessage()"><i class="bi bi-send"></i></button>
        </div>
    </div>

    <script>
        let selectedStudentName = "";
        let selectedStudentId = "";
        let scannedBooksList = [];
        let html5QrCode = null;
        let tempScannedBook = null;
        let isProcessingScan = false;
        let categoryModal = new bootstrap.Modal(document.getElementById('bookCategoryModal'));

        function normalizeText(text) {
            return text.toLowerCase().replace(/ӯ/g, 'у').replace(/ҳ/g, 'х').replace(/ҷ/g, 'ч').replace(/ӣ/g, 'и').replace(/ғ/g, 'г').replace(/қ/g, 'к').trim();
        }

        document.getElementById('searchStudent').addEventListener('input', function() {
            let filter = normalizeText(this.value);
            document.querySelectorAll('.student-btn').forEach(item => {
                let name = normalizeText(item.querySelector('.student-name').innerText);
                let sid = normalizeText(item.dataset.id || '');
                if (name.includes(filter) || sid.includes(filter)) {
                    item.classList.remove('d-none');
                    item.classList.add('d-flex');
                } else {
                    item.classList.remove('d-flex');
                    item.classList.add('d-none');
                }
            });
        });

        document.querySelectorAll('.student-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                document.querySelectorAll('.student-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                selectedStudentName = this.dataset.name;
                selectedStudentId = this.dataset.id;
                const badge = document.getElementById('studentBadge');
                badge.style.display = 'inline-block';
                badge.innerText = selectedStudentName + " (ID: " + selectedStudentId + ")";
                checkReady();
            });
        });

        function onScanSuccess(decodedText) {
            if (isProcessingScan) return;
            let rawCode = decodedText.trim();
            if (!rawCode) return;

            let NL = String.fromCharCode(10);
            let CR = String.fromCharCode(13);
            let parsedTitle = rawCode;

            try {
                let bookNameMatch = rawCode.match(/(?:Китоб|Имя|Название):\s*(.+)/i);
                if (bookNameMatch) {
                    let bookName = bookNameMatch[1].split(NL)[0].replace(CR, '').trim();
                    let bookClass = rawCode.match(/(?:Синф|Класс):\s*(.+)/i);
                    let quantity = rawCode.match(/(?:Шумора|Количество):\s*(.+)/i);
                    let price = rawCode.match(/(?:Нарх|Цена):\s*(.+)/i);
                    let year = rawCode.match(/(?:Соли воридшавӣ|Год):\s*(.+)/i);

                    let details = [];
                    if (bookClass) details.push("Синф: " + bookClass[1].split(NL)[0].replace(CR, '').trim());
                    if (year) details.push("Сол: " + year[1].split(NL)[0].replace(CR, '').trim());
                    if (quantity) details.push("Шумора: " + quantity[1].split(NL)[0].replace(CR, '').trim());
                    if (price) details.push("Нарх: " + price[1].split(NL)[0].replace(CR, '').trim());

                    parsedTitle = details.length > 0 ? bookName + " [" + details.join(' | ') + "]" : bookName;
                }
            } catch (e) {
                console.error("Ошибка парсинга QR:", e);
                parsedTitle = rawCode;
            }

            if (scannedBooksList.some(b => b.title === parsedTitle)) {
                let sb = document.getElementById('statusBox');
                sb.style.display = 'block';
                sb.className = 'alert alert-danger py-2 fw-semibold';
                sb.innerText = "⚠️ Ин китоб аллакай илова шудааст!";
                return;
            }

            isProcessingScan = true;
            document.getElementById('beepSound').play().catch(()=>{});

            tempScannedBook = parsedTitle;
            document.getElementById('modalBookTitle').innerText = parsedTitle;

            if (html5QrCode) { html5QrCode.pause(true); }
            categoryModal.show();
        }

        document.getElementById('confirmBookBtn').addEventListener('click', function() {
            if (tempScannedBook) {
                scannedBooksList.push({ id: Date.now(), title: tempScannedBook, category: document.getElementById('modalCategorySelect').value });
                tempScannedBook = null;
                renderScannedBooks();
                checkReady();
                document.getElementById('statusBox').style.display = 'none';
            }
            categoryModal.hide();
            isProcessingScan = false;
            if (html5QrCode) { html5QrCode.resume(); }
        });

        document.getElementById('cancelBookBtn').addEventListener('click', function() {
            tempScannedBook = null;
            categoryModal.hide();
            isProcessingScan = false;
            if (html5QrCode) { html5QrCode.resume(); }
        });

        function removeBook(id) {
            scannedBooksList = scannedBooksList.filter(b => b.id !== id);
            renderScannedBooks();
            checkReady();
        }

        function renderScannedBooks() {
            const container = document.getElementById('scannedBooksList');
            if (scannedBooksList.length === 0) {
                container.innerHTML = '<small class="text-muted">QR-кодро ба камера наздик кунед...</small>';
                return;
            }
            container.innerHTML = '';
            scannedBooksList.forEach(item => {
                container.innerHTML += `<div class="book-item d-flex justify-content-between align-items-center"><div><strong><i class="bi bi-book text-primary me-1"></i> ${item.title}</strong><div class="text-muted small">${item.category}</div></div><button onclick="removeBook(${item.id})" class="btn btn-sm btn-outline-danger">&times;</button></div>`;
            });
        }

        document.getElementById('startScanBtn').addEventListener('click', function() {
            if (!html5QrCode) { html5QrCode = new Html5Qrcode("qr-reader"); }
            html5QrCode.start({ facingMode: "environment" }, { fps: 10, qrbox: { width: 220, height: 220 } }, onScanSuccess).catch(err => alert("Хатогӣ дар кушодани камера!"));
        });

        function checkReady() {
            document.getElementById('sendBtn').disabled = !(selectedStudentName && scannedBooksList.length > 0);
        }

        function getOfflineQueue() {
            return JSON.parse(localStorage.getItem('offline_books_queue') || '[]');
        }

        function saveOfflineQueue(queue) {
            localStorage.setItem('offline_books_queue', JSON.stringify(queue));
            renderOfflineQueue();
        }

        function addToOfflineQueue(studentName, studentId, books) {
            let queue = getOfflineQueue();
            queue.push({
                queue_id: Date.now(),
                student_name: studentName,
                student_id: studentId,
                books: books,
                date: new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})
            });
            saveOfflineQueue(queue);
        }

        function renderOfflineQueue() {
            let queue = getOfflineQueue();
            let section = document.getElementById('offlineSection');
            let list = document.getElementById('offlineCardsList');
            let badge = document.getElementById('offlineBadge');
            let navBtn = document.getElementById('offlineNavBtn');

            if (queue.length === 0) {
                section.style.display = 'none';
                navBtn.style.display = 'none';
                return;
            }

            section.style.display = 'block';
            navBtn.style.display = 'inline-block';
            badge.innerText = queue.length;

            list.innerHTML = '';
            queue.forEach(item => {
                let booksHtml = item.books.map(b => `<span class="badge bg-secondary me-1 mb-1">${b.title} (${b.category})</span>`).join('');
                list.innerHTML += `
                    <div class="card mb-2 p-2 border shadow-sm bg-white">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <strong class="text-dark"><i class="bi bi-person me-1"></i>${item.student_name}</strong>
                                <span class="badge bg-light text-dark border">ID: ${item.student_id}</span>
                                <small class="text-muted ms-2">${item.date}</small>
                            </div>
                            <div>
                                <button class="btn btn-sm btn-success fw-bold me-1" onclick="retrySingleOffline(${item.queue_id})"><i class="bi bi-arrow-clockwise"></i> Такроран ирсол</button>
                                <button class="btn btn-sm btn-outline-danger" onclick="deleteOfflineItem(${item.queue_id})"><i class="bi bi-trash"></i></button>
                            </div>
                        </div>
                        <div class="mt-2">${booksHtml}</div>
                    </div>
                `;
            });
        }

        async function retrySingleOffline(queueId) {
            let queue = getOfflineQueue();
            let itemIndex = queue.findIndex(q => q.queue_id === queueId);
            if (itemIndex === -1) return;

            let item = queue[itemIndex];
            try {
                let response = await fetch('/issue_books', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        student_name: item.student_name,
                        student_id: item.student_id,
                        books: item.books
                    })
                });
                let data = await response.json();
                if (data.success) {
                    queue.splice(itemIndex, 1);
                    saveOfflineQueue(queue);
                    alert("✅ Маълумоти " + item.student_name + " муваффақона ирсол шуд!");
                    if (data.redirect_url) {
                        setTimeout(() => { window.open(data.redirect_url, '_blank'); }, 1000);
                    }
                } else {
                    alert("❌ Хатогӣ: " + data.message);
                }
            } catch (e) {
                alert("❌ Ҳануз пайвастшавӣ ба интернет нест!");
            }
        }

        async function retryAllOffline() {
            let queue = getOfflineQueue();
            if (queue.length === 0) return;

            let remaining = [];
            for (let item of queue) {
                try {
                    let response = await fetch('/issue_books', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            student_name: item.student_name,
                            student_id: item.student_id,
                            books: item.books
                        })
                    });
                    let data = await response.json();
                    if (data.success) {
                        if (data.redirect_url) {
                            window.open(data.redirect_url, '_blank');
                        }
                    } else {
                        remaining.push(item);
                    }
                } catch (e) {
                    remaining.push(item);
                }
            }
            saveOfflineQueue(remaining);
            if (remaining.length === 0) {
                alert("✅ Ҳамаи китобҳои ирсолнашуда бо муваффақият сабт шуданд!");
            } else {
                alert("⚠️ Каме аз китобҳо ирсол нашуданд. Бори дигар кӯшиш кунед.");
            }
        }

        function deleteOfflineItem(queueId) {
            if (!confirm('Ин сабтро нест кардан мехоҳед?')) return;
            let queue = getOfflineQueue().filter(q => q.queue_id !== queueId);
            saveOfflineQueue(queue);
        }

        window.addEventListener('online', function() {
            retryAllOffline();
        });

        function updateOnlineStatus() {
            fetch('/update_status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }).catch(() => {});
        }

        setInterval(updateOnlineStatus, 30000);
        updateOnlineStatus();

        window.addEventListener('beforeunload', function() {
            navigator.sendBeacon('/update_status', '');
        });

        document.getElementById('sendBtn').addEventListener('click', async function() {
            let sb = document.getElementById('statusBox');
            sb.style.display = 'block';
            sb.className = 'alert alert-warning py-2 fw-semibold';
            sb.innerText = "Нигоҳ дошта мешавад...";

            try {
                let response = await fetch('/issue_books', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        student_name: selectedStudentName,
                        student_id: selectedStudentId,
                        books: scannedBooksList
                    })
                });
                let data = await response.json();
                if(data.success) {
                    sb.className = 'alert alert-success py-2 fw-semibold';
                    sb.innerText = "✅ " + data.message;
                    scannedBooksList = [];
                    renderScannedBooks();
                    if(data.redirect_url) {
                        setTimeout(() => { window.open(data.redirect_url, '_blank'); }, 1500);
                    }
                } else {
                    sb.className = 'alert alert-danger py-2 fw-semibold';
                    sb.innerText = "❌ " + data.message;
                }
            } catch (err) {
                addToOfflineQueue(selectedStudentName, selectedStudentId, [...scannedBooksList]);
                sb.className = 'alert alert-danger py-2 fw-semibold';
                sb.innerText = "⚠️ Хатогии алоқа! Маълумот дар бахши «Китобҳои ирсолнашуда» сабт шуд.";
                scannedBooksList = [];
                renderScannedBooks();
            } finally {
                checkReady();
            }
        });

        function toggleChat() {
            const chatBox = document.getElementById('chatBox');
            chatBox.style.display = (chatBox.style.display === 'flex') ? 'none' : 'flex';
        }

        function sendQuickMsg(text) {
            const input = document.getElementById('chatInput');
            input.value = text;
            sendChatMessage();
        }

        async function sendChatMessage() {
            const input = document.getElementById('chatInput');
            const msg = input.value.trim();
            if (!msg) return;

            const chatMessages = document.getElementById('chatMessages');

            const userDiv = document.createElement('div');
            userDiv.className = 'chat-msg user';
            userDiv.innerText = msg;
            chatMessages.appendChild(userDiv);

            input.value = '';
            chatMessages.scrollTop = chatMessages.scrollHeight;

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: msg })
                });
                const data = await res.json();

                const botDiv = document.createElement('div');
                botDiv.className = 'chat-msg bot';
                botDiv.innerHTML = data.reply;
                chatMessages.appendChild(botDiv);
                chatMessages.scrollTop = chatMessages.scrollHeight;
            } catch (e) {
                console.error('Ошибка отправки сообщения:', e);
            }
        }

        document.addEventListener("DOMContentLoaded", function() {
            renderOfflineQueue();
        });
    </script>
</body>
</html>
"""

UPLOAD_PAGE_TEMPLATE = """<!DOCTYPE html><html lang="tj"><head><meta charset="UTF-8"><title>Боргузории база</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"></head><body class="bg-light p-5"><div class="container" style="max-width: 600px;"><div class="card p-4 shadow-sm"><h4 class="fw-bold mb-3"><i class="bi bi-file-earmark-excel text-success me-2"></i>Боргузории файли хонандагон</h4>{% if msg %}<div class="alert alert-info py-2">{{ msg }}</div>{% endif %}<form method="POST" enctype="multipart/form-data"><div class="mb-3"><input type="file" name="file" class="form-control" accept=".xls,.xlsx,.csv" required></div><button type="submit" class="btn btn-success w-100 fw-bold py-2 mb-3"><i class="bi bi-upload me-1"></i> Боргузорӣ кардан</button></form><hr><div class="mt-2 text-center"><a href="/clear_students" class="btn btn-danger w-100 fw-bold py-2" onclick="return confirm('Диққат! Ҳамаи хонандагон нест карда мешаванд. Оё идома медиҳед?');"><i class="bi bi-trash-fill me-1"></i> Нест кардани ҳамаи хонандагон</a></div><div class="mt-4 text-center"><a href="/dashboard" class="btn btn-outline-secondary btn-sm"><i class="bi bi-arrow-left"></i> Ба қафо</a></div></div></div></body></html>"""

HISTORY_TEMPLATE = """<!DOCTYPE html><html lang="tj"><head><meta charset="UTF-8"><title>Таърих</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"></head><body class="p-4 bg-light"><div class="container card p-4 shadow-sm"><h4 class="mb-4 fw-bold"><i class="bi bi-clock-history text-primary me-2"></i>Таърихи супоридан</h4><div class="d-flex justify-content-between mb-3"><a href="/dashboard" class="btn btn-primary"><i class="bi bi-arrow-left"></i> Ба қафо</a><a href="/clear_history" class="btn btn-danger" onclick="return confirm('Шумо мутмаин ҳастед?');"><i class="bi bi-trash2-fill"></i> Тоза кардани таърих</a></div><div class="table-responsive"><table class="table table-bordered table-striped align-middle"><thead class="table-dark"><tr><th>Вақт</th><th>Хонанда</th><th>Китобҳо</th><th class="text-center">Амал</th></tr></thead><tbody>{% for r in history %}<tr><td>{{ r.time }}</td><td><b>{{ r.student_name }}</b></td><td>{% for b in r.books %}<div class="badge bg-info text-dark mb-1"><i class="bi bi-book"></i> {{ b.title }} ({{ b.category }})</div><br>{% endfor %}</td><td class="text-center"><a href="/delete_history/{{ r.id }}" class="btn btn-sm btn-outline-danger" onclick="return confirm('Нест кардан мехоҳед?');"><i class="bi bi-x-circle"></i> Нест кардан</a></td></tr>{% endfor %}</tbody></table></div></div></body></html>"""

# ==========================================
# МАРШРУТЫ
# ==========================================

@app.route('/', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('admin_panel') if session.get('role') == 'admin' else url_for('dashboard'))

    error = None
    if request.method == 'POST':
        client_ip = request.remote_addr
        current_time = time.time()

        if client_ip in login_attempts:
            attempts, block_time = login_attempts[client_ip]
            if attempts >= 5 and current_time < block_time:
                return render_template_string(LOGIN_TEMPLATE, error="Хатогӣ: Шумо аз ҳад зиёд кӯшиш кардед. 5 дақиқа интизор шавед.")
            elif current_time >= block_time:
                login_attempts[client_ip] = [0, 0]

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        db = get_db()
        cursor = db.cursor()
        cursor.execute(adapt_query("SELECT password, role, name FROM users WHERE username = ?"), (username,))
        user = cursor.fetchone()

        if user and check_password_hash(user[0], password):
            if client_ip in login_attempts: del login_attempts[client_ip]
            session.clear()
            session['logged_in'] = True
            session['username'] = username
            session['role'] = user[1]
            session['school_name'] = user[2]
            return redirect(url_for('admin_panel')) if user[1] == 'admin' else redirect(url_for('dashboard'))

        if client_ip not in login_attempts: login_attempts[client_ip] = [1, 0]
        else:
            login_attempts[client_ip][0] += 1
            if login_attempts[client_ip][0] >= 5:
                login_attempts[client_ip][1] = current_time + 300
                send_telegram_alert("Unknown", f"🔐 Ошибка входа: {username}", "system_error")

        error = "Логин ё рамз хато аст!"
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/toggle_setting', methods=['POST'])
def toggle_setting():
    if not session.get('logged_in') or session.get('role') != 'admin':
        return jsonify({'success': False})

    data = request.get_json()
    key = data.get('key')
    val = data.get('value')

    valid_keys = ['alert_issue', 'alert_book_error', 'alert_system_error', 'alert_student_error', 'alert_upload_success']
    if key in valid_keys:
        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute(adapt_query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)"), (key, val))
            db.commit()
            return jsonify({'success': True})
        except Exception:
            return jsonify({'success': False})

    return jsonify({'success': False})

@app.route('/update_status', methods=['POST'])
def update_status():
    if not session.get('logged_in'):
        return jsonify({'success': False})
    username = session.get('username')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("INSERT OR REPLACE INTO school_status (username, last_seen) VALUES (?, ?)"),
                   (username, now_str))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin':
        return redirect(url_for('login'))

    msg = None
    err_msg = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_excel' and 'file' in request.files:
            file = request.files.get('file')
            if file and allowed_file(file.filename):
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)
                if size > MAX_FILE_SIZE:
                    err_msg = "Файл калон аст! Максимум 5 МБ."
                    send_telegram_alert("Admin", f"Попытка загрузить файл {size/1024/1024:.1f} МБ (превышает 5 МБ)", "system_error")
                else:
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(filepath)
                    try:
                        ext = filename.rsplit('.', 1)[1].lower()
                        if ext == 'csv':
                            df = pd.read_csv(filepath, dtype=str, encoding='utf-8').fillna('')
                        else:
                            df = pd.read_excel(filepath, header=None, dtype=str).fillna('')

                        db = get_db()
                        cursor = db.cursor()
                        count = 0
                        for _, row in df.iterrows():
                            if len(row) >= 3:
                                new_user = str(row.iloc[0]).strip()
                                new_pass = str(row.iloc[1]).strip()
                                new_name = str(row.iloc[2]).strip()
                                if new_user and new_user.lower() != "nan" and new_pass and new_pass.lower() != "nan":
                                    cursor.execute(adapt_query("SELECT username FROM users WHERE username = ?"), (new_user,))
                                    if not cursor.fetchone():
                                        cursor.execute(adapt_query("INSERT INTO users VALUES (?, ?, ?, ?)"),
                                                       (new_user, generate_password_hash(new_pass), 'school', new_name))
                                        count += 1
                        db.commit()
                        msg = f"Муваффақият! {count} мактаби нав илова шуд."
                        send_telegram_alert("Admin", f"Добавлено {count} школ из {ext} файла", "upload_success")
                    except Exception as e:
                        err_msg = f"Хатогӣ ҳангоми хондани файл: {e}"
                        log_error_and_telegram("Admin", e, "admin_panel_upload")
                    finally:
                        if os.path.exists(filepath):
                            os.remove(filepath)
            else:
                err_msg = "Файли нодуруст! Қабул: .xls, .xlsx, .csv"
                send_telegram_alert("Admin", "Попытка загрузки неверного формата файла", "student_error")

        elif action == 'add_single':
            new_user = request.form.get('new_username')
            new_pass = request.form.get('new_password')
            new_name = request.form.get('new_name')
            if new_user and new_pass and new_name:
                try:
                    db = get_db()
                    cursor = db.cursor()
                    cursor.execute(adapt_query("INSERT INTO users VALUES (?, ?, ?, ?)"),
                                   (new_user.strip(), generate_password_hash(new_pass.strip()), 'school', new_name.strip()))
                    db.commit()
                    msg = f"Мактаб бо муваффақият илова шуд!"
                    send_telegram_alert("Admin", f"Добавлена новая школа: {new_name}", "upload_success")
                except Exception as e:
                    err_msg = "Хатогӣ: Ин логин аллакай вуҷуд дорад!"
                    log_error_and_telegram("Admin", e, "add_single_school")

    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("SELECT username, name, role FROM users"))
    schools = cursor.fetchall()

    cursor.execute(adapt_query("SELECT username, last_seen FROM school_status"))
    status_dict = {row[0]: row[1] for row in cursor.fetchall()}

    schools_with_status = []
    for school in schools:
        last_seen = status_dict.get(school[0], 'Никогда')
        if last_seen != 'Никогда':
            try:
                last_time = datetime.strptime(last_seen, '%Y-%m-%d %H:%M:%S')
                is_online = '🟢 Онлайн' if (datetime.now() - last_time).seconds < 120 else '🔴 Офлайн'
            except:
                is_online = '🔴 Офлайн'
        else:
            is_online = '🔴 Офлайн'
        schools_with_status.append((school[0], school[1], school[2], is_online, last_seen))

    settings_dict = {}
    cursor.execute(adapt_query("SELECT key, value FROM settings"))
    for row in cursor.fetchall():
        settings_dict[row[0]] = row[1]

    return render_template_string(
        ADMIN_TEMPLATE,
        schools=schools_with_status,
        msg=msg,
        err_msg=err_msg,
        alert_issue=settings_dict.get('alert_issue', 'on'),
        alert_book_error=settings_dict.get('alert_book_error', 'on'),
        alert_system_error=settings_dict.get('alert_system_error', 'on'),
        alert_student_error=settings_dict.get('alert_student_error', 'on'),
        alert_upload_success=settings_dict.get('alert_upload_success', 'on')
    )

@app.route('/delete_school/<username>')
def delete_school(username):
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    if username != 'superadmin':
        db = get_db()
        cursor = db.cursor()
        cursor.execute(adapt_query("DELETE FROM users WHERE username = ?"), (username,))
        cursor.execute(adapt_query("DELETE FROM school_status WHERE username = ?"), (username,))
        db.commit()
        send_telegram_alert(session.get('school_name', 'Admin'), f"Удалена школа: {username}", "system_error")
    return redirect(url_for('admin_panel'))

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in') or session.get('role') == 'admin': return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()

    try:
        cursor.execute(adapt_query("SELECT id, student_id, name, grade FROM students WHERE school = ?"), (session.get('school_name'),))
        rows = cursor.fetchall()
        students = [{"id": r[1] if r[1] else r[0], "name": r[2], "grade": r[3]} for r in rows]
    except Exception:
        cursor.execute(adapt_query("SELECT id, name, grade FROM students WHERE school = ?"), (session.get('school_name'),))
        rows = cursor.fetchall()
        students = [{"id": r[0], "name": r[1], "grade": r[2]} for r in rows]

    return render_template_string(DASHBOARD_TEMPLATE, school_name=session.get('school_name'), students=students)

@app.route("/upload_students_page", methods=["GET", "POST"])
def upload_students_page():
    if not session.get("logged_in") or session.get("role") == "admin":
        return redirect(url_for("login"))

    msg = "✅ Ҳамаи хонандагон нест карда шуданд!" if request.args.get("msg") == "cleared" else None

    if request.method == "POST" and "file" in request.files:
        file = request.files.get("file")
        if file and allowed_file(file.filename):
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(0)
            if size > MAX_FILE_SIZE:
                msg = "Файл калон аст! Максимум 5 МБ."
                send_telegram_alert(session.get("school_name"), f"Попытка загрузить файл {size/1024/1024:.1f} МБ (превышает 5 МБ)", "system_error")
            else:
                filename = secure_filename(file.filename)
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file.save(filepath)

                try:
                    ext = filename.rsplit('.', 1)[1].lower()
                    if ext == 'csv':
                        df = pd.read_csv(filepath, dtype=str, encoding='utf-8').fillna("")
                    else:
                        df = pd.read_excel(filepath, dtype=str).fillna("")

                    col_map = {}
                    for col in df.columns:
                        clean_col = str(col).lower().strip()
                        if "id" in clean_col or "идентификатор" in clean_col:
                            col_map["id"] = col
                        elif "фио" in clean_col or "ном" in clean_col or "fio" in clean_col:
                            col_map["fio"] = col
                        elif "синф" in clean_col or "класс" in clean_col or "grade" in clean_col:
                            col_map["grade"] = col

                    if len(col_map) < 3 and len(df.columns) >= 3:
                        col_map = {"id": df.columns[0], "fio": df.columns[1], "grade": df.columns[2]}

                    if "id" not in col_map or "fio" not in col_map or "grade" not in col_map:
                        msg = '❌ Хатогӣ! Формати файл нодуруст. Нужны колонки: "ID", "ФИО", "Синф"'
                        send_telegram_alert(session.get("school_name"), "Ошибка загрузки: отсутствуют колонки", "student_error")
                    else:
                        db = get_db()
                        cursor = db.cursor()
                        school_name = session.get("school_name")
                        count = 0

                        for _, row in df.iterrows():
                            st_id = str(row[col_map["id"]]).strip()
                            st_name = str(row[col_map["fio"]]).strip()
                            st_grade = str(row[col_map["grade"]]).strip()

                            if st_name and st_name.lower() != "nan":
                                cursor.execute(adapt_query("INSERT INTO students (school, student_id, name, grade) VALUES (?, ?, ?, ?)"),
                                               (school_name, st_id, st_name, st_grade))
                                count += 1

                        db.commit()
                        msg = f"✅ {count} хонанда илова шуд!"
                        send_telegram_alert(school_name, f"Загружено {count} учеников из {ext} файла", "upload_success")
                except Exception as e:
                    msg = f"❌ Хатогӣ: {e}"
                    log_error_and_telegram(session.get("school_name"), e, "upload_students")
                finally:
                    if os.path.exists(filepath):
                        os.remove(filepath)

    return render_template_string(UPLOAD_PAGE_TEMPLATE, msg=msg)

@app.route('/clear_students')
def clear_students():
    if not session.get('logged_in') or session.get('role') == 'admin': return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("DELETE FROM students WHERE school = ?"), (session.get('school_name'),))
    db.commit()
    send_telegram_alert(session.get('school_name'), f"Удалены все ученики школы", "system_error")
    return redirect(url_for('upload_students_page', msg='cleared'))

# ==========================================
# ИСПРАВЛЕННАЯ ФУНКЦИЯ ВЫДАЧИ КНИГ (С ЛОГИРОВАНИЕМ)
# ==========================================
@app.route('/issue_books', methods=['POST'])
@limiter.limit("20 per minute")
def issue_books():
    try:
        if not session.get('logged_in'):
            send_telegram_alert("Unknown", "Попытка выдачи книг без авторизации", "system_error")
            return jsonify({'success': False, 'message': 'Ворид !'})

        data = request.get_json() or {}
        student_name = data.get('student_name')
        student_id = data.get('student_id')
        raw_books = data.get('books', [])

        if not student_name or not raw_books:
            send_telegram_alert(session.get('school_name'), f"Неполные данные при выдаче ученику: {student_name}", "student_error")
            return jsonify({'success': False, 'message': 'Маълумот нопурра аст!'})

        # Убираем дубликаты книг
        books = []
        for book in raw_books:
            if not any(b.get('title') == book.get('title') for b in books):
                books.append(book)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        books_json = json.dumps(books, ensure_ascii=False)

        # Сохраняем в локальную историю
        db = get_db()
        cursor = db.cursor()
        cursor.execute(adapt_query("INSERT INTO history (time, school, student_name, books) VALUES (?, ?, ?, ?)"),
                       (now_str, session.get('school_name'), f"{student_name} (ID: {student_id})", books_json))
        db.commit()

        # Отправляем каждую книгу отдельно на основной сайт
        success_count = 0
        error_books = []

        for book in books:
            try:
                payload = {
                    "school": session.get('school_name'),
                    "student_id": student_id,
                    "student_name": student_name,
                    "book": book,
                    "timestamp": now_str
                }

                # Логируем отправку
                logger.info(f"Отправка книги: {book.get('title')} на {MAIN_SITE_BOOKS_URL}/{student_id}/")
                logger.info(f"Payload: {payload}")

                response = requests.post(
                    f"{MAIN_SITE_BOOKS_URL}/{student_id}/",
                    json=payload,
                    timeout=(2.0, 3.0)
                )

                # Логируем ответ сервера
                logger.info(f"Ответ сервера: статус {response.status_code}, тело: {response.text}")

                if response.status_code == 200:
                    success_count += 1
                else:
                    error_books.append(book.get('title', 'Неизвестно'))
                    logger.error(f"Ошибка добавления книги {book.get('title')}: {response.text}")

            except Exception as e:
                logger.error(f"Исключение при отправке книги {book.get('title')}: {e}")
                error_books.append(book.get('title', 'Неизвестно'))
                log_error_and_telegram(session.get('school_name'), e, f"issue_book_single - {book.get('title', '')}")

            time.sleep(0.3)  # задержка между отправками

        # Уведомление в Telegram
        if success_count == len(books):
            send_telegram_alert(
                session.get('school_name'),
                f"✅ Успешно выдано {success_count} книг ученику {student_name} (ID: {student_id})",
                "issue"
            )
        else:
            send_telegram_alert(
                session.get('school_name'),
                f"⚠️ Частичная выдача: {success_count} из {len(books)} книг. Ошибки: {', '.join(error_books)}",
                "book_error"
            )

        target_url = f"{MAIN_SITE_BOOKS_URL}/{student_id}/"
        return jsonify({
            'success': True,
            'message': f'✅ {success_count} аз {len(books)} китоб сабт шуд!',
            'redirect_url': target_url,
            'errors': error_books if error_books else None
        })

    except Exception as e:
        log_error_and_telegram(session.get('school_name', 'Unknown'), e, 'issue_books')
        return jsonify({'success': False, 'message': f'Хатогӣ: {str(e)}'}), 500

@app.route('/history')
def history():
    if not session.get('logged_in') or session.get('role') == 'admin': return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("SELECT id, time, student_name, books FROM history WHERE school = ? ORDER BY id DESC"), (session.get('school_name'),))
    rows = cursor.fetchall()
    history_list = [{'id': r[0], 'time': r[1], 'student_name': r[2], 'books': json.loads(r[3]) if r[3] else []} for r in rows]
    return render_template_string(HISTORY_TEMPLATE, history=history_list)

@app.route('/delete_history/<int:record_id>')
def delete_history(record_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("DELETE FROM history WHERE id = ? AND school = ?"), (record_id, session.get('school_name')))
    db.commit()
    return redirect(url_for('history'))

@app.route('/clear_history')
def clear_history():
    if not session.get('logged_in'): return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute(adapt_query("DELETE FROM history WHERE school = ?"), (session.get('school_name'),))
    db.commit()
    return redirect(url_for('history'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/chat', methods=['POST'])
@limiter.limit("15 per minute")
def chat_api():
    data = request.get_json() or {}
    user_msg = data.get('message', '').strip()
    msg_lower = user_msg.lower()

    if 'excel' in msg_lower or 'база' in msg_lower or 'файл' in msg_lower:
        reply = (
            "<b>📊 Чӣ тавр файли Excel-ро ба база ворид кунем?</b><br><br>"
            "1. Ба менюи болоӣ гузашта, бахши <b>«База»</b>-ро интихоб кунед.<br>"
            "2. Тугмаи <b>«Интихоби файл»</b>-ро пахш карда, файли формати <code>.xlsx</code>, <code>.xls</code> ё <code>.csv</code>-ро интихоб кунед.<br>"
            "3. <b>Муҳим:</b> Сутунҳои файл бояд чунин бошанд:<br>"
            "   • Сутуни 1: <code>ID</code> (Рақами хонанда)<br>"
            "   • Сутуни 2: <code>ФИО</code> (Ном ва насаб)<br>"
            "   • Сутуни 3: <code>Синф</code> (Масалан: 9-А)<br>"
            "4. Тугмаи <b>«Боргузорӣ»</b>-ро пахш кунед. Система ҳамаи хонандагонро худаш сабт мекунад."
        )
    elif 'китоб' in msg_lower or 'скан' in msg_lower or 'супор' in msg_lower:
        reply = (
            "<b>📖 Тартиби пурраи супоридани китоб ба хонанда:</b><br><br>"
            "1. <b>Интихоби хонанда:</b> Аз рӯйхати тарафи чап номи хонандаро ҷустуҷӯ карда, болои он пахш кунед.<br>"
            "2. <b>Фаъол кардани камера:</b> Тугмаи <code>Камераро фаъол кунед</code>-ро пахш кунед.<br>"
            "3. <b>Скан кардани QR:</b> QR-коди паси китобро ба камера наздик кунед, то сигнал (бип) диҳад.<br>"
            "4. <b>Интихоби категория:</b> Категорияи иҷораро :Соли якуми иҷора (34% - Нав), соли дуюми иҷора (33%), соли сеюми иҷора (33%), соли чоруми иҷора (16.5%), соли панҷум ва минбаъда (8.25%) ё Яклухт (100%) интихоб карда, <i>«Илова кардан»</i>-ро пахш кунед.<br>"
            "5. <b>Тасдиқ:</b> Агар ҳамаи китобҳо илова шуда бошанд, тугмаи сабзи <code>Тасдиқ ва супоридан</code>-ро пахш кунед."
        )
    elif 'камера' in msg_lower or 'хато' in msg_lower or 'кор намекунад' in msg_lower:
        reply = (
            "<b>📷 Агар камера фаъол нашавад ё хатогӣ диҳад:</b><br><br>"
            "1. <b>Иҷозати браузер:</b> Тафтиш кунед, ки браузер ба истифодаи камера иҷозат додааст (дар назди адреси сайт аломати қулф 🔒-ро пахш кунед).<br>"
            "2. <b>Пайвастшавӣ:</b> Камера танҳо тавассути пайвасти бехатар (<code>https://</code> ё <code>localhost</code>) кор мекунад.<br>"
            "3. <b>Аз нав бор кардан:</b> Саҳифаро аз нав бор кунед (F5 ё Refresh) ва аз нав кӯшиш кунед.<br>"
            "4. Тафтиш кунед, ки камера дар дигар барномаҳо машғул набошад."
        )
    elif 'тамос' in msg_lower or 'админ' in msg_lower or 'дастгирӣ' in msg_lower or 'связь' in msg_lower:
        reply = (
            "<b>👨‍💻 Маркази дастгирии техникӣ ва таҳиягар:</b><br><br>"
            "Агар дар система хатогӣ дидед ё саволе пайдо шуд, метавонед мустақиман муроҷиат кунед:<br><br>"
            "📱 <b>WhatsApp:</b> <a href='https://wa.me/992929661194' target='_blank'>+992 92 966 1194</a><br>"
            "✈️ <b>Telegram:</b> <a href='https://t.me/DAFERTU' target='_blank'>@DAFERTU</a><br><br>"
            "<i>Мо ҳамеша дар алоқа ҳастем!</i>"
        )
    elif 'таърих' in msg_lower or 'история' in msg_lower:
        reply = (
            "<b>📜 Бахши таърихи супоридани китобҳо:</b><br><br>"
            "Барои дидани рӯйхати пӯрраи китобҳои додашуда, ба бахши <b>«Таърих»</b> дар менюи болоӣ гузаред.<br>"
            "Дар он ҷо метавонед дидед, ки кадом хонанда кадом рӯз китоб гирифтааст ва дар сурати хатогӣ сабтро нест (удалить) кунед."
        )
    else:
        reply = (
            "<b>❓ Саволи шумо қабул шуд.</b><br><br>"
            "Барои гирифтани посухи зуд, яке аз тугмаҳои тайёрро дар поён пахш кунед ё ба <b>Тамос бо таҳиягар</b> муроҷиат намоед."
        )

    return jsonify({'reply': reply})

# ==========================================
# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# ==========================================
@app.errorhandler(Exception)
def handle_exception(e):
    school_name = session.get('school_name', 'Unknown')
    log_error_and_telegram(school_name, e, request.url)

    if request.headers.get('Content-Type') == 'application/json':
        return jsonify({
            'success': False,
            'error': str(e),
            'details': 'Ошибка залогирована и отправлена администратору'
        }), 500

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Хатогӣ!</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="p-4 text-center bg-light">
        <div class="container mt-5">
            <h1 class="text-danger">⚠️ Хатогӣ</h1>
            <p class="lead">Чизе хато шуд! Администратор огоҳ карда шуд.</p>
            <p class="text-muted small">Код: {e.__class__.__name__}</p>
            <a href="/dashboard" class="btn btn-primary mt-3">Ба саҳифаи асосӣ</a>
        </div>
    </body>
    </html>
    """, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
