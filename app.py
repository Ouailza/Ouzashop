"""
متجر ألعاب إلكتروني (حسابات + شحن) — Flask + SQLite
تشغيل محلي:  python app.py
الإنتاج:      gunicorn app:app
"""
import getpass
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# ───────────────────────── الإعدادات ─────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "store.db")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


STORE_NAME = os.environ.get("STORE_NAME", "متجر الألعاب")
COUNTRY_CODE = re.sub(r"\D", "", os.environ.get("DEFAULT_COUNTRY_CODE", "213"))

KINDS = ("account", "topup")          # حساب / شحن
CURRENCIES = ("DZD", "USD")
STATUSES = ("new", "contacted", "completed", "cancelled")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_EXTRA_IMAGES = 10

try:
    from zoneinfo import ZoneInfo

    SITE_TZ = ZoneInfo(os.environ.get("SITE_TZ", "Africa/Algiers"))
except Exception:  # لا توجد قاعدة بيانات المناطق الزمنية على الجهاز
    SITE_TZ = timezone(timedelta(hours=1))


def load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path="")
app.config.update(
    SECRET_KEY=load_secret_key(),
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_flag("COOKIE_SECURE"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SEND_FILE_MAX_AGE_DEFAULT=300,
)
app.json.ensure_ascii = False
if env_flag("BEHIND_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ───────────────────────── قاعدة البيانات ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  username_lc TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'account',
  category TEXT NOT NULL DEFAULT '',
  price REAL NOT NULL,
  currency TEXT NOT NULL DEFAULT 'DZD',
  notes TEXT NOT NULL DEFAULT '',
  main_image TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  product_name TEXT NOT NULL,
  product_price REAL NOT NULL,
  product_currency TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  buyer_name TEXT NOT NULL,
  contact_type TEXT NOT NULL,
  contact TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active, id);
CREATE INDEX IF NOT EXISTS idx_images_product ON product_images(product_id, position);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ───────────────────────── أدوات عامة ─────────────────────────
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(error=e.message), e.status


@app.errorhandler(413)
def handle_too_large(_e):
    return jsonify(error="حجم الملفات كبير جدًا (الحد الأقصى 40 ميغابايت للطلب الواحد)"), 413


@app.errorhandler(404)
def handle_404(_e):
    if request.path.startswith("/api/"):
        return jsonify(error="غير موجود"), 404
    return "الصفحة غير موجودة", 404


@app.errorhandler(405)
def handle_405(_e):
    return jsonify(error="طريقة الطلب غير مسموحة"), 405


@app.errorhandler(500)
def handle_500(_e):
    return jsonify(error="حدث خطأ في الخادم، حاول مرة أخرى"), 500


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_local_str(iso_str):
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(SITE_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def today_bounds_utc():
    """بداية ونهاية «اليوم» بتوقيت المتجر (الجزائر افتراضيًا)، مُحوَّلة إلى UTC."""
    start_local = datetime.now(SITE_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return iso(start_local), iso(end_local)


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_field(value, label, min_len, max_len):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(f"{label}: قيمة غير صالحة")
    value = _CTRL_RE.sub("", value).strip()
    if len(value) < min_len:
        raise ApiError(f"{label} مطلوب" if min_len == 1 else f"{label} قصير جدًا (الحد الأدنى {min_len} أحرف)")
    if len(value) > max_len:
        raise ApiError(f"{label} طويل جدًا (الحد الأقصى {max_len} حرفًا)")
    return value


USERNAME_RE = re.compile(r"^[\w.\-]{3,24}$")
PHONE_RE = re.compile(r"^\+?\d{8,15}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,}$")


def clean_phone(value):
    value = re.sub(r"[\s\-().]", "", value or "")
    if not PHONE_RE.match(value):
        raise ApiError("رقم الهاتف غير صالح (أرقام فقط، من 8 إلى 15 رقمًا)")
    return value


def wa_link(contact):
    digits = re.sub(r"\D", "", contact or "")
    if not digits:
        return None
    if contact.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and COUNTRY_CODE:
        digits = COUNTRY_CODE + digits[1:]
    return f"https://wa.me/{digits}"


# ───────────────────────── تحديد المحاولات ─────────────────────────
_attempts = {}


def _recent(key, window):
    now = time.time()
    items = [t for t in _attempts.get(key, []) if now - t < window]
    if items:
        _attempts[key] = items
    else:
        _attempts.pop(key, None)
    return items


def is_limited(key, limit, window):
    return len(_recent(key, window)) >= limit


def record_attempt(key):
    if len(_attempts) > 5000:  # تنظيف دوري حتى لا تكبر الذاكرة
        for k in list(_attempts):
            _recent(k, 3600)
    _attempts.setdefault(key, []).append(time.time())


def client_ip():
    return request.remote_addr or "unknown"


# ───────────────────────── الجلسات و CSRF ─────────────────────────
def csrf_token():
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        sent = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            raise ApiError("انتهت صلاحية الجلسة، أعد تحميل الصفحة", 403)


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def current_user():
    if "_user" not in g:
        uid = session.get("uid")
        g._user = (
            get_db().execute("SELECT id, username, phone, role FROM users WHERE id = ?", (uid,)).fetchone()
            if uid
            else None
        )
    return g._user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            raise ApiError("يجب تسجيل الدخول أولًا", 401)
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            raise ApiError("يجب تسجيل الدخول أولًا", 401)
        if user["role"] != "admin":
            raise ApiError("هذه الصفحة للأدمن فقط", 403)
        return fn(*args, **kwargs)

    return wrapper


def public_user(row):
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "phone": row["phone"], "role": row["role"]}


def start_session(uid):
    session.clear()
    session.permanent = True
    session["uid"] = uid
    session["csrf"] = secrets.token_urlsafe(32)
    g.pop("_user", None)


DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


# ───────────────────────── الحسابات ─────────────────────────
@app.get("/api/me")
def api_me():
    return jsonify(user=public_user(current_user()), csrf=csrf_token(), store={"name": STORE_NAME})


@app.post("/api/register")
def api_register():
    ip = client_ip()
    if is_limited(("register", ip), 10, 3600):
        raise ApiError("محاولات كثيرة، حاول بعد قليل", 429)
    data = json_body()
    username = text_field(data.get("username"), "اسم المستخدم", 3, 24)
    if not USERNAME_RE.match(username):
        raise ApiError("اسم المستخدم: حروف وأرقام ونقطة وشرطة فقط، من 3 إلى 24 حرفًا")
    phone = clean_phone(data.get("phone"))
    password = data.get("password")
    if not isinstance(password, str) or not (8 <= len(password) <= 128):
        raise ApiError("كلمة المرور يجب أن تكون من 8 إلى 128 حرفًا")

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, username_lc, phone, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?, 'user', ?)",
            (username, username.lower(), phone, generate_password_hash(password), iso(utc_now())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError("اسم المستخدم مستخدم من قبل، اختر اسمًا آخر", 409)
    record_attempt(("register", ip))
    start_session(cur.lastrowid)
    return jsonify(user=public_user(current_user()), csrf=session["csrf"]), 201


@app.post("/api/login")
def api_login():
    ip = client_ip()
    data = json_body()
    username = data.get("username") if isinstance(data.get("username"), str) else ""
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    username_lc = username.strip().lower()[:64]
    key_user = ("login", ip, username_lc)
    key_ip = ("login-ip", ip)
    if is_limited(key_user, 8, 600) or is_limited(key_ip, 40, 600):
        raise ApiError("محاولات كثيرة، انتظر 10 دقائق ثم حاول مجددًا", 429)

    row = get_db().execute("SELECT * FROM users WHERE username_lc = ?", (username_lc,)).fetchone()
    valid = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password[:128])
    if not (row and valid):
        record_attempt(key_user)
        record_attempt(key_ip)
        raise ApiError("اسم المستخدم أو كلمة المرور غير صحيحة", 401)

    _attempts.pop(key_user, None)
    start_session(row["id"])
    return jsonify(user=public_user(current_user()), csrf=session["csrf"])


@app.post("/api/logout")
def api_logout():
    session.clear()
    g.pop("_user", None)
    return jsonify(ok=True, csrf=csrf_token())


# ───────────────────────── المنتجات (للعملاء) ─────────────────────────
def media_url(filename):
    return f"/media/{filename}"


def num(value):
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


def images_for(db, product_ids):
    result = {pid: [] for pid in product_ids}
    if not product_ids:
        return result
    marks = ",".join("?" * len(product_ids))
    rows = db.execute(
        f"SELECT id, product_id, filename FROM product_images WHERE product_id IN ({marks}) "
        "ORDER BY position, id",
        list(product_ids),
    ).fetchall()
    for r in rows:
        result[r["product_id"]].append(r)
    return result


def product_dict(row, imgs, admin=False):
    d = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "category": row["category"],
        "price": num(row["price"]),
        "currency": row["currency"],
        "notes": row["notes"],
        "main_image": media_url(row["main_image"]),
    }
    if admin:
        d["is_active"] = bool(row["is_active"])
        d["images"] = [{"id": i["id"], "url": media_url(i["filename"])} for i in imgs]
    else:
        d["images"] = [media_url(i["filename"]) for i in imgs]
    return d


def int_arg(name, default=None, low=None, high=None):
    try:
        value = int(request.args.get(name, ""))
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


@app.get("/api/products")
@login_required
def api_products():
    db = get_db()
    limit = int_arg("limit", 8, 1, 30)
    before = int_arg("before", None, 1)
    kind = request.args.get("kind", "")
    category = request.args.get("category", "")[:40]

    where, params = ["is_active = 1"], []
    if kind in KINDS:
        where.append("kind = ?")
        params.append(kind)
    if category:
        where.append("category = ?")
        params.append(category)
    if before:
        where.append("id < ?")
        params.append(before)

    rows = db.execute(
        f"SELECT * FROM products WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        params + [limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    imgs = images_for(db, [r["id"] for r in rows])
    out = {
        "items": [product_dict(r, imgs[r["id"]]) for r in rows],
        "has_more": has_more,
        "next_before": rows[-1]["id"] if rows else None,
    }
    if before is None:
        cat_where, cat_params = ["is_active = 1", "category != ''"], []
        if kind in KINDS:
            cat_where.append("kind = ?")
            cat_params.append(kind)
        out["categories"] = [
            r["category"]
            for r in db.execute(
                f"SELECT DISTINCT category FROM products WHERE {' AND '.join(cat_where)} ORDER BY category",
                cat_params,
            )
        ]
    return jsonify(out)


# ───────────────────────── الطلبات (للعملاء) ─────────────────────────
@app.post("/api/orders")
@login_required
def api_create_order():
    user = current_user()
    if is_limited(("order", user["id"]), 10, 3600):
        raise ApiError("أرسلت طلبات كثيرة، حاول لاحقًا", 429)
    data = json_body()
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        raise ApiError("المنتج غير صالح")
    name = text_field(data.get("name"), "الاسم", 2, 60)
    contact_type = data.get("contact_type")
    if contact_type == "whatsapp":
        contact = clean_phone(data.get("contact"))
    elif contact_type == "email":
        contact = text_field(data.get("contact"), "البريد الإلكتروني", 5, 254)
        if not EMAIL_RE.match(contact):
            raise ApiError("البريد الإلكتروني غير صالح")
    else:
        raise ApiError("اختر طريقة التواصل: واتساب أو بريد إلكتروني")

    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ? AND is_active = 1", (product_id,)).fetchone()
    if product is None:
        raise ApiError("هذا المنتج لم يعد متاحًا", 404)
    cur = db.execute(
        "INSERT INTO orders (product_id, product_name, product_price, product_currency, user_id, "
        "buyer_name, contact_type, contact, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
        (product["id"], product["name"], product["price"], product["currency"], user["id"],
         name, contact_type, contact, iso(utc_now())),
    )
    db.commit()
    record_attempt(("order", user["id"]))
    where = "على الواتساب" if contact_type == "whatsapp" else "عبر بريدك الإلكتروني"
    return jsonify(ok=True, order_id=cur.lastrowid, message=f"سنتواصل معك قريبًا {where} لإتمام الطلب."), 201


# ───────────────────────── الصور ─────────────────────────
def sniff_image(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def remove_media(filename):
    if filename and re.match(r"^[0-9a-f]{32}\.(jpg|png|webp|gif)$", filename):
        try:
            os.remove(os.path.join(UPLOAD_DIR, filename))
        except OSError:
            pass


def store_upload(fs):
    head = fs.stream.read(16)
    fs.stream.seek(0)
    ext = sniff_image(head)
    if ext is None:
        raise ApiError("أحد الملفات ليس صورة صالحة (المسموح: JPG, PNG, WEBP, GIF)")
    name = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    fs.save(path)
    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        os.remove(path)
        raise ApiError("حجم إحدى الصور كبير (الحد الأقصى 8 ميغابايت للصورة)")
    return name


def save_files(files):
    saved = []
    try:
        for fs in files:
            saved.append(store_upload(fs))
    except Exception:
        for n in saved:
            remove_media(n)
        raise
    return saved


def uploaded(field):
    return [f for f in request.files.getlist(field) if f and f.filename]


# ───────────────────────── لوحة الأدمن ─────────────────────────
def parse_product_form():
    f = request.form
    name = text_field(f.get("name"), "اسم المنتج", 2, 80)
    kind = f.get("kind", "account")
    if kind not in KINDS:
        raise ApiError("نوع المنتج غير صالح")
    category = text_field(f.get("category"), "اللعبة", 0, 40)
    try:
        price = round(float((f.get("price") or "").replace(",", ".")), 2)
    except ValueError:
        raise ApiError("السعر غير صالح")
    if not (0 < price < 100_000_000):
        raise ApiError("السعر يجب أن يكون أكبر من صفر")
    currency = f.get("currency", "DZD")
    if currency not in CURRENCIES:
        raise ApiError("العملة غير صالحة")
    notes = text_field(f.get("notes"), "الملاحظات", 0, 3000)
    return dict(name=name, kind=kind, category=category, price=price, currency=currency, notes=notes)


@app.get("/api/admin/stats")
@admin_required
def admin_stats():
    db = get_db()
    start, end = today_bounds_utc()
    today_total = db.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND created_at < ?", (start, end)
    ).fetchone()[0]
    new_total = db.execute("SELECT COUNT(*) FROM orders WHERE status = 'new'").fetchone()[0]
    prod = db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active FROM products").fetchone()
    return jsonify(today_orders=today_total, new_orders=new_total,
                   products_total=prod["total"], products_active=prod["active"])


@app.get("/api/admin/orders")
@admin_required
def admin_orders():
    scope = request.args.get("scope", "today")
    status = request.args.get("status", "")
    before = int_arg("before", None, 1)
    limit = int_arg("limit", 30, 1, 100)
    where, params = ["1 = 1"], []
    if scope == "today":
        start, end = today_bounds_utc()
        where.append("o.created_at >= ? AND o.created_at < ?")
        params += [start, end]
    if status in STATUSES:
        where.append("o.status = ?")
        params.append(status)
    if before:
        where.append("o.id < ?")
        params.append(before)
    rows = get_db().execute(
        "SELECT o.*, u.username FROM orders o LEFT JOIN users u ON u.id = o.user_id "
        f"WHERE {' AND '.join(where)} ORDER BY o.id DESC LIMIT ?",
        params + [limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": r["id"],
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "price": num(r["product_price"]),
            "currency": r["product_currency"],
            "buyer_name": r["buyer_name"],
            "contact_type": r["contact_type"],
            "contact": r["contact"],
            "wa_link": wa_link(r["contact"]) if r["contact_type"] == "whatsapp" else None,
            "status": r["status"],
            "created_at": to_local_str(r["created_at"]),
            "username": r["username"],
        }
        for r in rows
    ]
    return jsonify(items=items, has_more=has_more, next_before=rows[-1]["id"] if rows else None)


@app.post("/api/admin/orders/<int:order_id>/status")
@admin_required
def admin_order_status(order_id):
    status = json_body().get("status")
    if status not in STATUSES:
        raise ApiError("حالة غير صالحة")
    db = get_db()
    cur = db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    db.commit()
    if cur.rowcount == 0:
        raise ApiError("الطلب غير موجود", 404)
    return jsonify(ok=True)


@app.get("/api/admin/products")
@admin_required
def admin_products():
    db = get_db()
    before = int_arg("before", None, 1)
    limit = int_arg("limit", 30, 1, 100)
    where, params = ["1 = 1"], []
    if before:
        where.append("id < ?")
        params.append(before)
    rows = db.execute(
        f"SELECT * FROM products WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params + [limit + 1]
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    imgs = images_for(db, [r["id"] for r in rows])
    return jsonify(
        items=[product_dict(r, imgs[r["id"]], admin=True) for r in rows],
        has_more=has_more,
        next_before=rows[-1]["id"] if rows else None,
        categories=[r["category"] for r in db.execute(
            "SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category")],
    )


def fetch_admin_product(db, pid):
    row = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise ApiError("المنتج غير موجود", 404)
    return row


@app.post("/api/admin/products")
@admin_required
def admin_create_product():
    fields = parse_product_form()
    mains = uploaded("main_image")
    if not mains:
        raise ApiError("الصورة الرئيسية مطلوبة")
    extras = uploaded("images")
    if len(extras) > MAX_EXTRA_IMAGES:
        raise ApiError(f"الحد الأقصى {MAX_EXTRA_IMAGES} صور إضافية")
    saved = save_files([mains[0]] + extras)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO products (name, kind, category, price, currency, notes, main_image, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (fields["name"], fields["kind"], fields["category"], fields["price"], fields["currency"],
             fields["notes"], saved[0], iso(utc_now())),
        )
        pid = cur.lastrowid
        for pos, fn in enumerate(saved[1:]):
            db.execute("INSERT INTO product_images (product_id, filename, position) VALUES (?, ?, ?)", (pid, fn, pos))
        db.commit()
    except Exception:
        db.rollback()
        for fn in saved:
            remove_media(fn)
        raise
    row = fetch_admin_product(db, pid)
    return jsonify(product=product_dict(row, images_for(db, [pid])[pid], admin=True)), 201


@app.put("/api/admin/products/<int:pid>")
@admin_required
def admin_update_product(pid):
    db = get_db()
    row = fetch_admin_product(db, pid)
    fields = parse_product_form()
    new_main = uploaded("main_image")
    extras = uploaded("images")
    remove_ids = {int(x) for x in request.form.getlist("remove_images") if x.isdigit()}

    existing = db.execute("SELECT id, filename FROM product_images WHERE product_id = ?", (pid,)).fetchall()
    removed = [e for e in existing if e["id"] in remove_ids]
    if len(existing) - len(removed) + len(extras) > MAX_EXTRA_IMAGES:
        raise ApiError(f"الحد الأقصى {MAX_EXTRA_IMAGES} صور إضافية")

    saved = save_files(new_main[:1] + extras)
    new_main_name = saved[0] if new_main else None
    new_extra_names = saved[1:] if new_main else saved
    try:
        db.execute(
            "UPDATE products SET name=?, kind=?, category=?, price=?, currency=?, notes=?, main_image=? WHERE id=?",
            (fields["name"], fields["kind"], fields["category"], fields["price"], fields["currency"],
             fields["notes"], new_main_name or row["main_image"], pid),
        )
        for e in removed:
            db.execute("DELETE FROM product_images WHERE id = ?", (e["id"],))
        pos = db.execute("SELECT COALESCE(MAX(position), -1) FROM product_images WHERE product_id = ?", (pid,)).fetchone()[0]
        for fn in new_extra_names:
            pos += 1
            db.execute("INSERT INTO product_images (product_id, filename, position) VALUES (?, ?, ?)", (pid, fn, pos))
        db.commit()
    except Exception:
        db.rollback()
        for fn in saved:
            remove_media(fn)
        raise
    if new_main_name:
        remove_media(row["main_image"])
    for e in removed:
        remove_media(e["filename"])
    row = fetch_admin_product(db, pid)
    return jsonify(product=product_dict(row, images_for(db, [pid])[pid], admin=True))


@app.post("/api/admin/products/<int:pid>/visibility")
@admin_required
def admin_product_visibility(pid):
    active = 1 if json_body().get("active") else 0
    db = get_db()
    cur = db.execute("UPDATE products SET is_active = ? WHERE id = ?", (active, pid))
    db.commit()
    if cur.rowcount == 0:
        raise ApiError("المنتج غير موجود", 404)
    return jsonify(ok=True, is_active=bool(active))


@app.delete("/api/admin/products/<int:pid>")
@admin_required
def admin_delete_product(pid):
    db = get_db()
    row = fetch_admin_product(db, pid)
    files = [row["main_image"]] + [r["filename"] for r in db.execute(
        "SELECT filename FROM product_images WHERE product_id = ?", (pid,))]
    db.execute("DELETE FROM products WHERE id = ?", (pid,))
    db.commit()
    for fn in files:
        remove_media(fn)
    return jsonify(ok=True)


# ───────────────────────── الصفحات والملفات ─────────────────────────
@app.get("/")
def index():
    resp = send_from_directory(PUBLIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/media/<path:filename>")
def media(filename):
    if not re.match(r"^[0-9a-f]{32}\.(jpg|png|webp|gif)$", filename):
        return "", 404
    resp = send_from_directory(UPLOAD_DIR, filename, max_age=31536000)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ───────────────────────── حساب الأدمن ─────────────────────────
def upsert_admin(username, password):
    """ينشئ حساب الأدمن أو يحدّث كلمة مروره."""
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError("invalid admin username (3-24 letters/digits/._-)")
    if len(password) < 8:
        raise ValueError("admin password must be at least 8 characters")
    conn = connect()
    try:
        row = conn.execute("SELECT id, password_hash, role FROM users WHERE username_lc = ?", (username.lower(),)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, username_lc, phone, password_hash, role, created_at) "
                "VALUES (?, ?, '', ?, 'admin', ?)",
                (username, username.lower(), generate_password_hash(password), iso(utc_now())),
            )
            conn.commit()
            return "created"
        if row["role"] != "admin" or not check_password_hash(row["password_hash"], password):
            conn.execute("UPDATE users SET role='admin', password_hash=? WHERE id=?", (generate_password_hash(password), row["id"]))
            conn.commit()
            return "updated"
        return "unchanged"
    finally:
        conn.close()


def ensure_admin():
    env_user = os.environ.get("ADMIN_USERNAME", "").strip()
    env_pass = os.environ.get("ADMIN_PASSWORD", "")
    if env_user and env_pass:
        try:
            result = upsert_admin(env_user, env_pass)
            if result != "unchanged":
                print(f"[store] admin account '{env_user}' {result} from environment variables")
        except ValueError as e:
            print(f"[store] WARNING: ADMIN_USERNAME/ADMIN_PASSWORD ignored: {e}")
        return
    conn = connect()
    try:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    finally:
        conn.close()
    if has_admin:
        return
    password = secrets.token_urlsafe(12)
    try:
        upsert_admin("admin", password)
    except sqlite3.IntegrityError:
        return  # عملية أخرى أنشأته للتو
    print("=" * 60)
    print("[store] First run: an admin account was created.")
    print("[store]   username: admin")
    print(f"[store]   password: {password}")
    print("[store] Set ADMIN_USERNAME / ADMIN_PASSWORD env vars to choose your own.")
    print("=" * 60)


init_db()
ensure_admin()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        user = input("Admin username: ").strip()
        pw = getpass.getpass("Admin password (8+ chars): ")
        print("Done:", upsert_admin(user, pw))
    else:
        app.run(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=env_flag("FLASK_DEBUG"),
        )
