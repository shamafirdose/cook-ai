"""Cook AI data layer: Turso (libSQL) cloud database with one private data set per user.

Every public function takes the signed-in user's id first, and every query is
filtered by it, so one user can never read or change another user's rows.
Without Turso secrets the app uses a local SQLite file, which is only meant
for testing on your own computer.
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Iterable, TypeVar

try:
    import libsql
except ImportError:  # older package name
    import libsql_experimental as libsql

from logic import (
    DIETS, MAX_INGREDIENTS_PER_DISH, clean_ingredient_names, clean_name,
    get_secret, normalize_key,
)
import shelf_life


T = TypeVar("T")

LOCAL_DB_PATH = get_secret("COOK_AI_LOCAL_DB", "cook_ai_local.db")
MAX_ITEMS = 300   # per list, per user
MAX_DISHES = 100  # per user

# Public "kind" names used by the app -> real table names (whitelist, so SQL stays safe).
KINDS = {
    "pantry": "pantry_items",
    "essential": "kitchen_essentials",
    "prohibited": "prohibited_items",
    "shopping": "shopping_items",
}
_EXTRA_COLUMN = {"pantry": "use_soon", "shopping": "bought"}
HISTORY_SOURCES = ("favourite", "alternative", "near_match")


class DatabaseError(RuntimeError):
    """The database could not be reached, or a query failed twice."""


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def storage_mode() -> str:
    return "cloud" if get_secret("TURSO_DATABASE_URL") else "local"


def _get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        url = get_secret("TURSO_DATABASE_URL")
        token = get_secret("TURSO_AUTH_TOKEN")
        if url:
            try:
                conn = libsql.connect(url, auth_token=token)
            except BaseException as exc:
                import sqlite3
                conn = sqlite3.connect(LOCAL_DB_PATH, check_same_thread=False)
        else:
            try:
                conn = libsql.connect(LOCAL_DB_PATH)
            except BaseException:
                import sqlite3
                conn = sqlite3.connect(LOCAL_DB_PATH, check_same_thread=False)
        _local.conn = conn
    return conn


def _reset_conn() -> None:
    conn = getattr(_local, "conn", None)
    _local.conn = None
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _tx(work: Callable[[object], T]) -> T:
    """Run `work(conn)` as one transaction; retry once on a fresh connection."""
    last: Exception | None = None
    for _ in range(2):
        conn = _get_conn()
        try:
            result = work(conn)
            conn.commit()
            return result
        except Exception as exc:  # network drop, expired stream, SQL error
            last = exc
            try:
                conn.rollback()
            except Exception:
                pass
            _reset_conn()
    raise DatabaseError(f"Database error: {last}") from last


def _insert(conn, sql: str, params: tuple) -> bool:
    """INSERT that returns False (instead of raising) on a UNIQUE violation."""
    try:
        conn.execute(sql, params)
        return True
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            return False
        raise


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _uid(user_id: object) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("A signed-in user id is required.")
    return uid


def _table(kind: str) -> str:
    try:
        return KINDS[kind]
    except KeyError:
        raise ValueError(f"Unknown list kind: {kind!r}") from None


# --------------------------------------------------------------------------
# Schema: only CREATE IF NOT EXISTS, so existing data is never touched
# --------------------------------------------------------------------------
def _item_table(table: str, extra: str = "") -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            {extra}
            created_at TEXT NOT NULL,
            UNIQUE (user_id, name_key)
        )"""


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        diet TEXT NOT NULL DEFAULT 'both',
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS usage_log (
        user_id TEXT NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day))""",
    _item_table("pantry_items", "use_soon INTEGER NOT NULL DEFAULT 0, expires_at TEXT,"),
    _item_table("kitchen_essentials"),
    _item_table("prohibited_items"),
    _item_table("shopping_items", "bought INTEGER NOT NULL DEFAULT 0,"),
    """CREATE TABLE IF NOT EXISTS food_rescue_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        item_name TEXT NOT NULL,
        saved_value REAL NOT NULL DEFAULT 3.50,
        rescued_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS dishes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        ingredients_status TEXT NOT NULL DEFAULT 'ready',
        created_at TEXT NOT NULL,
        UNIQUE (user_id, name_key))""",
    """CREATE TABLE IF NOT EXISTS dish_ingredients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dish_id INTEGER NOT NULL,
        ingredient_name TEXT NOT NULL,
        ingredient_key TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS cook_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        dish_name TEXT NOT NULL,
        source TEXT NOT NULL,
        cooked_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS user_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        email TEXT,
        feedback_type TEXT NOT NULL DEFAULT 'feedback',
        message TEXT NOT NULL,
        rating INTEGER,
        created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_dish_ingredients_dish ON dish_ingredients (dish_id)",
    "CREATE INDEX IF NOT EXISTS idx_dishes_user ON dishes (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_user ON cook_history (user_id, cooked_at)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_created ON user_feedback (created_at DESC)",
]


def initialize_database() -> None:
    """Create any missing tables. Safe to call on every run; does the work once per process."""
    global _initialized
    with _init_lock:
        if _initialized:
            return

        def work(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
            try:
                conn.execute("ALTER TABLE pantry_items ADD COLUMN expires_at TEXT")
            except Exception:
                pass

        _tx(work)
        _initialized = True


def check_connection() -> tuple[bool, str]:
    try:
        _tx(lambda conn: conn.execute("SELECT 1").fetchone())
        label = "Turso cloud database" if storage_mode() == "cloud" else "local test database"
        return True, f"Connected to the {label}."
    except Exception as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# Settings (diet) and daily usage cap
# --------------------------------------------------------------------------
def get_diet(user_id: str) -> str:
    uid = _uid(user_id)
    row = _tx(lambda conn: conn.execute("SELECT diet FROM user_settings WHERE user_id = ?", (uid,)).fetchone())
    return row[0] if row and row[0] in DIETS else "both"


def set_diet(user_id: str, diet: str) -> None:
    uid = _uid(user_id)
    if diet not in DIETS:
        raise ValueError(f"diet must be one of {DIETS}")
    _tx(lambda conn: conn.execute(
        "INSERT INTO user_settings (user_id, diet, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET diet = excluded.diet",
        (uid, diet, _now()),
    ))


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def get_usage_today(user_id: str) -> int:
    uid = _uid(user_id)
    row = _tx(lambda conn: conn.execute(
        "SELECT count FROM usage_log WHERE user_id = ? AND day = ?", (uid, _today())).fetchone())
    return int(row[0]) if row else 0


def consume_suggestion(user_id: str, daily_limit: int) -> tuple[bool, int]:
    """Count one suggestion request. Returns (allowed, used_today_after)."""
    uid = _uid(user_id)
    day = _today()

    def work(conn):
        row = conn.execute("SELECT count FROM usage_log WHERE user_id = ? AND day = ?", (uid, day)).fetchone()
        used = int(row[0]) if row else 0
        if used >= daily_limit:
            return False, used
        conn.execute(
            "INSERT INTO usage_log (user_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
            (uid, day),
        )
        return True, used + 1

    return _tx(work)


def load_all(user_id: str) -> dict:
    """Everything the main screen needs, in fast statements (fast on a cloud database)."""
    uid = _uid(user_id)
    day = _today()

    def work(conn):
        pantry_rows = conn.execute(
            "SELECT id, name, name_key, use_soon, expires_at FROM pantry_items WHERE user_id = ? ORDER BY name_key, id", (uid,)
        ).fetchall()
        rows = conn.execute(
            "SELECT 'essential', id, name, name_key, 0 FROM kitchen_essentials WHERE user_id = ? "
            "UNION ALL SELECT 'prohibited', id, name, name_key, 0 FROM prohibited_items WHERE user_id = ? "
            "UNION ALL SELECT 'shopping', id, name, name_key, bought FROM shopping_items WHERE user_id = ? "
            "UNION ALL SELECT 'diet', 0, diet, '', 0 FROM user_settings WHERE user_id = ? "
            "UNION ALL SELECT 'usage', 0, '', '', count FROM usage_log WHERE user_id = ? AND day = ?",
            (uid, uid, uid, uid, uid, day),
        ).fetchall()
        dish_rows = conn.execute(
            "SELECT d.id, d.name, d.name_key, d.ingredients_status, i.ingredient_name "
            "FROM dishes d LEFT JOIN dish_ingredients i ON i.dish_id = d.id "
            "WHERE d.user_id = ? ORDER BY d.name_key, d.id, i.id", (uid,)).fetchall()
        try:
            rescue_stats = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(saved_value), 0.0) FROM food_rescue_log WHERE user_id = ?", (uid,)
            ).fetchone()
        except Exception:
            rescue_stats = (0, 0.0)
        return pantry_rows, rows, dish_rows, rescue_stats

    pantry_rows, rows, dish_rows, rescue_stats = _tx(work)
    data: dict = {
        "pantry": [], "essential": [], "prohibited": [], "shopping": [],
        "diet": "both", "usage_today": 0, "dishes": [],
        "stats": {
            "rescued_count": int(rescue_stats[0]) if rescue_stats else 0,
            "money_saved": round(float(rescue_stats[1]), 2) if rescue_stats else 0.0,
        }
    }
    for item_id, name, key, flag, expires_at in pantry_rows:
        data["pantry"].append({
            "id": item_id,
            "name": name,
            "key": key,
            "use_soon": bool(flag),
            "expires_at": expires_at,
            "expiry_status": shelf_life.get_expiry_status(expires_at),
        })
    for kind, item_id, name, key, flag in rows:
        if kind == "diet":
            data["diet"] = name if name in DIETS else "both"
        elif kind == "usage":
            data["usage_today"] = int(flag)
        else:
            item = {"id": item_id, "name": name, "key": key}
            if kind == "shopping":
                item["bought"] = bool(flag)
            data[kind].append(item)
    for kind in ("pantry", "essential", "prohibited", "shopping"):
        data[kind].sort(key=lambda i: (i["key"], i["id"]))
    dishes: dict[int, dict] = {}
    for dish_id, name, key, status, ingredient in dish_rows:
        entry = dishes.setdefault(
            dish_id, {"id": dish_id, "name": name, "key": key, "status": status, "ingredients": []})
        if ingredient:
            entry["ingredients"].append(ingredient)
    data["dishes"] = list(dishes.values())
    return data


# --------------------------------------------------------------------------
# Simple name lists: pantry, essentials, not-allowed, shopping
# Status strings: "ok", "empty", "exists", "limit", "notfound"
# --------------------------------------------------------------------------
def get_items(user_id: str, kind: str) -> list[dict]:
    uid, table = _uid(user_id), _table(kind)
    extra = _EXTRA_COLUMN.get(kind)
    columns = "id, name, name_key" + (f", {extra}" if extra else "")
    rows = _tx(lambda conn: conn.execute(
        f"SELECT {columns} FROM {table} WHERE user_id = ? ORDER BY name_key, id", (uid,)).fetchall())
    items = []
    for row in rows:
        item = {"id": row[0], "name": row[1], "key": row[2]}
        if extra:
            item[extra] = bool(row[3])
        items.append(item)
    return items


def _add_one(conn, uid: str, table: str, name: str, expires_at: str | None = None) -> str:
    name = clean_name(name)
    key = normalize_key(name)
    if not name or not key:
        return "empty"
    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,)).fetchone()[0]
    if count >= MAX_ITEMS:
        return "limit"
    if conn.execute(f"SELECT 1 FROM {table} WHERE user_id = ? AND name_key = ?", (uid, key)).fetchone():
        return "exists"
    if table == "pantry_items":
        exp = expires_at or shelf_life.estimate_shelf_life(name)["expires_at"]
        inserted = _insert(
            conn,
            f"INSERT INTO {table} (user_id, name, name_key, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, name, key, exp, _now()),
        )
    else:
        inserted = _insert(
            conn,
            f"INSERT INTO {table} (user_id, name, name_key, created_at) VALUES (?, ?, ?, ?)",
            (uid, name, key, _now()),
        )
    return "ok" if inserted else "exists"


def add_item(user_id: str, kind: str, name: str, expires_at: str | None = None) -> str:
    uid, table = _uid(user_id), _table(kind)
    return _tx(lambda conn: _add_one(conn, uid, table, name, expires_at=expires_at))


def add_many(user_id: str, kind: str, names: Iterable[str]) -> dict[str, list[str]]:
    """Add several names in one go. Returns {'added': [...], 'skipped': [...]}."""
    uid, table = _uid(user_id), _table(kind)
    names = list(names)

    def work(conn):
        result: dict[str, list[str]] = {"added": [], "skipped": []}
        for name in names:
            status = _add_one(conn, uid, table, name)
            result["added" if status == "ok" else "skipped"].append(clean_name(name))
        return result

    return _tx(work)


def add_pantry_items_with_expiry(user_id: str, items: list[dict]) -> dict[str, list[str]]:
    """Add items where each item is {'name': str, 'expires_at': str | None}."""
    uid = _uid(user_id)

    def work(conn):
        result: dict[str, list[str]] = {"added": [], "skipped": []}
        for item in items:
            name = item.get("name", "")
            exp = item.get("expires_at")
            status = _add_one(conn, uid, "pantry_items", name, expires_at=exp)
            result["added" if status == "ok" else "skipped"].append(clean_name(name))
        return result

    return _tx(work)


def set_item_expiry(user_id: str, item_id: int, expires_at: str | None) -> None:
    uid = _uid(user_id)
    _tx(lambda conn: conn.execute(
        "UPDATE pantry_items SET expires_at = ? WHERE id = ? AND user_id = ?",
        (expires_at, item_id, uid)
    ))



def rename_item(user_id: str, kind: str, item_id: int, new_name: str) -> str:
    uid, table = _uid(user_id), _table(kind)
    name = clean_name(new_name)
    key = normalize_key(name)
    if not name or not key:
        return "empty"

    def work(conn):
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ? AND user_id = ?", (item_id, uid)).fetchone():
            return "notfound"
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE user_id = ? AND name_key = ? AND id <> ?", (uid, key, item_id)
        ).fetchone():
            return "exists"
        conn.execute(f"UPDATE {table} SET name = ?, name_key = ? WHERE id = ? AND user_id = ?",
                     (name, key, item_id, uid))
        return "ok"

    return _tx(work)


def delete_item(user_id: str, kind: str, item_id: int) -> None:
    delete_items(user_id, kind, [item_id])


def delete_items(user_id: str, kind: str, item_ids: Iterable[int]) -> None:
    uid, table = _uid(user_id), _table(kind)
    ids = [int(i) for i in item_ids]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    _tx(lambda conn: conn.execute(f"DELETE FROM {table} WHERE user_id = ? AND id IN ({marks})", (uid, *ids)))


def _set_flag(user_id: str, kind: str, item_id: int, flag: bool) -> None:
    uid, table = _uid(user_id), _table(kind)
    column = _EXTRA_COLUMN[kind]
    _tx(lambda conn: conn.execute(
        f"UPDATE {table} SET {column} = ? WHERE id = ? AND user_id = ?", (1 if flag else 0, item_id, uid)))


def set_use_soon(user_id: str, item_id: int, flag: bool) -> None:
    """Star (or unstar) a pantry item that should be used up first."""
    _set_flag(user_id, "pantry", item_id, flag)


def set_bought(user_id: str, item_id: int, flag: bool) -> None:
    _set_flag(user_id, "shopping", item_id, flag)


def move_shopping_to_pantry(user_id: str, item_id: int) -> str:
    """Move a bought shopping item into the pantry. Returns the pantry status string."""
    uid = _uid(user_id)

    def work(conn):
        row = conn.execute(
            "SELECT name FROM shopping_items WHERE id = ? AND user_id = ?", (item_id, uid)).fetchone()
        if not row:
            return "notfound"
        status = _add_one(conn, uid, "pantry_items", row[0])
        if status in ("ok", "exists"):
            conn.execute("DELETE FROM shopping_items WHERE id = ? AND user_id = ?", (item_id, uid))
        return status

    return _tx(work)


# --------------------------------------------------------------------------
# Favourite dishes (name + ingredient list; 'pending' means no ingredients yet)
# --------------------------------------------------------------------------
def get_dishes(user_id: str) -> list[dict]:
    uid = _uid(user_id)
    rows = _tx(lambda conn: conn.execute(
        "SELECT d.id, d.name, d.name_key, d.ingredients_status, i.ingredient_name "
        "FROM dishes d LEFT JOIN dish_ingredients i ON i.dish_id = d.id "
        "WHERE d.user_id = ? ORDER BY d.name_key, d.id, i.id", (uid,)).fetchall())
    dishes: dict[int, dict] = {}
    for dish_id, name, key, status, ingredient in rows:
        entry = dishes.setdefault(
            dish_id, {"id": dish_id, "name": name, "key": key, "status": status, "ingredients": []})
        if ingredient:
            entry["ingredients"].append(ingredient)
    return list(dishes.values())


def _write_ingredients(conn, dish_id: int, ingredients: list[str]) -> None:
    conn.execute("DELETE FROM dish_ingredients WHERE dish_id = ?", (dish_id,))
    for name in ingredients:
        conn.execute(
            "INSERT INTO dish_ingredients (dish_id, ingredient_name, ingredient_key) VALUES (?, ?, ?)",
            (dish_id, name, normalize_key(name)),
        )
    conn.execute("UPDATE dishes SET ingredients_status = ? WHERE id = ?",
                 ("ready" if ingredients else "pending", dish_id))


def add_dish(user_id: str, name: str, ingredients: Iterable[str] | None = None) -> str:
    """Save a favourite. With no ingredients it is saved as 'pending' and filled in later."""
    uid = _uid(user_id)
    name = clean_name(name)
    key = normalize_key(name)
    if not name or not key:
        return "empty"
    cleaned = clean_ingredient_names(ingredients)[:MAX_INGREDIENTS_PER_DISH]

    def work(conn):
        count = conn.execute("SELECT COUNT(*) FROM dishes WHERE user_id = ?", (uid,)).fetchone()[0]
        if count >= MAX_DISHES:
            return "limit"
        if conn.execute("SELECT 1 FROM dishes WHERE user_id = ? AND name_key = ?", (uid, key)).fetchone():
            return "exists"
        inserted = _insert(
            conn,
            "INSERT INTO dishes (user_id, name, name_key, ingredients_status, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, name, key, "pending", _now()),
        )
        if not inserted:
            return "exists"
        dish_id = conn.execute(
            "SELECT id FROM dishes WHERE user_id = ? AND name_key = ?", (uid, key)).fetchone()[0]
        _write_ingredients(conn, dish_id, cleaned)
        return "ok"

    return _tx(work)


def update_dish(user_id: str, dish_id: int, name: str, ingredients: Iterable[str] | None = None) -> str:
    """Rename a favourite; replaces its ingredients when `ingredients` is not None."""
    uid = _uid(user_id)
    name = clean_name(name)
    key = normalize_key(name)
    if not name or not key:
        return "empty"
    cleaned = None if ingredients is None else clean_ingredient_names(ingredients)

    def work(conn):
        if not conn.execute("SELECT 1 FROM dishes WHERE id = ? AND user_id = ?", (dish_id, uid)).fetchone():
            return "notfound"
        if conn.execute(
            "SELECT 1 FROM dishes WHERE user_id = ? AND name_key = ? AND id <> ?", (uid, key, dish_id)
        ).fetchone():
            return "exists"
        conn.execute("UPDATE dishes SET name = ?, name_key = ? WHERE id = ? AND user_id = ?",
                     (name, key, dish_id, uid))
        if cleaned is not None:
            _write_ingredients(conn, dish_id, cleaned)
        return "ok"

    return _tx(work)


def set_dish_ingredients(user_id: str, dish_id: int, ingredients: Iterable[str]) -> str:
    """Fill in (or replace) the ingredient list of a saved favourite."""
    uid = _uid(user_id)
    cleaned = clean_ingredient_names(ingredients)

    def work(conn):
        if not conn.execute("SELECT 1 FROM dishes WHERE id = ? AND user_id = ?", (dish_id, uid)).fetchone():
            return "notfound"
        _write_ingredients(conn, dish_id, cleaned)
        return "ok"

    return _tx(work)


def delete_dish(user_id: str, dish_id: int) -> None:
    uid = _uid(user_id)

    def work(conn):
        conn.execute(
            "DELETE FROM dish_ingredients WHERE dish_id IN (SELECT id FROM dishes WHERE id = ? AND user_id = ?)",
            (dish_id, uid),
        )
        conn.execute("DELETE FROM dishes WHERE id = ? AND user_id = ?", (dish_id, uid))

    _tx(work)


# --------------------------------------------------------------------------
# "Cooked this" history (used later to learn each user's patterns)
# --------------------------------------------------------------------------
def add_history(user_id: str, dish_name: str, source: str, used_item_ids: Iterable[int] | None = None) -> dict:
    uid = _uid(user_id)
    name = clean_name(dish_name)
    if not name or source not in HISTORY_SOURCES:
        raise ValueError("A dish name and a valid source are required.")

    rescued_count = 0
    def work(conn):
        conn.execute(
            "INSERT INTO cook_history (user_id, dish_name, source, cooked_at) VALUES (?, ?, ?, ?)",
            (uid, name, source, _now()),
        )
        nonlocal rescued_count
        if used_item_ids:
            ids = [int(i) for i in used_item_ids]
            if ids:
                marks = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT name, expires_at, use_soon FROM pantry_items WHERE user_id = ? AND id IN ({marks})",
                    (uid, *ids),
                ).fetchall()
                for item_name, exp, soon in rows:
                    days = shelf_life.days_until(exp)
                    if bool(soon) or (days is not None and days <= 4):
                        conn.execute(
                            "INSERT INTO food_rescue_log (user_id, item_name, saved_value, rescued_at) VALUES (?, ?, ?, ?)",
                            (uid, item_name, 3.50, _now()),
                        )
                        rescued_count += 1
                conn.execute(f"DELETE FROM pantry_items WHERE user_id = ? AND id IN ({marks})", (uid, *ids))
        return rescued_count

    count = _tx(work)
    return {"status": "ok", "rescued_count": count}


def get_waste_stats(user_id: str) -> dict:
    uid = _uid(user_id)

    def work(conn):
        try:
            rescue = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(saved_value), 0.0) FROM food_rescue_log WHERE user_id = ?", (uid,)
            ).fetchone()
            rescued_count = int(rescue[0]) if rescue else 0
            money_saved = round(float(rescue[1]), 2) if rescue else 0.0
        except Exception:
            rescued_count, money_saved = 0, 0.0
        return {"rescued_count": rescued_count, "money_saved": money_saved}

    return _tx(work)


def get_recent_history(user_id: str, limit: int = 50) -> list[dict]:
    uid = _uid(user_id)
    rows = _tx(lambda conn: conn.execute(
        "SELECT dish_name, source, cooked_at FROM cook_history WHERE user_id = ? "
        "ORDER BY cooked_at DESC, id DESC LIMIT ?", (uid, int(limit))).fetchall())
    return [{"name": r[0], "source": r[1], "cooked_at": r[2]} for r in rows]


# --------------------------------------------------------------------------
# Privacy: remove everything stored for one user
# --------------------------------------------------------------------------
def delete_all_user_data(user_id: str) -> None:
    uid = _uid(user_id)

    def work(conn):
        conn.execute(
            "DELETE FROM dish_ingredients WHERE dish_id IN (SELECT id FROM dishes WHERE user_id = ?)", (uid,))
        for table in ("dishes", "pantry_items", "kitchen_essentials", "prohibited_items",
                      "shopping_items", "cook_history", "usage_log", "user_settings"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))

    _tx(work)


# --------------------------------------------------------------------------
# Feedback & Feature Requests
# --------------------------------------------------------------------------
def add_feedback(user_id: str, message: str, email: str = "", feedback_type: str = "feedback", rating: int | None = None) -> int:
    """Save user feedback/feature request permanently."""
    uid = str(user_id or "guest").strip()
    msg = str(message or "").strip()
    if not msg:
        raise ValueError("Message cannot be empty.")

    def work(conn):
        conn.execute(
            "INSERT INTO user_feedback (user_id, email, feedback_type, message, rating, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, email.strip(), feedback_type.strip(), msg, rating, _now())
        )
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        return row[0] if row else 1

    return _tx(work)


def get_all_feedback(limit: int = 100) -> list[dict]:
    """Retrieve recent user feedback for developer review."""
    def work(conn):
        rows = conn.execute(
            "SELECT id, user_id, email, feedback_type, message, rating, created_at "
            "FROM user_feedback ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": r[0], "user_id": r[1], "email": r[2], "feedback_type": r[3],
                "message": r[4], "rating": r[5], "created_at": r[6]
            }
            for r in rows
        ]
    return _tx(work)
