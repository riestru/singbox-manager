import sqlite3
import os
from datetime import datetime
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            email       TEXT,
            status      TEXT    NOT NULL DEFAULT 'active',  -- active | suspended | deleted
            traffic_limit_gb  REAL DEFAULT 0,   -- 0 = no limit
            traffic_used_rx   INTEGER DEFAULT 0, -- bytes received (download)
            traffic_used_tx   INTEGER DEFAULT 0, -- bytes sent (upload)
            expire_at   TEXT,                    -- ISO datetime or NULL
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            sub_token   TEXT    UNIQUE,          -- токен для URL подписки
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS traffic_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            rx_bytes    INTEGER DEFAULT 0,
            tx_bytes    INTEGER DEFAULT 0,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        -- Таблица онлайн-статуса: разделяется между процессами бота и веба
        CREATE TABLE IF NOT EXISTS online_cache (
            name        TEXT PRIMARY KEY,
            updated_at  TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


# ── Online-кэш (общий для bot и web процессов) ────────────────────────────

def set_online_users(names: set):
    """Записывает текущих online-пользователей в БД (перезаписывает целиком)."""
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM online_cache")
        conn.executemany(
            "INSERT OR REPLACE INTO online_cache (name, updated_at) VALUES (?, ?)",
            [(n, now) for n in names]
        )
        conn.commit()
    finally:
        conn.close()


def get_online_users_db(max_age_seconds: int = 90) -> set:
    """Возвращает имена online-пользователей из БД не старше max_age_seconds."""
    from datetime import timedelta
    threshold = (datetime.utcnow() - timedelta(seconds=max_age_seconds)).isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM online_cache WHERE updated_at > ?", (threshold,)
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


# ── CRUD пользователей ─────────────────────────────────────────────────────

def add_user(name: str, password: str, email: str = None,
             traffic_limit_gb: float = 0, expire_at: str = None,
             sub_token: str = None, notes: str = None) -> dict:
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO users
               (name, password, email, status, traffic_limit_gb,
                expire_at, created_at, updated_at, sub_token, notes)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (name, password, email, traffic_limit_gb,
             expire_at, now, now, sub_token, notes)
        )
        conn.commit()
        return get_user_by_name(name)
    finally:
        conn.close()


def get_user_by_name(name: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(uid: int) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_token(token: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE sub_token=?", (token,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_users(include_deleted: bool = False) -> list[dict]:
    conn = get_conn()
    try:
        if include_deleted:
            rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE status != 'deleted' ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_status(name: str, status: str):
    """status: active | suspended | deleted"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET status=?, updated_at=? WHERE name=?",
            (status, datetime.utcnow().isoformat(), name)
        )
        conn.commit()
    finally:
        conn.close()


def update_user_field(name: str, field: str, value):
    allowed = {"email", "traffic_limit_gb", "expire_at", "notes", "sub_token"}
    if field not in allowed:
        raise ValueError(f"Field {field!r} not updatable")
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE users SET {field}=?, updated_at=? WHERE name=?",
            (value, datetime.utcnow().isoformat(), name)
        )
        conn.commit()
    finally:
        conn.close()


def update_traffic(name: str, rx_bytes: int, tx_bytes: int):
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE users SET
                traffic_used_rx = traffic_used_rx + ?,
                traffic_used_tx = traffic_used_tx + ?,
                updated_at = ?
               WHERE name = ?""",
            (rx_bytes, tx_bytes, datetime.utcnow().isoformat(), name)
        )
        conn.commit()
    finally:
        conn.close()


def reset_traffic(name: str):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET traffic_used_rx=0, traffic_used_tx=0, updated_at=? WHERE name=?",
            (datetime.utcnow().isoformat(), name)
        )
        conn.commit()
    finally:
        conn.close()


def get_total_traffic() -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT SUM(traffic_used_rx) as rx, SUM(traffic_used_tx) as tx FROM users WHERE status != 'deleted'"
        ).fetchone()
        return {"rx": row["rx"] or 0, "tx": row["tx"] or 0}
    finally:
        conn.close()
