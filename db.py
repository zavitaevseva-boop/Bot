"""Работа с базой данных SQLite: товары, заказы, пользователи."""
import aiosqlite
from datetime import datetime

DB_PATH = "shop.db"


async def init_db(path: str = DB_PATH):
    global DB_PATH
    DB_PATH = path
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                price_stars INTEGER NOT NULL,
                price_rub INTEGER DEFAULT 0,
                photo_id TEXT,
                content_kind TEXT NOT NULL,      -- text | document | photo | video | audio
                content TEXT NOT NULL,           -- текст/ссылка или file_id
                active INTEGER DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                method TEXT NOT NULL,            -- stars | manual
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,            -- pending | paid | rejected | refunded
                charge_id TEXT,
                created_at TEXT
            );
            """
        )
        await db.commit()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


async def _fetchall(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _fetchone(sql, params=()):
    rows = await _fetchall(sql, params)
    return rows[0] if rows else None


async def _execute(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


# ---------- пользователи ----------
async def add_user(user_id, username, first_name):
    await _execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?,?,?,?)",
        (user_id, username, first_name, _now()),
    )


async def all_user_ids():
    return [r["user_id"] for r in await _fetchall("SELECT user_id FROM users")]


# ---------- товары ----------
async def add_product(title, description, price_stars, price_rub, photo_id, content_kind, content):
    return await _execute(
        """INSERT INTO products (title, description, price_stars, price_rub, photo_id,
           content_kind, content, active, created_at) VALUES (?,?,?,?,?,?,?,1,?)""",
        (title, description, price_stars, price_rub, photo_id, content_kind, content, _now()),
    )


async def get_products(only_active=True):
    sql = "SELECT * FROM products" + (" WHERE active=1" if only_active else "") + " ORDER BY id"
    return await _fetchall(sql)


async def get_product(pid):
    return await _fetchone("SELECT * FROM products WHERE id=?", (pid,))


async def update_product(pid, field, value):
    allowed = {"title", "description", "price_stars", "price_rub", "photo_id", "content_kind", "content", "active"}
    if field not in allowed:
        raise ValueError(field)
    await _execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))


async def delete_product(pid):
    await _execute("DELETE FROM products WHERE id=?", (pid,))


# ---------- заказы ----------
async def create_order(user_id, username, product_id, method, amount, status="pending", charge_id=None):
    return await _execute(
        """INSERT INTO orders (user_id, username, product_id, method, amount, status, charge_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, username, product_id, method, amount, status, charge_id, _now()),
    )


async def get_order(oid):
    return await _fetchone("SELECT * FROM orders WHERE id=?", (oid,))


async def set_order_status(oid, status):
    await _execute("UPDATE orders SET status=? WHERE id=?", (status, oid))


async def user_purchases(user_id):
    return await _fetchall(
        """SELECT o.id, o.product_id, p.title, o.created_at FROM orders o
           JOIN products p ON p.id=o.product_id
           WHERE o.user_id=? AND o.status='paid' ORDER BY o.id DESC""",
        (user_id,),
    )


async def has_paid(user_id, product_id):
    return await _fetchone(
        "SELECT id FROM orders WHERE user_id=? AND product_id=? AND status='paid'", (user_id, product_id)
    ) is not None


async def recent_orders(limit=15):
    return await _fetchall(
        """SELECT o.*, p.title FROM orders o LEFT JOIN products p ON p.id=o.product_id
           ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    )


async def stats():
    s = await _fetchone(
        """SELECT
             (SELECT COUNT(*) FROM users) AS users,
             (SELECT COUNT(*) FROM orders WHERE status='paid') AS sales,
             (SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND method='stars') AS stars,
             (SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND method='manual') AS rub"""
    )
    return s
