"""
cloud_api/app.py
================
Siganduru Shop — Cloud Sync + Mobile API
Deploy free on: Render / Railway / Fly.io
Env vars required:
- DATABASE_URL
- OWNER_KEY
"""

import os, datetime
from flask import Flask, request, jsonify
from functools import wraps

app = Flask(__name__)

# ── ENV ──────────────────────────────────────────────────────
OWNER_KEY = os.environ.get("OWNER_KEY", "changeme")
DB_URL    = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

def get_db():
    if DB_URL:
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(DB_URL)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn, "pg"
    else:
        import sqlite3
        conn = sqlite3.connect("cloud_dev.db")
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"

def init_cloud_db():
    conn, driver = get_db()
    cur = conn.cursor()

    text = "TEXT"
    real = "NUMERIC" if driver == "pg" else "REAL"
    pk   = "SERIAL PRIMARY KEY" if driver == "pg" else "INTEGER PRIMARY KEY AUTOINCREMENT"

    tables = [
        f"""CREATE TABLE IF NOT EXISTS items (
            item_id INTEGER PRIMARY KEY,
            item_name {text},
            unit {text},
            purchase_price {real},
            selling_price {real},
            stock_quantity {real},
            updated_at {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bills (
            bill_id INTEGER PRIMARY KEY,
            bill_date {text},
            bill_time {text},
            total {real},
            payment_mode {text},
            created_by {text},
            synced_at {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bill_items (
            id INTEGER PRIMARY KEY,
            bill_id INTEGER,
            item_id INTEGER,
            quantity {real},
            selling_price {real},
            line_total {real}
        )""",
        f"""CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY,
            order_date {text},
            order_time {text},
            status {text},
            notes {text},
            created_by {text},
            synced_at {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS sync_log (
            id {pk},
            synced_at {text},
            table_name {text},
            records INTEGER,
            source {text}
        )"""
    ]

    for t in tables:
        cur.execute(t)

    conn.commit()
    conn.close()

# 🔥 REQUIRED FOR RENDER / GUNICORN
with app.app_context():
    init_cloud_db()

# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != OWNER_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify({
        "app": "Siganduru Shop Cloud API",
        "status": "running"
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

@app.route("/api/auth/verify")
@require_auth
def verify():
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────────
# SYNC UPLOAD (DESKTOP → CLOUD)
# ─────────────────────────────────────────────────────────────

@app.route("/api/sync/upload", methods=["POST"])
@require_auth
def sync_upload():
    data = request.get_json()
    conn, driver = get_db()
    cur = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()

    for it in data.get("items", []):
        if driver == "pg":
            cur.execute("""
                INSERT INTO items VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (item_id) DO UPDATE SET
                item_name=EXCLUDED.item_name,
                selling_price=EXCLUDED.selling_price,
                stock_quantity=EXCLUDED.stock_quantity,
                updated_at=EXCLUDED.updated_at
            """, (
                it["item_id"], it["item_name"], it["unit"],
                it["purchase_price"], it["selling_price"],
                it["stock_quantity"], now
            ))
        else:
            cur.execute("""
                INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)
            """, (
                it["item_id"], it["item_name"], it["unit"],
                it["purchase_price"], it["selling_price"],
                it["stock_quantity"], now
            ))

    cur.execute(
        "INSERT INTO sync_log(synced_at, table_name, records, source) VALUES (%s,%s,%s,%s)"
        if driver == "pg" else
        "INSERT INTO sync_log(synced_at, table_name, records, source) VALUES (?,?,?,?)",
        (now, "batch", len(data.get("items", [])), "desktop")
    )

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "synced_at": now})

# ─────────────────────────────────────────────────────────────
# MOBILE READ
# ─────────────────────────────────────────────────────────────

@app.route("/api/mobile/stock")
@require_auth
def mobile_stock():
    conn, driver = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items ORDER BY item_name")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
