"""SQLite: пользователи, предметы, вотчлист, алерты и НАША история цен.

История цен копится с первого дня работы бота — это наш главный актив
на случай, если внешний API изменится или закроется.
"""
from datetime import datetime, timedelta

import aiosqlite

from config import DB_PATH, EARLY_BIRD_LIMIT, REF_BONUS_DAYS

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
-- своя история стоимости стэшей (прогрев профилей)
CREATE TABLE IF NOT EXISTS stash_history (
    player_id TEXT NOT NULL,
    ts        TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (player_id, ts)
);
"""


# колонки базы клиентов, которых может не быть в старой базе
_USER_COLUMNS = [
    ("first_name", "TEXT"),
    ("last_name", "TEXT"),
    ("lang", "TEXT"),
    ("is_premium", "INTEGER DEFAULT 0"),
    ("last_seen", "TEXT"),
    ("interactions", "INTEGER DEFAULT 0"),
    ("df_player_id", "TEXT"),
    ("df_player_name", "TEXT"),
    ("blocked", "INTEGER DEFAULT 0"),
    # --- PRO и рефералы ---
    ("pro_until", "TEXT"),          # до какой даты активен PRO (NULL = нет)
    ("early_bird", "INTEGER DEFAULT 0"),   # получил ли ранний доступ (из первых 500)
    ("ref_by", "INTEGER"),         # кто пригласил (tg_id), NULL если сам
    ("ref_credited", "INTEGER DEFAULT 0"), # засчитан ли этот юзер пригласившему (антифрод)
    ("ref_count", "INTEGER DEFAULT 0"),    # сколько активных друзей привёл
    ("digest_off", "INTEGER DEFAULT 0"),   # отписался от ежедневного дайджеста «Выгода дня»
]


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")     # устойчивость к сбоям питания + конкурентное чтение
        await db.execute("PRAGMA synchronous=NORMAL")   # безопасно с WAL, быстро
        # порядок важен: таблицы -> миграция колонок -> индексы по новым колонкам
        await db.executescript(SCHEMA)
        cur = await db.execute("PRAGMA table_info(users)")
        have = {r[1] for r in await cur.fetchall()}
        for name, decl in _USER_COLUMNS:
            if name not in have:
                await db.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_seen ON users(last_seen)")
        await db.commit()


async def upsert_user(tg_id: int, username: str | None, first_name: str | None = None,
                      last_name: str | None = None, lang: str | None = None,
                      is_premium: bool = False) -> None:
    """Создаёт/обновляет карточку клиента и отмечает активность (last_seen)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(tg_id, username, first_name, last_name, lang, is_premium, last_seen, interactions) "
            "VALUES(?,?,?,?,?,?,datetime('now'),1) "
            "ON CONFLICT(tg_id) DO UPDATE SET "
            "  username=excluded.username,"
            "  first_name=COALESCE(excluded.first_name, users.first_name),"
            "  last_name=COALESCE(excluded.last_name, users.last_name),"
            "  lang=COALESCE(excluded.lang, users.lang),"
            "  is_premium=excluded.is_premium,"
            "  last_seen=datetime('now'),"
            "  interactions=users.interactions+1,"
            "  blocked=0",
            (tg_id, username, first_name, last_name, lang, int(is_premium)),
        )
        await db.commit()


async def mark_blocked(tg_id: int) -> None:
    """Пользователь заблокировал бота — не шлём ему пуши."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET blocked=1 WHERE tg_id=?", (tg_id,))
        await db.commit()


# ---------- дайджест «Выгода дня» ----------

async def digest_recipients() -> list[int]:
    """Кому шлём дайджест: не заблокировали бота, не отписались, активны за 30 дней."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tg_id FROM users WHERE blocked=0 AND COALESCE(digest_off,0)=0 "
            "AND last_seen > datetime('now','-30 days')")
        return [r[0] for r in await cur.fetchall()]


async def get_digest_off(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(digest_off,0) FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False


async def set_digest_off(tg_id: int, off: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET digest_off=? WHERE tg_id=?", (1 if off else 0, tg_id))
        await db.commit()


async def link_df_player(tg_id: int, player_id: str, player_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET df_player_id=?, df_player_name=? WHERE tg_id=?",
                         (player_id, player_name, tg_id))
        await db.commit()


async def user_stats() -> dict:
    """Сводка по базе клиентов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async def one(sql):
            cur = await db.execute(sql)
            return (await cur.fetchone())[0]
        return {
            "total": await one("SELECT COUNT(*) FROM users"),
            "online": await one("SELECT COUNT(*) FROM users WHERE last_seen > datetime('now','-5 minutes')"),
            "today": await one("SELECT COUNT(*) FROM users WHERE last_seen > datetime('now','-1 day')"),
            "week": await one("SELECT COUNT(*) FROM users WHERE last_seen > datetime('now','-7 days')"),
            "new_today": await one("SELECT COUNT(*) FROM users WHERE created_at > datetime('now','-1 day')"),
            "linked": await one("SELECT COUNT(*) FROM users WHERE df_player_id IS NOT NULL"),
            "blocked": await one("SELECT COUNT(*) FROM users WHERE blocked=1"),
            "with_alerts": await one("SELECT COUNT(DISTINCT tg_id) FROM alerts WHERE active=1"),
            "with_favs": await one("SELECT COUNT(DISTINCT tg_id) FROM watchlist"),
        }


# ---------- PRO ----------

async def pro_status(tg_id: int) -> dict:
    """Статус PRO пользователя + сколько ранних мест осталось."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT pro_until, early_bird, ref_count FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE early_bird=1")
        early_taken = (await cur.fetchone())[0]
    pro_until, early, ref_count = (row or (None, 0, 0))
    active = False
    if pro_until:
        try:
            active = datetime.fromisoformat(pro_until) > datetime.utcnow()
        except ValueError:
            pass
    return {
        "pro": active,
        "pro_until": pro_until if active else None,
        "early_bird": bool(early),
        "ref_count": ref_count or 0,
        "slots_left": max(0, EARLY_BIRD_LIMIT - early_taken),
    }


async def add_pro_days(tg_id: int, days: int, mark_early: bool = False) -> str:
    """Продлевает PRO на N дней (от текущей даты окончания или от сейчас)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT pro_until FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        base = datetime.utcnow()
        if row and row[0]:
            try:
                cur_until = datetime.fromisoformat(row[0])
                if cur_until > base:
                    base = cur_until
            except ValueError:
                pass
        new_until = (base + timedelta(days=days)).isoformat()
        if mark_early:
            await db.execute("UPDATE users SET pro_until=?, early_bird=1 WHERE tg_id=?", (new_until, tg_id))
        else:
            await db.execute("UPDATE users SET pro_until=? WHERE tg_id=?", (new_until, tg_id))
        await db.commit()
        return new_until


async def early_slots_left() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE early_bird=1")
        return max(0, EARLY_BIRD_LIMIT - (await cur.fetchone())[0])


# ---------- рефералы ----------

async def set_referrer(tg_id: int, ref_by: int) -> bool:
    """Запоминает, кто пригласил (только для новых, без реферера, не самого себя)."""
    if ref_by == tg_id:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT ref_by FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row and row[0]:
            return False  # реферер уже есть
        cur = await db.execute("SELECT 1 FROM users WHERE tg_id=?", (ref_by,))
        if not await cur.fetchone():
            return False  # пригласившего нет в базе
        await db.execute("UPDATE users SET ref_by=? WHERE tg_id=?", (ref_by, tg_id))
        await db.commit()
        return True


async def pending_referrals() -> list[tuple]:
    """Друзья, которых пора засчитать: привёл реферер, юзер активен, ещё не засчитан.

    Активен = сделал 3+ действия (антифрод против пустых аккаунтов).
    Возвращает (friend_id, referrer_id).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tg_id, ref_by FROM users "
            "WHERE ref_by IS NOT NULL AND ref_credited=0 AND interactions >= 3 AND blocked=0")
        return await cur.fetchall()


async def mark_referral_credited(friend_id: int, referrer_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET ref_credited=1 WHERE tg_id=?", (friend_id,))
        await db.execute("UPDATE users SET ref_count=ref_count+1 WHERE tg_id=?", (referrer_id,))
        await db.commit()


async def recent_users(limit: int = 10) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tg_id, username, first_name, last_seen, interactions, df_player_name "
            "FROM users ORDER BY last_seen DESC LIMIT ?", (limit,))
        return await cur.fetchall()


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


async def all_items() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM items")
        return await cur.fetchall()


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


async def alert_active_for_item(tg_id: int, item_id: str):
    """Активная слежка юзера за предметом: (direction, threshold) или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT direction, threshold FROM alerts WHERE tg_id=? AND item_id=? AND active=1 LIMIT 1",
            (tg_id, item_id),
        )
        return await cur.fetchone()


async def alerts_deactivate_item(tg_id: int, item_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE alerts SET active=0 WHERE tg_id=? AND item_id=? AND active=1",
            (tg_id, item_id),
        )
        await db.commit()
        return cur.rowcount


async def alert_add(tg_id: int, item_id: str, direction: str, threshold: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts(tg_id, item_id, direction, threshold) VALUES(?,?,?,?)",
            (tg_id, item_id, direction, threshold),
        )
        await db.commit()


async def alerts_full(tg_id: int) -> list[tuple]:
    """(alert_id, item_id, direction, threshold) — для API."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, item_id, direction, threshold FROM alerts WHERE tg_id=? AND active=1",
            (tg_id,))
        return await cur.fetchall()


async def get_df_player(tg_id: int) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT df_player_id, df_player_name FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()


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


async def tracked_players() -> list[str]:
    """Игроки, привязанные пользователями — их профили прогреваем."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT df_player_id FROM users WHERE df_player_id IS NOT NULL")
        return [r[0] for r in await cur.fetchall()]


async def stash_add(player_id: str, ts: str, value: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO stash_history(player_id, ts, value) VALUES(?,?,?)",
                         (player_id, ts, value))
        await db.commit()


async def stash_series(player_id: str, days: int = 30) -> list[tuple]:
    """Наша собственная история стэша: [(ts, value), ...]"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ts, value FROM stash_history WHERE player_id=? "
            "AND ts > datetime('now', ?) ORDER BY ts", (player_id, f"-{days} days"))
        return await cur.fetchall()


async def all_item_ids() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM items")
        return [r[0] for r in await cur.fetchall()]


async def history_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM price_history")
        return (await cur.fetchone())[0]


async def history_rows_7d() -> list[tuple]:
    """(item_id, ts, price) за 7 дней, отсортировано — сырьё для флипов."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT item_id, ts, price FROM price_history "
            "WHERE ts > datetime('now','-7 days') ORDER BY item_id, ts")
        return await cur.fetchall()


async def item_series(item_id: str, days: int = 7) -> list[tuple]:
    """Наша история цен одного предмета за N дней: [(ts, price), ...] — для графика в карточке."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ts, price FROM price_history WHERE item_id=? "
            "AND ts > datetime('now', ?) ORDER BY ts", (item_id, f"-{days} days"))
        return await cur.fetchall()


async def history_add_many(rows: list[tuple]) -> None:
    """rows: [(item_id, ts, price), ...] — массовая вставка. Пишем ТОЛЬКО реальные цены (>0, не NaN)."""
    clean = [(i, t, p) for (i, t, p) in rows
             if isinstance(p, (int, float)) and p == p and p > 0]
    if not clean:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO price_history(item_id, ts, price) VALUES(?,?,?)", clean)
        await db.commit()


async def history_cleanup(days: int = 90) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM price_history WHERE ts < datetime('now', ?)", (f"-{days} days",))
        await db.execute("DELETE FROM stash_history WHERE ts < datetime('now','-180 days')")
        await db.commit()


async def history_add(item_id: str, ts: str, price: float) -> None:
    if not (isinstance(price, (int, float)) and price == price and price > 0):
        return                                          # только реальные цены (>0, не NaN)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO price_history(item_id, ts, price) VALUES(?,?,?)",
            (item_id, ts, price),
        )
        await db.commit()
