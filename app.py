"""
Siganduru Shop — Cloud API (Full Version)
Deploy on Render.com
Env vars: DATABASE_URL, OWNER_KEY
"""

import os, datetime
from flask import Flask, request, jsonify
from functools import wraps

app = Flask(__name__)

OWNER_KEY = os.environ.get("OWNER_KEY", "changeme")
DB_URL    = os.environ.get("DATABASE_URL", "")

# ── Database ──────────────────────────────────────────────────

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

def q(conn, driver, sql, params=()):
    cur = conn.cursor()
    if driver == "pg":
        cur.execute(sql.replace("?", "%s"), params)
    else:
        cur.execute(sql, params)
    try:
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except:
        return []

def init_cloud_db():
    conn, driver = get_db()
    cur = conn.cursor()
    real = "NUMERIC" if driver == "pg" else "REAL"
    pk   = "SERIAL PRIMARY KEY" if driver == "pg" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    tables = [
        f"""CREATE TABLE IF NOT EXISTS items (
            item_id INTEGER PRIMARY KEY,
            item_name TEXT, unit TEXT,
            purchase_price {real} DEFAULT 0,
            selling_price  {real} DEFAULT 0,
            stock_quantity {real} DEFAULT 0,
            updated_at TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS bills (
            bill_id INTEGER PRIMARY KEY,
            bill_date TEXT, bill_time TEXT,
            total {real}, payment_mode TEXT,
            created_by TEXT, synced_at TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS bill_items (
            id INTEGER PRIMARY KEY,
            bill_id INTEGER, item_id INTEGER,
            quantity {real}, selling_price {real}, line_total {real})""",
        f"""CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY,
            order_date TEXT, order_time TEXT,
            status TEXT, notes TEXT,
            created_by TEXT, synced_at TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS sync_log (
            id {pk}, synced_at TEXT,
            table_name TEXT, records INTEGER, source TEXT)""",
    ]
    for t in tables:
        cur.execute(t)
    conn.commit()
    conn.close()

with app.app_context():
    init_cloud_db()

# ── Auth ──────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != OWNER_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ── Basic routes ──────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify({"app": "Siganduru Shop Cloud API", "status": "running"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

@app.route("/api/auth/verify")
@require_auth
def verify():
    return jsonify({"ok": True})

# ── Sync Upload (desktop → cloud) ─────────────────────────────

@app.route("/api/sync/upload", methods=["POST"])
@require_auth
def sync_upload():
    data = request.get_json()
    conn, driver = get_db()
    cur = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()
    counts = {}

    # Items
    items = data.get("items", [])
    for it in items:
        if driver == "pg":
            cur.execute("""
                INSERT INTO items VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (item_id) DO UPDATE SET
                item_name=EXCLUDED.item_name, unit=EXCLUDED.unit,
                purchase_price=EXCLUDED.purchase_price,
                selling_price=EXCLUDED.selling_price,
                stock_quantity=EXCLUDED.stock_quantity,
                updated_at=EXCLUDED.updated_at
            """, (it["item_id"],it["item_name"],it["unit"],
                  it["purchase_price"],it["selling_price"],
                  it["stock_quantity"],now))
        else:
            cur.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)",
                (it["item_id"],it["item_name"],it["unit"],
                 it["purchase_price"],it["selling_price"],
                 it["stock_quantity"],now))
    counts["items"] = len(items)

    # Bills
    bills = data.get("bills", [])
    for b in bills:
        if driver == "pg":
            cur.execute("""INSERT INTO bills VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (bill_id) DO NOTHING""",
                (b["bill_id"],b["bill_date"],b["bill_time"],
                 b["total"],b["payment_mode"],b["created_by"],now))
        else:
            cur.execute("INSERT OR IGNORE INTO bills VALUES (?,?,?,?,?,?,?)",
                (b["bill_id"],b["bill_date"],b["bill_time"],
                 b["total"],b["payment_mode"],b["created_by"],now))
    counts["bills"] = len(bills)

    # Bill items
    bill_items = data.get("bill_items", [])
    for bi in bill_items:
        if driver == "pg":
            cur.execute("""INSERT INTO bill_items VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING""",
                (bi["id"],bi["bill_id"],bi["item_id"],
                 bi["quantity"],bi["selling_price"],bi["line_total"]))
        else:
            cur.execute("INSERT OR IGNORE INTO bill_items VALUES (?,?,?,?,?,?)",
                (bi["id"],bi["bill_id"],bi["item_id"],
                 bi["quantity"],bi["selling_price"],bi["line_total"]))
    counts["bill_items"] = len(bill_items)

    if driver == "pg":
        cur.execute("INSERT INTO sync_log(synced_at,table_name,records,source) VALUES (%s,%s,%s,%s)",
            (now,"batch",sum(counts.values()),"desktop"))
    else:
        cur.execute("INSERT INTO sync_log(synced_at,table_name,records,source) VALUES (?,?,?,?)",
            (now,"batch",sum(counts.values()),"desktop"))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "synced_at": now, "counts": counts})

# ── Mobile: Dashboard ─────────────────────────────────────────

@app.route("/api/mobile/dashboard")
@require_auth
def mobile_dashboard():
    today = datetime.date.today().strftime("%Y-%m-%d")
    month = datetime.date.today().strftime("%Y-%m")
    conn, driver = get_db()

    today_bills  = q(conn, driver, "SELECT total, payment_mode FROM bills WHERE bill_date=?", (today,))
    month_bills  = q(conn, driver, "SELECT total FROM bills WHERE bill_date LIKE ?", (f"{month}%",))
    low_stock    = q(conn, driver, "SELECT COUNT(*) cnt FROM items WHERE stock_quantity <= 2")
    recent_bills = q(conn, driver,
        "SELECT bill_id,bill_date,bill_time,total,payment_mode FROM bills ORDER BY bill_id DESC LIMIT 10")
    conn.close()

    cash  = sum(float(r["total"] or 0) for r in today_bills if r["payment_mode"] == "Cash")
    upi   = sum(float(r["total"] or 0) for r in today_bills if r["payment_mode"] == "UPI")
    return jsonify({
        "today": {
            "bill_count": len(today_bills),
            "cash": cash, "upi": upi,
            "total": cash + upi,
            "date": today,
        },
        "month_total": sum(float(r["total"] or 0) for r in month_bills),
        "low_stock_count": list(low_stock[0].values())[0] if low_stock else 0,
        "recent_bills": recent_bills,
    })

# ── Mobile: Stock ─────────────────────────────────────────────

@app.route("/api/mobile/stock")
@require_auth
def mobile_stock():
    conn, driver = get_db()
    rows = q(conn, driver, "SELECT * FROM items ORDER BY item_name")
    conn.close()
    return jsonify(rows)

# ── Mobile: Daily ─────────────────────────────────────────────

@app.route("/api/mobile/daily")
@require_auth
def mobile_daily():
    date = request.args.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    conn, driver = get_db()
    bills = q(conn, driver, "SELECT * FROM bills WHERE bill_date=? ORDER BY bill_id DESC", (date,))
    items = q(conn, driver, """
        SELECT i.item_name, i.unit, i.selling_price,
               SUM(bi.quantity) qty_sold,
               SUM(bi.line_total) total_sales
        FROM bill_items bi
        JOIN bills b ON bi.bill_id=b.bill_id
        JOIN items  i ON bi.item_id=i.item_id
        WHERE b.bill_date=?
        GROUP BY bi.item_id, i.item_name, i.unit, i.selling_price
        ORDER BY total_sales DESC
    """, (date,))
    conn.close()
    cash = sum(float(r["total"] or 0) for r in bills if r["payment_mode"] == "Cash")
    upi  = sum(float(r["total"] or 0) for r in bills if r["payment_mode"] == "UPI")
    return jsonify({
        "date": date, "count": len(bills),
        "cash": cash, "upi": upi, "total": cash + upi,
        "bills": bills, "items": items,
    })

# ── Mobile: Monthly ───────────────────────────────────────────

@app.route("/api/mobile/monthly")
@require_auth
def mobile_monthly():
    yr     = int(request.args.get("year",  datetime.date.today().year))
    mo     = int(request.args.get("month", datetime.date.today().month))
    prefix = f"{yr}-{mo:02d}"
    conn, driver = get_db()
    items = q(conn, driver, """
        SELECT i.item_name, i.unit,
               SUM(bi.quantity) qty,
               SUM(bi.line_total) revenue,
               SUM(bi.quantity * i.purchase_price) cost
        FROM bill_items bi
        JOIN bills b ON bi.bill_id=b.bill_id
        JOIN items  i ON bi.item_id=i.item_id
        WHERE b.bill_date LIKE ?
        GROUP BY bi.item_id, i.item_name, i.unit
        ORDER BY revenue DESC
    """, (f"{prefix}%",))
    totals = q(conn, driver,
        "SELECT SUM(total) revenue, COUNT(*) bill_count FROM bills WHERE bill_date LIKE ?",
        (f"{prefix}%",))
    conn.close()
    total_rev  = float(totals[0]["revenue"] or 0) if totals else 0
    total_cost = sum(float(r["cost"] or 0) for r in items)
    return jsonify({
        "year": yr, "month": mo,
        "revenue": total_rev, "cost": total_cost,
        "profit": total_rev - total_cost,
        "bill_count": totals[0]["bill_count"] if totals else 0,
        "items": items,
    })

# ── Mobile: Chart ─────────────────────────────────────────────

@app.route("/api/mobile/chart/daily_sales")
@require_auth
def chart_daily_sales():
    conn, driver = get_db()
    if driver == "pg":
        rows = q(conn, driver, """
            SELECT bill_date, SUM(total) total, COUNT(*) bill_count
            FROM bills
            WHERE bill_date >= (CURRENT_DATE - INTERVAL '30 days')::text
            GROUP BY bill_date ORDER BY bill_date
        """)
    else:
        rows = q(conn, driver, """
            SELECT bill_date, SUM(total) total, COUNT(*) bill_count
            FROM bills
            WHERE bill_date >= date('now', '-30 days')
            GROUP BY bill_date ORDER BY bill_date
        """)
    conn.close()
    return jsonify(rows)

# ── Sync Status ───────────────────────────────────────────────

@app.route("/api/sync/status")
@require_auth
def sync_status():
    conn, driver = get_db()
    rows = q(conn, driver, "SELECT * FROM sync_log ORDER BY id DESC LIMIT 5")
    conn.close()
    return jsonify(rows)

# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
