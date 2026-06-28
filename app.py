"""
cloud_api/app.py
================
Siganduru Shop — Cloud Sync + Mobile API
Deploy free on: Railway / Render / Fly.io
Set env vars: DATABASE_URL, OWNER_KEY
"""

import os, datetime, hashlib, json
from flask import Flask, request, jsonify
from functools import wraps

app = Flask(__name__)

# ── Single owner API key (set in env, never in code) ──────────
OWNER_KEY = os.environ.get("OWNER_KEY", "changeme_set_in_env")
DB_URL    = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

def get_db():
    """Return psycopg2 connection (PostgreSQL) or SQLite for local dev."""
    if DB_URL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_URL)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn, "pg"
    else:
        import sqlite3
        conn = sqlite3.connect("cloud_dev.db")
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"

def q(conn, driver, sql, params=()):
    """Run query — works for both pg and sqlite."""
    cur = conn.cursor()
    if driver == "pg":
        cur.execute(sql.replace("?", "%s").replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "SERIAL PRIMARY KEY"
        ), params)
    else:
        cur.execute(sql, params)
    return cur

def init_cloud_db():
    conn, driver = get_db()
    cur = conn.cursor()

    # Use SERIAL for pg, AUTOINCREMENT for sqlite
    auto = "SERIAL PRIMARY KEY" if driver == "pg" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    text = "TEXT" if driver == "pg" else "TEXT"
    real = "NUMERIC" if driver == "pg" else "REAL"

    statements = [
        f"""CREATE TABLE IF NOT EXISTS items (
            item_id        INTEGER PRIMARY KEY,
            item_name      {text},
            unit           {text},
            purchase_price {real} DEFAULT 0,
            selling_price  {real} DEFAULT 0,
            stock_quantity {real} DEFAULT 0,
            updated_at     {text},
            created_at     {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bills (
            bill_id      INTEGER PRIMARY KEY,
            bill_date    {text},
            bill_time    {text},
            total        {real},
            payment_mode {text},
            created_by   {text},
            synced_at    {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bill_items (
            id            INTEGER PRIMARY KEY,
            bill_id       INTEGER,
            item_id       INTEGER,
            quantity      {real},
            selling_price {real},
            line_total    {real}
        )""",
        f"""CREATE TABLE IF NOT EXISTS orders (
            order_id   INTEGER PRIMARY KEY,
            order_date {text},
            order_time {text},
            status     {text},
            notes      {text},
            created_by {text},
            synced_at  {text}
        )""",
        f"""CREATE TABLE IF NOT EXISTS sync_log (
            id         {'SERIAL PRIMARY KEY' if driver=='pg' else 'INTEGER PRIMARY KEY AUTOINCREMENT'},
            synced_at  {text},
            table_name {text},
            records    INTEGER,
            source     {text}
        )""",
    ]
    for s in statements:
        cur.execute(s)
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────
# AUTH — simple API key in header
# ─────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != OWNER_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/api/auth/verify")
@require_auth
def auth_verify():
    return jsonify({"ok": True, "message": "Authenticated"})

# ─────────────────────────────────────────────────────────────
# SYNC UPLOAD — desktop → cloud
# ─────────────────────────────────────────────────────────────

@app.route("/api/sync/upload", methods=["POST"])
@require_auth
def sync_upload():
    """
    Desktop sends changed records in one batch.
    Payload: { items:[...], bills:[...], bill_items:[...], orders:[...] }
    Strategy: UPSERT (insert or update by primary key)
    """
    data  = request.get_json()
    conn, driver = get_db()
    now   = datetime.datetime.utcnow().isoformat()
    counts = {}

    try:
        cur = conn.cursor()

        # ── Items (prices + stock) ──────────────────────────
        items = data.get("items", [])
        for it in items:
            if driver == "pg":
                cur.execute("""
                    INSERT INTO items
                      (item_id, item_name, unit, purchase_price, selling_price, stock_quantity, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (item_id) DO UPDATE SET
                      item_name=EXCLUDED.item_name,
                      unit=EXCLUDED.unit,
                      purchase_price=EXCLUDED.purchase_price,
                      selling_price=EXCLUDED.selling_price,
                      stock_quantity=EXCLUDED.stock_quantity,
                      updated_at=EXCLUDED.updated_at
                """, (it["item_id"], it["item_name"], it["unit"],
                      it["purchase_price"], it["selling_price"],
                      it["stock_quantity"], now))
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO items
                      (item_id, item_name, unit, purchase_price, selling_price, stock_quantity, updated_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (it["item_id"], it["item_name"], it["unit"],
                      it["purchase_price"], it["selling_price"],
                      it["stock_quantity"], now))
        counts["items"] = len(items)

        # ── Bills ───────────────────────────────────────────
        bills = data.get("bills", [])
        for b in bills:
            if driver == "pg":
                cur.execute("""
                    INSERT INTO bills
                      (bill_id, bill_date, bill_time, total, payment_mode, created_by, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (bill_id) DO NOTHING
                """, (b["bill_id"], b["bill_date"], b["bill_time"],
                      b["total"], b["payment_mode"], b["created_by"], now))
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO bills
                      (bill_id, bill_date, bill_time, total, payment_mode, created_by, synced_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (b["bill_id"], b["bill_date"], b["bill_time"],
                      b["total"], b["payment_mode"], b["created_by"], now))
        counts["bills"] = len(bills)

        # ── Bill Items ──────────────────────────────────────
        bill_items = data.get("bill_items", [])
        for bi in bill_items:
            if driver == "pg":
                cur.execute("""
                    INSERT INTO bill_items
                      (id, bill_id, item_id, quantity, selling_price, line_total)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (bi["id"], bi["bill_id"], bi["item_id"],
                      bi["quantity"], bi["selling_price"], bi["line_total"]))
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO bill_items
                      (id, bill_id, item_id, quantity, selling_price, line_total)
                    VALUES (?,?,?,?,?,?)
                """, (bi["id"], bi["bill_id"], bi["item_id"],
                      bi["quantity"], bi["selling_price"], bi["line_total"]))
        counts["bill_items"] = len(bill_items)

        # ── Orders ──────────────────────────────────────────
        orders = data.get("orders", [])
        for o in orders:
            if driver == "pg":
                cur.execute("""
                    INSERT INTO orders
                      (order_id, order_date, order_time, status, notes, created_by, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (order_id) DO UPDATE SET status=EXCLUDED.status
                """, (o["order_id"], o["order_date"], o["order_time"],
                      o["status"], o.get("notes",""), o["created_by"], now))
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO orders
                      (order_id, order_date, order_time, status, notes, created_by, synced_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (o["order_id"], o["order_date"], o["order_time"],
                      o["status"], o.get("notes",""), o["created_by"], now))
        counts["orders"] = len(orders)

        # Log the sync
        if driver == "pg":
            cur.execute(
                "INSERT INTO sync_log(synced_at, table_name, records, source) VALUES(%s,%s,%s,%s)",
                (now, "batch", sum(counts.values()), "desktop"))
        else:
            cur.execute(
                "INSERT INTO sync_log(synced_at, table_name, records, source) VALUES(?,?,?,?)",
                (now, "batch", sum(counts.values()), "desktop"))

        conn.commit()
        return jsonify({"ok": True, "synced_at": now, "counts": counts})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()

# ─────────────────────────────────────────────────────────────
# MOBILE READ ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.route("/api/mobile/dashboard")
@require_auth
def mobile_dashboard():
    """Overview: today's stats + month total + low stock count."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    month = datetime.date.today().strftime("%Y-%m")
    conn, driver = get_db()

    def run(sql, params=()):
        cur = conn.cursor()
        if driver == "pg": cur.execute(sql.replace("?","%s"), params)
        else:              cur.execute(sql, params)
        return cur.fetchall()

    today_bills = run("SELECT total, payment_mode FROM bills WHERE bill_date=?", (today,))
    month_bills = run("SELECT total FROM bills WHERE bill_date LIKE ?", (f"{month}%",))
    low_stock   = run("SELECT COUNT(*) cnt FROM items WHERE stock_quantity <= 2")
    recent      = run("""
        SELECT bill_id, bill_date, bill_time, total, payment_mode
        FROM bills ORDER BY bill_id DESC LIMIT 10
    """)
    conn.close()

    cash  = sum(r["total"] for r in today_bills if r["payment_mode"] == "Cash")
    upi   = sum(r["total"] for r in today_bills if r["payment_mode"] == "UPI")
    return jsonify({
        "today": {
            "bill_count": len(today_bills),
            "cash": cash, "upi": upi,
            "total": cash + upi,
            "date": today,
        },
        "month_total":  sum(r["total"] for r in month_bills),
        "low_stock_count": list(low_stock[0].values())[0] if low_stock else 0,
        "recent_bills": [dict(r) for r in recent],
    })

@app.route("/api/mobile/stock")
@require_auth
def mobile_stock():
    conn, driver = get_db()
    cur = conn.cursor()
    if driver == "pg": cur.execute("SELECT * FROM items ORDER BY item_name")
    else:              cur.execute("SELECT * FROM items ORDER BY item_name")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/mobile/daily")
@require_auth
def mobile_daily():
    date = request.args.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    conn, driver = get_db()

    def run(sql, params=()):
        cur = conn.cursor()
        if driver == "pg": cur.execute(sql.replace("?","%s"), params)
        else:              cur.execute(sql, params)
        return cur.fetchall()

    bills = run("SELECT * FROM bills WHERE bill_date=? ORDER BY bill_id DESC", (date,))
    items = run("""
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

    cash = sum(r["total"] for r in bills if r["payment_mode"]=="Cash")
    upi  = sum(r["total"] for r in bills if r["payment_mode"]=="UPI")
    return jsonify({
        "date": date, "count": len(bills),
        "cash": cash, "upi": upi, "total": cash+upi,
        "bills": [dict(r) for r in bills],
        "items": [dict(r) for r in items],
    })

@app.route("/api/mobile/monthly")
@require_auth
def mobile_monthly():
    yr = int(request.args.get("year",  datetime.date.today().year))
    mo = int(request.args.get("month", datetime.date.today().month))
    prefix = f"{yr}-{mo:02d}"
    conn, driver = get_db()

    def run(sql, params=()):
        cur = conn.cursor()
        if driver == "pg": cur.execute(sql.replace("?","%s").replace("LIKE ?","LIKE %s"), params)
        else:              cur.execute(sql, params)
        return cur.fetchall()

    daily = run("""
        SELECT bill_date, COUNT(*) bill_count, SUM(total) total
        FROM bills WHERE bill_date LIKE ?
        GROUP BY bill_date ORDER BY bill_date
    """, (f"{prefix}%",))

    items = run("""
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
    conn.close()

    item_list  = [dict(r) for r in items]
    total_rev  = sum(r["revenue"] or 0 for r in item_list)
    total_cost = sum(r["cost"]    or 0 for r in item_list)
    return jsonify({
        "year": yr, "month": mo,
        "revenue": total_rev, "cost": total_cost,
        "profit":  total_rev - total_cost,
        "daily_breakdown": [dict(r) for r in daily],
        "items": item_list,
    })

@app.route("/api/mobile/chart/daily_sales")
@require_auth
def chart_daily_sales():
    """Last 30 days sales — for chart in mobile app."""
    conn, driver = get_db()
    cur = conn.cursor()
    sql = """
        SELECT bill_date, SUM(total) total, COUNT(*) bill_count
        FROM bills
        WHERE bill_date >= date('now', '-30 days')
        GROUP BY bill_date ORDER BY bill_date
    """
    if driver == "pg":
        sql = sql.replace("date('now', '-30 days')", "CURRENT_DATE - INTERVAL '30 days'")
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sync/status")
@require_auth
def sync_status():
    conn, driver = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 5")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

if __name__ == "__main__":
    init_cloud_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
