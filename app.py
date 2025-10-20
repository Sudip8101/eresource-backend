# app.py  — eResources backend (stable)

import os
import sqlite3
import datetime
from flask import Flask, request, jsonify, g, send_from_directory, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from flask_cors import CORS
except ImportError:
    CORS = None


# -------------------------
# Config
# -------------------------
FRONTEND_BASE = os.environ.get("FRONTEND_BASE", "https://eresource.simpletoolspro.com")

app = Flask(__name__)

from threading import Lock

_bootstrap_lock = Lock()
app.config.setdefault("BOOTSTRAPPED", False)

def _bootstrap_once():
    """Run _bootstrap() exactly once per process (Flask 3-safe)."""
    if not app.config["BOOTSTRAPPED"]:
        with _bootstrap_lock:
            if not app.config["BOOTSTRAPPED"]:
                _bootstrap()
                app.config["BOOTSTRAPPED"] = True

@app.before_request
def _ensure_bootstrap():
    # Cheap check; runs _bootstrap() only once
    if not app.config["BOOTSTRAPPED"]:
        _bootstrap_once()


# CORS for your site only, API routes
if CORS is not None:
    CORS(
        app,
        resources={r"/api/*": {"origins": [FRONTEND_BASE]}},
        supports_credentials=False,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        max_age=3600,
    )

@app.after_request
def add_cors_headers(resp):
    """Belts & braces to ensure CORS is on responses to your site."""
    origin = request.headers.get("Origin")
    if origin and origin.startswith(FRONTEND_BASE):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/api/<path:_subpath>", methods=["OPTIONS"])
def _api_preflight(_subpath):
    """Avoid 405 on preflights."""
    origin = request.headers.get("Origin", FRONTEND_BASE)
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# -------------------------
# Uploads
# -------------------------
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route("/uploads/<path:fname>")
def serve_upload(fname):
    return send_from_directory(UPLOAD_DIR, fname, as_attachment=False)


# -------------------------
# Database (keep name: eresource.db)
# -------------------------
DB_PATH = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(__file__), "eresource.db")  # << original filename
)
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# -------------------------
# Schema helpers (idempotent)
# -------------------------
def init_tables():
    """Users, Admins, Logs + seed admin."""
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            enrollment_no TEXT,
            course TEXT,
            semester TEXT,
            mobile TEXT,
            role TEXT DEFAULT 'Student',
            last_seen TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS admins(
            admin_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'Admin',
            created_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS login_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            name TEXT,
            timestamp TEXT,
            ip TEXT
        )
    """)

    # seed one admin if none
    row = db.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
    if not row:
        db.execute(
            "INSERT INTO admins(name,email,password_hash,created_at) VALUES(?,?,?,?)",
            (
                "System Admin",
                "admin@kkhsou.ac.in",
                generate_password_hash("Admin@123"),
                datetime.datetime.now().isoformat()
            )
        )
    db.commit()


def ensure_resources_table():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            type TEXT CHECK(type IN ('pdf','epub','video')) NOT NULL,
            course TEXT NOT NULL,
            tags TEXT,
            link TEXT,
            added_by_email TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def ensure_ratings_notes_tables():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(resource_id, email)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


# -------------------------
# Utility
# -------------------------
def log_login(email: str, name: str):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    get_db().execute(
        "INSERT INTO login_logs (email, name, timestamp, ip) VALUES (?, ?, ?, ?)",
        (email, name, datetime.datetime.now().isoformat(), ip)
    )
    get_db().commit()


# -------------------------
# Routes
# -------------------------
@app.get("/health")
def health():
    return jsonify({"status": "OK"})


# ----- Student APIs -----
@app.get("/api/user")
def get_user_by_email():
    """Fetch user (dashboard) + update last_seen."""
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400

    db = get_db()
    db.execute("UPDATE users SET last_seen=datetime('now') WHERE LOWER(email)=?", (email,))
    db.commit()

    row = db.execute("SELECT * FROM users WHERE LOWER(email)=?", (email,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    return jsonify(dict(row))


@app.post("/api/link-enrollment")
def link_enrollment():
    """Save new student info."""
    data = request.get_json(force=True)
    required = ["email", "name", "enrollment_no", "course", "semester"]
    if not all(data.get(f) for f in required):
        return jsonify({"error": "Missing required fields"}), 400

    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO users (name,email,enrollment_no,course,semester,mobile,role)
        VALUES (?,?,?,?,?,?, 'Student')
    """, (
        data["name"].strip(),
        data["email"].strip().lower(),
        data["enrollment_no"].strip(),
        data["course"].strip(),
        data["semester"].strip(),
        (data.get("mobile") or "").strip()
    ))
    db.commit()
    return jsonify({"message": "Enrollment linked successfully"})


@app.post("/api/login-email")
def login_email():
    """Password-less student login (by email)."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400

    row = get_db().execute(
        "SELECT name, email, course, semester FROM users WHERE LOWER(email)=?",
        (email,)
    ).fetchone()

    if row:
        log_login(email, row["name"])
        return jsonify({
            "status": "existing",
            "name": row["name"],
            "redirect": f"/dashboard.html?email={email}"
        })

    log_login(email, "Unknown")
    return jsonify({
        "status": "new",
        "redirect": f"/ui_07_enrollment_linking_post_google_sign_in.html?email={email}"
    })


# ----- Admin login -----
@app.post("/api/admin-login")
def admin_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "")
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    row = get_db().execute(
        "SELECT admin_id, name, email, password_hash, role FROM admins WHERE LOWER(email)=?",
        (email,)
    ).fetchone()

    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    log_login(email, row["name"])
    return jsonify({
        "status": "ok",
        "name": row["name"],
        "role": row["role"],
        "redirect": "/admin.html"
    })


# ----- Course/Resources (student) -----
@app.get("/api/courses")
def list_courses():
    email = (request.args.get("email") or "").strip().lower()

    primary = None
    if email:
        row = get_db().execute("SELECT course FROM users WHERE LOWER(email)=?", (email,)).fetchone()
        if row and row["course"]:
            primary = row["course"]

    seed = [primary, "Web Development", "Image Processing", "M.Sc. (IT)"]
    seen, courses = set(), []
    for c in seed:
        if c and c not in seen:
            seen.add(c)
            courses.append(c)
    return jsonify({"courses": courses})


@app.get("/api/resources")
def list_resources():
    course = (request.args.get("course") or "").strip()
    conn = get_conn()
    sql = """
      SELECT id, title, type, course, tags, link, created_at
      FROM resources
      {where}
      ORDER BY id DESC
      LIMIT 300
    """
    where = "WHERE course = ?" if course else ""
    rows = conn.execute(sql.format(where=where), (course,) if course else ()).fetchall()
    conn.close()

    out = []
    for r in rows:
        item = dict(r)
        item["type"] = (item.get("type") or "").upper()
        item["tags"] = [t.strip() for t in (item.get("tags") or "").split(",") if t.strip()]
        out.append(item)
    return jsonify({"resources": out})


# ----- Admin APIs -----
@app.get("/api/admin/summary")
def admin_summary():
    conn = get_conn()
    c = conn.cursor()
    users_count = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    resources_count = c.execute("SELECT COUNT(*) AS n FROM resources").fetchone()["n"]
    courses = [r["course"] for r in c.execute("""
        SELECT course FROM users WHERE course IS NOT NULL AND TRIM(course)!=''
        UNION
        SELECT course FROM resources WHERE course IS NOT NULL AND TRIM(course)!=''
        ORDER BY course
    """).fetchall()]
    latest_users = c.execute("""
        SELECT user_id, name, email, course, semester
        FROM users ORDER BY user_id DESC LIMIT 5
    """).fetchall()
    latest_resources = c.execute("""
        SELECT id, title, type, course, tags, created_at
        FROM resources ORDER BY id DESC LIMIT 5
    """).fetchall()
    conn.close()
    return jsonify({
        "totals": {
            "users": users_count,
            "resources": resources_count,
            "courses": len(courses)
        },
        "courses": courses,
        "latest_users": [dict(r) for r in latest_users],
        "latest_resources": [dict(r) for r in latest_resources]
    })


@app.get("/api/admin/users")
def admin_users():
    q = (request.args.get("q") or "").strip().lower()
    conn = get_conn()
    c = conn.cursor()
    if q:
        rows = c.execute("""
            SELECT user_id, name, email, course, semester, mobile
            FROM users
            WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR lower(course) LIKE ?
            ORDER BY user_id DESC LIMIT 200
        """, (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = c.execute("""
            SELECT user_id, name, email, course, semester, mobile
            FROM users ORDER BY user_id DESC LIMIT 200
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/admin/resources")
def admin_resources():
    course = (request.args.get("course") or "").strip()
    rtype = (request.args.get("type") or "").strip().lower()
    conn = get_conn()
    c = conn.cursor()
    sql = "SELECT id, title, type, course, tags, link, created_at FROM resources WHERE 1=1"
    params = []
    if course:
        sql += " AND course = ?"
        params.append(course)
    if rtype in ("pdf", "epub", "video"):
        sql += " AND lower(type) = ?"
        params.append(rtype)
    sql += " ORDER BY id DESC LIMIT 300"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/resources")
def admin_add_resource():
    data = request.get_json(force=True) or {}
    title   = (data.get("title")  or "").strip()
    rtype   = (data.get("type")   or "").strip().lower()
    course  = (data.get("course") or "").strip()
    link    = (data.get("link")   or "").strip()
    added_by = (data.get("added_by_email") or "").strip().lower()

    # tags can be list or comma string
    tags_in = data.get("tags") or ""
    if isinstance(tags_in, list):
        tags = ",".join(t.strip() for t in tags_in if t and t.strip())
    else:
        tags = ",".join(t.strip() for t in str(tags_in).split(",") if t.strip())

    # normalize relative /uploads/* into absolute URL on this backend
    if link and (link.startswith("/uploads/") or link.startswith("uploads/")):
        fname = link.split("/uploads/")[-1]
        link  = url_for("serve_upload", fname=fname, _external=True)

    if not title or rtype not in ("pdf", "epub", "video") or not course:
        return jsonify({"error": "title, type(pdf|epub|video) and course are required"}), 400

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO resources(title, type, course, tags, link, added_by_email)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (title, rtype, course, tags, link, added_by))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": rid})


@app.post("/api/admin/upload")
def admin_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    safe_name = f.filename.replace("..","").replace("/","_").replace("\\","_")
    path = os.path.join(UPLOAD_DIR, safe_name)
    f.save(path)
    file_url = url_for("serve_upload", fname=safe_name, _external=True)
    return jsonify({"url": file_url})


@app.get("/api/admin/resources/recent")
def admin_recent_resources():
    conn = get_conn()
    rows = conn.execute("""
      SELECT id, title, type, course, tags, created_at
      FROM resources ORDER BY id DESC LIMIT 50
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/admin/resources/all")
def admin_all_resources():
    conn = get_conn()
    rows = conn.execute("""
      SELECT id, title, type, course, tags, created_at
      FROM resources ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.delete("/api/admin/resources/<int:rid>")
def admin_delete_resource(rid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM resources WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/api/admin/stats")
def admin_stats():
    conn = get_conn()
    c = conn.cursor()
    total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_resources = c.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    courses = c.execute("SELECT COUNT(DISTINCT course) FROM resources").fetchone()[0]
    online_now = c.execute("""
        SELECT COUNT(*) FROM users
        WHERE last_seen IS NOT NULL
          AND last_seen >= datetime('now','-5 minutes')
    """).fetchone()[0]
    conn.close()
    return jsonify({
        "total_users": total_users or 0,
        "total_resources": total_resources or 0,
        "courses": courses or 0,
        "online_now": online_now or 0
    })



# -------------------------
# Bootstrap schema on start (Flask 3)
# -------------------------
# -----------------------
# Bootstrap schema on start (Flask 3-compatible)
# -----------------------
def _bootstrap():
    print(f"Bootstrapping DB at: {DB_PATH}")
    init_tables()
    ensure_resources_table()
    ensure_ratings_notes_tables()
    print("Schema ready.")



# -------------------------
# Local dev
# -------------------------
if __name__ == "__main__":
    with app.app_context():
        _bootstrap_once()
    app.run(debug=True, host="0.0.0.0", port=5000)


