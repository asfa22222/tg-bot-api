"""SQLite database layer for persistent storage."""

import aiosqlite

from config import DB_PATH

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _init_tables(_db)
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _init_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            label TEXT,
            is_active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            is_admin INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            devin_session_id TEXT NOT NULL,
            devin_url TEXT,
            tg_user_id INTEGER NOT NULL,
            tg_chat_id INTEGER NOT NULL DEFAULT 0,
            title TEXT,
            status TEXT DEFAULT 'running',
            last_status TEXT DEFAULT '',
            last_event_id TEXT DEFAULT '',
            polling_active INTEGER DEFAULT 1,
            api_key_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS active_session (
            tg_user_id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS key_rotation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_key_idx INTEGER DEFAULT 0
        );
    """)
    await db.execute(
        "INSERT OR IGNORE INTO key_rotation (id, current_key_idx) VALUES (1, 0)"
    )
    await db.commit()


# --- API Keys ---

async def add_api_key(key: str, label: str | None = None) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO api_keys (key, label) VALUES (?, ?)", (key, label)
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def remove_api_key(key_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()
    return cursor.rowcount > 0


async def list_api_keys() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, key, label, is_active, added_at FROM api_keys ORDER BY id"
    )
    return [dict(r) for r in rows]


async def get_active_keys() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, key FROM api_keys WHERE is_active = 1 ORDER BY id"
    )
    return [dict(r) for r in rows]


async def deactivate_key(key_id: int) -> None:
    db = await get_db()
    await db.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    await db.commit()


async def activate_key(key_id: int) -> None:
    db = await get_db()
    await db.execute("UPDATE api_keys SET is_active = 1 WHERE id = ?", (key_id,))
    await db.commit()


async def get_current_key_index() -> int:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT current_key_idx FROM key_rotation WHERE id = 1"
    )
    return row[0]["current_key_idx"] if row else 0


async def set_current_key_index(idx: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE key_rotation SET current_key_idx = ? WHERE id = 1", (idx,)
    )
    await db.commit()


# --- Whitelist ---

async def add_to_whitelist(
    tg_id: int, username: str | None = None, is_admin: bool = False
) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO whitelist (tg_id, username, is_admin) VALUES (?, ?, ?)",
        (tg_id, username, int(is_admin)),
    )
    await db.commit()


async def remove_from_whitelist(tg_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM whitelist WHERE tg_id = ?", (tg_id,))
    await db.commit()
    return cursor.rowcount > 0


async def is_whitelisted(tg_id: int) -> bool:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT 1 FROM whitelist WHERE tg_id = ?", (tg_id,)
    )
    return len(rows) > 0


async def is_admin(tg_id: int) -> bool:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT is_admin FROM whitelist WHERE tg_id = ?", (tg_id,)
    )
    return len(rows) > 0 and rows[0]["is_admin"] == 1


async def get_whitelist_count() -> int:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM whitelist")
    return rows[0]["cnt"]


async def list_whitelist() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT tg_id, username, is_admin, added_at FROM whitelist ORDER BY added_at"
    )
    return [dict(r) for r in rows]


# --- Sessions ---

async def create_session_record(
    devin_session_id: str,
    devin_url: str,
    tg_user_id: int,
    tg_chat_id: int,
    title: str | None,
    api_key_id: int,
) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO sessions (devin_session_id, devin_url, tg_user_id, tg_chat_id, title, api_key_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (devin_session_id, devin_url, tg_user_id, tg_chat_id, title, api_key_id),
    )
    session_row_id = cursor.lastrowid
    await db.execute(
        "INSERT OR REPLACE INTO active_session (tg_user_id, session_id) VALUES (?, ?)",
        (tg_user_id, session_row_id),
    )
    await db.commit()
    return session_row_id  # type: ignore[return-value]


async def get_active_session(tg_user_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT s.* FROM sessions s
           JOIN active_session a ON s.id = a.session_id
           WHERE a.tg_user_id = ?""",
        (tg_user_id,),
    )
    return dict(rows[0]) if rows else None


async def list_user_sessions(tg_user_id: int, limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, devin_session_id, devin_url, title, status, created_at
           FROM sessions WHERE tg_user_id = ? ORDER BY created_at DESC LIMIT ?""",
        (tg_user_id, limit),
    )
    return [dict(r) for r in rows]


async def get_session_by_id(session_row_id: int, tg_user_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM sessions WHERE id = ? AND tg_user_id = ?",
        (session_row_id, tg_user_id),
    )
    return dict(rows[0]) if rows else None


async def delete_session(session_row_id: int, tg_user_id: int) -> bool:
    db = await get_db()
    # Remove from active_session if it's the active one
    await db.execute(
        "DELETE FROM active_session WHERE tg_user_id = ? AND session_id = ?",
        (tg_user_id, session_row_id),
    )
    cursor = await db.execute(
        "DELETE FROM sessions WHERE id = ? AND tg_user_id = ?",
        (session_row_id, tg_user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_active_session(tg_user_id: int, session_row_id: int) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO active_session (tg_user_id, session_id) VALUES (?, ?)",
        (tg_user_id, session_row_id),
    )
    await db.commit()


async def update_session_status(session_row_id: int, status: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET status = ? WHERE id = ?", (status, session_row_id)
    )
    await db.commit()


async def get_polling_sessions() -> list[dict]:
    """Get all sessions that should be polled for updates."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, devin_session_id, devin_url, tg_user_id, tg_chat_id,
                  title, status, last_status, last_event_id
           FROM sessions
           WHERE polling_active = 1"""
    )
    return [dict(r) for r in rows]


async def update_session_last_status(session_row_id: int, last_status: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET last_status = ? WHERE id = ?",
        (last_status, session_row_id),
    )
    await db.commit()


async def update_session_last_event_id(session_row_id: int, event_id: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET last_event_id = ? WHERE id = ?",
        (event_id, session_row_id),
    )
    await db.commit()


async def stop_polling_session(session_row_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET polling_active = 0 WHERE id = ?", (session_row_id,)
    )
    await db.commit()
