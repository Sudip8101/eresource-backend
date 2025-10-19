from flask import Flask, request, jsonify, g
from flask_cors import CORS
import sqlite3, datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask import send_from_directory, g  
import os
from flask import url_for

# Auto-create folders if missing
os.makedirs("uploads", exist_ok=True)
os.makedirs("data", exist_ok=True)

app = Flask(__name__)

# --- File Upload Setup ---
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route("/uploads/<path:fname>")
def serve_upload(fname):
    return send_from_directory(UPLOAD_DIR, fname, as_attachment=False)

CORS(app)

DB_PATH = "eresources.db"

# --- DB helper (add once) ---
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Admin helpers (add near the top of app.py) ---


def ensure_resources_table():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            type TEXT CHECK(type IN ('pdf','epub','video')) NOT NULL,
            course TEXT NOT NULL,
            tags TEXT,                 -- comma-separated tags
            link TEXT,                 -- URL or local path
            added_by_email TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


# call this once at startup (e.g., right after your other init calls)
ensure_resources_table()

def ensure_user_last_seen_column():
    db = get_db()
    cols = [r["name"] for r in db.execute("PRAGMA table_info(users)") .fetchall()]
    if "last_seen" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
        db.commit()



def ensure_ratings_notes_tables():
    conn = get_conn()
    c = conn.cursor()
    # ratings: one row per (resource_id, email)
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
    # notes: many notes per (resource_id, email)
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

# call once at startup with your other init calls
ensure_ratings_notes_tables()



@app.post("/api/rate")
def api_rate():
    data = request.get_json(force=True) or {}
    rid = int(data.get("resource_id") or 0)
    email = (data.get("email") or "").strip().lower()
    rating = int(data.get("rating") or 0)
    if not (rid and email and 1 <= rating <= 5):
        return jsonify({"error": "resource_id, email, rating(1-5) required"}), 400

    conn = get_conn()
    c = conn.cursor()
    # upsert
    c.execute("""
        INSERT INTO ratings(resource_id, email, rating)
        VALUES(?,?,?)
        ON CONFLICT(resource_id,email) DO UPDATE SET rating=excluded.rating, created_at=datetime('now')
    """, (rid, email, rating))
    conn.commit()

    # return fresh average
    row = c.execute("SELECT ROUND(AVG(rating),2) AS avg, COUNT(*) AS votes FROM ratings WHERE resource_id=?", (rid,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "avg_rating": row["avg"] or 0, "votes": row["votes"] or 0})

@app.get("/api/ratings")
def api_get_ratings():
    rid = request.args.get("resource_id", type=int)
    if not rid:
        return jsonify({"error": "resource_id required"}), 400
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT email, rating, created_at FROM ratings WHERE resource_id=? ORDER BY created_at DESC", (rid,)).fetchall()
    avg = conn.execute("SELECT ROUND(AVG(rating),2) AS avg, COUNT(*) AS votes FROM ratings WHERE resource_id=?", (rid,)).fetchone()
    conn.close()
    return jsonify({"avg": avg["avg"] or 0, "votes": avg["votes"] or 0, "items": [dict(r) for r in rows]})

@app.get("/api/ratings/summary")
def api_ratings_summary():
    # For admin reports: top rated resources
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
      SELECT r.id, r.title, r.course,
             ROUND(AVG(rt.rating),2) AS avg_rating,
             COUNT(rt.id) AS votes
      FROM resources r
      JOIN ratings rt ON rt.resource_id = r.id
      GROUP BY r.id
      HAVING votes >= 1
      ORDER BY avg_rating DESC, votes DESC
      LIMIT 20
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/notes")
def api_add_note():
    data = request.get_json(force=True) or {}
    rid = int(data.get("resource_id") or 0)
    email = (data.get("email") or "").strip().lower()
    text = (data.get("text") or "").strip()
    if not (rid and email and text):
        return jsonify({"error": "resource_id, email, text required"}), 400
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO notes(resource_id,email,text) VALUES(?,?,?)", (rid, email, text))
    conn.commit()
    note_id = c.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": note_id})

@app.get("/api/notes")
def api_list_notes():
    rid = request.args.get("resource_id", type=int)
    email = (request.args.get("email") or "").strip().lower()
    if not (rid and email):
        return jsonify({"error":"resource_id and email required"}), 400
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, text, created_at
        FROM notes
        WHERE resource_id=? AND email=?
        ORDER BY id DESC
    """, (rid, email)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.delete("/api/notes/<int:note_id>")
def api_delete_note(note_id):
    email = (request.args.get("email") or "").strip().lower()  # simple guard
    if not email:
        return jsonify({"error":"email required"}), 400
    conn = get_conn()
    c = conn.cursor()
    # only allow deleting own notes
    c.execute("DELETE FROM notes WHERE id=? AND email=?", (note_id, email))
    conn.commit()
    changed = c.rowcount
    conn.close()
    return jsonify({"ok": bool(changed)})








# ---------------- DB helpers ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db



@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_tables():
    """Create tables if missing + seed default admin."""
    db = get_db()

    # Students/users
    db.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        enrollment_no TEXT,
        course TEXT,
        semester TEXT,
        mobile TEXT,
        role TEXT DEFAULT 'Student'
    )""")

    # Admins
    db.execute("""CREATE TABLE IF NOT EXISTS admins(
        admin_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'Admin',
        created_at TEXT
    )""")

    # Login logs (students & admins)
    db.execute("""CREATE TABLE IF NOT EXISTS login_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        name TEXT,
        timestamp TEXT,
        ip TEXT
    )""")

    # Seed one admin if none exists (CHANGE PASSWORD after first run)
    row = db.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
    if not row:
        db.execute(
            "INSERT INTO admins(name,email,password_hash,created_at) VALUES(?,?,?,?)",
            (
                "System Admin",
                "admin@kkhsou.ac.in",
                generate_password_hash("Admin@123"),  # <-- change after first run
                datetime.datetime.now().isoformat()
            )
        )
    db.commit()

# ---------------- Utility ----------------
def log_login(email: str, name: str):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    get_db().execute(
        "INSERT INTO login_logs (email, name, timestamp, ip) VALUES (?, ?, ?, ?)",
        (email, name, datetime.datetime.now().isoformat(), ip)
    )
    get_db().commit()

# ---------------- Routes ----------------
@app.get("/health")
def health():
    return jsonify({"status": "OK"})

@app.get("/api/user")
def get_user_by_email():
    """Fetch user details by email (used by dashboard) + update last_seen."""
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400

    db = get_db()
    # ⏰ Update last_seen timestamp every time the student fetches dashboard
    db.execute("UPDATE users SET last_seen=datetime('now') WHERE LOWER(email)=?", (email,))
    db.commit()

    row = db.execute(
        "SELECT * FROM users WHERE LOWER(email)=?", (email,)
    ).fetchone()

    if not row:
        return jsonify({"error": "User not found"}), 404

    return jsonify(dict(row)), 200


@app.post("/api/link-enrollment")
def link_enrollment():
    """Save new student info from enrollment linking page."""
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
    return jsonify({"message": "Enrollment linked successfully"}), 200

@app.post("/api/login-email")
def login_email():
    """Password-less student login by email (until Google OAuth is live)."""
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
        }), 200

    # New student → go to one-time enrollment
    log_login(email, "Unknown")
    return jsonify({
        "status": "new",
        "redirect": f"/ui_07_enrollment_linking_post_google_sign_in.html?email={email}"
    }), 200

@app.post("/api/admin-login")
def admin_login():
    """Admin login with email + password (hashed)."""
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

    # success
    log_login(email, row["name"])
    return jsonify({
        "status": "ok",
        "name": row["name"],
        "role": row["role"],
        "redirect": "/admin.html"
    }), 200

# --- Course Browser APIs ---

# --- Course Browser APIs (single, clean set) ---

# Demo resources until a real 'resources' table is wired


@app.get("/api/courses")
def list_courses():
    """
    Return a unique, ordered list of courses for the student.
    - Primary course comes from users table (if present)
    - A few demo courses are included so the browser looks populated
    Response: {"courses": ["M.Sc. (IT)", "Web Development", "Image Processing"]}
    """
    email = (request.args.get("email") or "").strip().lower()

    primary = None
    if email:
        row = get_db().execute(
            "SELECT course FROM users WHERE LOWER(email)=?", (email,)
        ).fetchone()
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
    """Return resources for an optional course (from DB), shaped for the dashboard cards."""
    course = (request.args.get("course") or "").strip()

    conn = get_conn()
    conn.row_factory = sqlite3.Row
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

    # dashboard expects tags as a list
    out = []
    for r in rows:
        item = dict(r)
        item["type"] = (item.get("type") or "").upper()  # “PDF” / “EPUB” / “VIDEO” etc.
        item["tags"] = [t.strip() for t in (item.get("tags") or "").split(",") if t.strip()]
        out.append(item)

    return jsonify({"resources": out})


# =========================
# Admin APIs (safe prefix)
# =========================

@app.get("/api/admin/summary")
def admin_summary():
    """Return quick metrics + recent items for dashboard cards."""
    conn = get_conn()
    c = conn.cursor()

    # totals
    users_count = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    resources_count = c.execute("SELECT COUNT(*) AS n FROM resources").fetchone()["n"]
    # distinct courses from users + resources
    courses = [r["course"] for r in c.execute("""
        SELECT course FROM users WHERE course IS NOT NULL AND TRIM(course)!=''
        UNION
        SELECT course FROM resources WHERE course IS NOT NULL AND TRIM(course)!=''
        ORDER BY course
    """).fetchall()]

    latest_users = c.execute("""
        SELECT user_id, name, email, course, semester
        FROM users
        ORDER BY user_id DESC
        LIMIT 5
    """).fetchall()

    latest_resources = c.execute("""
        SELECT id, title, type, course, tags, created_at
        FROM resources
        ORDER BY id DESC
        LIMIT 5
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
    """Search/browse users (q matches name/email/course)."""
    q = (request.args.get("q") or "").strip().lower()
    conn = get_conn()
    c = conn.cursor()
    if q:
        rows = c.execute("""
            SELECT user_id, name, email, course, semester, mobile
            FROM users
            WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR lower(course) LIKE ?
            ORDER BY user_id DESC
            LIMIT 200
        """, (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = c.execute("""
            SELECT user_id, name, email, course, semester, mobile
            FROM users
            ORDER BY user_id DESC
            LIMIT 200
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/admin/resources")
def admin_resources():
    """List resources; optional filter by course or type."""
    course = (request.args.get("course") or "").strip()
    rtype = (request.args.get("type") or "").strip().lower()
    conn = get_conn()
    c = conn.cursor()

    sql = "SELECT id, title, type, course, tags, link, created_at FROM resources WHERE 1=1"
    params = []
    if course:
        sql += " AND course = ?"
        params.append(course)
    if rtype in ("pdf","epub","video"):
        sql += " AND lower(type) = ?"
        params.append(rtype)

    sql += " ORDER BY id DESC LIMIT 300"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/resources")
def admin_add_resource():
    """Create a new resource row (robust tags + link normalization)."""
    data = request.get_json(force=True) or {}
    title   = (data.get("title")  or "").strip()
    rtype   = (data.get("type")   or "").strip().lower()
    course  = (data.get("course") or "").strip()
    link    = (data.get("link")   or "").strip()
    added_by = (data.get("added_by_email") or "").strip().lower()

    # tags can be a list or a comma string
    tags_in = data.get("tags") or ""
    if isinstance(tags_in, list):
        tags = ",".join(t.strip() for t in tags_in if t and t.strip())
    else:
        tags = ",".join(t.strip() for t in str(tags_in).split(",") if t.strip())

    # normalize relative /uploads paths to an absolute URL on this backend
    if link and (link.startswith("/uploads/") or link.startswith("uploads/")):
        fname = link.split("/uploads/")[-1]  # safe for both "/uploads/x" and "uploads/x"
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
    safe_name = f.filename.replace("..", "").replace("/", "_").replace("\\", "_")
    path = os.path.join(UPLOAD_DIR, safe_name)
    f.save(path)
    return jsonify({"url": f"/uploads/{safe_name}"})

# latest 50
@app.get("/api/admin/resources/recent")
def admin_recent_resources():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
      SELECT id, title, type, course, tags, created_at
      FROM resources
      ORDER BY id DESC LIMIT 50
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# all (for reports)
@app.get("/api/admin/resources/all")
def admin_all_resources():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
      SELECT id, title, type, course, tags, created_at
      FROM resources ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


    
# delete
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
    """Small KPIs for the admin dashboard."""
    conn = get_conn()
    c = conn.cursor()

    total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_resources = c.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    courses = c.execute("SELECT COUNT(DISTINCT course) FROM resources").fetchone()[0]

    # users seen in last 5 minutes (make sure /api/user updates last_seen)
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


# ---------------- Run ----------------
if __name__ == "__main__":
    with app.app_context():
        init_tables()
        ensure_user_last_seen_column()
    app.run(debug=True)



