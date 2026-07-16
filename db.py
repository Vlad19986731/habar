"""SQLite: пользователи, предметы, вотчлист, алерты и НАША история цен.

История цен копится с первого дня работы бота — это наш главный актив
на случай, если внешний API изменится или закроется.
"""
import aiosqlite

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    name_lc  TEXT NOT NULL,
    icon_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name_lc);
CREATE TABLE IF NOT EXISTS watchlist (
    tg_id   INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (tg_id, item_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id     INTEGER NOT NULL,
    item_id   TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('below','above')),
    threshold REAL NOT NULL,
    active    INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS price_history (
    item_id TEXT NOT NULL,
    ts      TEXT NOT NULL,
    price   REAL NOT NULL,
    PRIMARY KEY (item_id, ts)
);
"""


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_user(tg_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(tg_id, username) VALUES(?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username",
            (tg_id, username),
        )
        await db.commit()


async def replace_items(items: list[dict]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO items(id, name, name_lc, icon_url) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, name_lc=excluded.name_lc, icon_url=excluded.icon_url",
            [(i["id"], i["name"], i["name"].lower(), i.get("iconUrl", "")) for i in items],
        )
        await db.commit()
    return len(items)


async def search_items(query_lc: str, limit: int = 8) -> list[tuple]:
    """Поиск по подстроке; сначала — совпадения с начала слова."""
    like = f"%{query_lc}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, name FROM items WHERE name_lc LIKE ? "
            "ORDER BY CASE WHEN name_lc LIKE ? THEN 0 ELSE 1 END, length(name) LIMIT ?",
            (like, f"{query_lc}%", limit),
        )
        return await cur.fetchall()


async def get_item(item_id: str) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, icon_url FROM items WHERE id=?", (item_id,))
        return await cur.fetchone()


async def items_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM items")
        return (await cur.fetchone())[0]


# ---------- вотчлист ----------

async def watch_add(tg_id: int, item_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?)", (tg_id, item_id))
        await db.commit()


async def watch_remove(tg_id: int, item_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM watchlist WHERE tg_id=? AND item_id=?", (tg_id, item_id))
        await db.commit()


async def watch_list(tg_id: int) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT i.id, i.name FROM watchlist w JOIN items i ON i.id=w.item_id WHERE w.tg_id=? ORDER BY i.name",
            (tg_id,),
        )
        return await cur.fetchall()


async def is_watched(tg_id: int, item_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM watchlist WHERE tg_id=? AND item_id=?", (tg_id, item_id))
        return await cur.fetchone() is not None


# ---------- алерты ----------

async def alert_exists(tg_id: int, item_id: str, direction: str, threshold: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM alerts WHERE tg_id=? AND item_id=? AND direction=? AND threshold=? AND active=1",
            (tg_id, item_id, direction, threshold),
        )
        return await cur.fetchone() is not None


async def alert_add(tg_id: int, item_id: str, direction: str, threshold: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts(tg_id, item_id, direction, threshold) VALUES(?,?,?,?)",
            (tg_id, item_id, direction, threshold),
        )
        await db.commit()


async def alerts_for_user(tg_id: int) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT a.id, i.name, a.direction, a.threshold FROM alerts a "
            "JOIN items i ON i.id=a.item_id WHERE a.tg_id=? AND a.active=1 ORDER BY a.id",
            (tg_id,),
        )
        return await cur.fetchall()


async def alert_delete(alert_id: int, tg_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE id=? AND tg_id=?", (alert_id, tg_id))
        await db.commit()


async def active_alerts() -> list[tuple]:
    """(alert_id, tg_id, item_id, item_name, direction, threshold)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT a.id, a.tg_id, a.item_id, i.name, a.direction, a.threshold "
            "FROM alerts a JOIN items i ON i.id=a.item_id WHERE a.active=1"
        )
        return await cur.fetchall()


async def alert_deactivate(alert_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alerts SET active=0 WHERE id=?", (alert_id,))
        await db.commit()


# ---------- история цен ----------

async def tracked_item_ids() -> list[str]:
    """Предметы, за которыми следим: вотчлисты + активные алерты."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT item_id FROM watchlist "
            "UNION SELECT DISTINCT item_id FROM alerts WHERE active=1"
        )
        return [r[0] for r in await cur.fetchall()]


async def history_add(item_id: str, ts: str, price: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO price_history(item_id, ts, price) VALUES(?,?,?)",
            (item_id, ts, price),
        )
        await db.commit()
