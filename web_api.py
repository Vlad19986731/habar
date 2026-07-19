"""HTTP API для Mini App: алерты, избранное, профиль, активность.

Безопасность: каждый запрос подписан Telegram initData (HMAC-SHA256 с токеном бота).
Подделать чужого пользователя невозможно — подпись проверяется на сервере.
Спецификация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import html
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

import db
from config import (ADMIN_IDS, API_PORT, BOT_TOKEN, BOT_USERNAME,
                    EARLY_BIRD_DAYS, REF_BONUS_DAYS)

log = logging.getLogger("dfbot.api")
_bot = None   # Bot для отправки в поддержку (ставится в start_api)

# initData считаем свежим сутки — дальше требуем переоткрыть приложение
MAX_AUTH_AGE = 86400


def verify_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram и возвращает объект пользователя."""
    if not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        if time.time() - int(data.get("auth_date", 0)) > MAX_AUTH_AGE:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def auth(request) -> dict | None:
    return verify_init_data(request.headers.get("X-Init-Data", ""))


async def touch(u: dict) -> None:
    """Отмечаем активность — это и есть «онлайн» для базы клиентов."""
    await db.upsert_user(u["id"], u.get("username"), u.get("first_name"),
                         u.get("last_name"), u.get("language_code"),
                         bool(u.get("is_premium")))


routes = web.RouteTableDef()


def need_auth(handler):
    async def wrapper(request):
        u = auth(request)
        if not u:
            return web.json_response({"error": "unauthorized"}, status=401)
        await touch(u)
        return await handler(request, u)
    wrapper.__name__ = handler.__name__
    return wrapper


@routes.get("/api/me")
@need_auth
async def api_me(request, u):
    """Состояние пользователя: слежка и избранное (сервер — источник правды)."""
    alerts = {}
    for aid, item_id, direction, threshold in await db.alerts_full(u["id"]):
        alerts[item_id] = {"d": "d" if direction == "below" else "u", "t": threshold}
    favs = [i[0] for i in await db.watch_list(u["id"])]
    prof = await db.get_df_player(u["id"])
    return web.json_response({
        "user": {"id": u["id"], "name": u.get("first_name"), "username": u.get("username")},
        "alerts": alerts,
        "favorites": favs,
        "profile": {"id": prof[0], "name": prof[1]} if prof and prof[0] else None,
    })


@routes.post("/api/alert")
@need_auth
async def api_alert(request, u):
    """Поставить слежку за ценой — прямо из приложения, без перехода в чат."""
    body = await request.json()
    item_id, d, thr = body.get("item_id"), body.get("dir"), body.get("threshold")
    if not item_id or d not in ("d", "u") or not isinstance(thr, (int, float)) or thr <= 0:
        return web.json_response({"error": "bad_request"}, status=400)
    item = await db.get_item(item_id)
    if not item:
        return web.json_response({"error": "item_not_found"}, status=404)
    direction = "below" if d == "d" else "above"
    existing = await db.alert_active_for_item(u["id"], item_id)
    if existing:
        return web.json_response({"ok": False, "reason": "already",
                                  "existing": {"d": "d" if existing[0] == "below" else "u",
                                               "t": existing[1]}})
    await db.alert_add(u["id"], item_id, direction, float(thr))
    await db.watch_add(u["id"], item_id)   # слежу = в избранном
    return web.json_response({"ok": True, "name": item[1]})


@routes.post("/api/alert/remove")
@need_auth
async def api_alert_remove(request, u):
    body = await request.json()
    item_id = body.get("item_id")
    if not item_id:
        return web.json_response({"error": "bad_request"}, status=400)
    n = await db.alerts_deactivate_item(u["id"], item_id)
    return web.json_response({"ok": True, "removed": n})


@routes.post("/api/fav")
@need_auth
async def api_fav(request, u):
    body = await request.json()
    item_id, on = body.get("item_id"), bool(body.get("on"))
    if not item_id:
        return web.json_response({"error": "bad_request"}, status=400)
    if on:
        await db.watch_add(u["id"], item_id)
    else:
        await db.alerts_deactivate_item(u["id"], item_id)  # убрал из избранного — слежка не нужна
        await db.watch_remove(u["id"], item_id)
    return web.json_response({"ok": True})


@routes.post("/api/profile")
@need_auth
async def api_profile(request, u):
    """Привязка игрока: сервер начнёт прогревать его профиль каждый час."""
    body = await request.json()
    pid, pname = body.get("player_id"), body.get("player_name")
    if pid:
        await db.link_df_player(u["id"], pid, pname or "")
    else:
        await db.link_df_player(u["id"], None, None)
    return web.json_response({"ok": True})


@routes.get("/api/stash")
@need_auth
async def api_stash(request, u):
    """Наша собственная история стэша (копится сервером, не зависит от чужого API)."""
    prof = await db.get_df_player(u["id"])
    if not prof or not prof[0]:
        return web.json_response({"error": "no_profile"}, status=404)
    series = await db.stash_series(prof[0], 30)
    return web.json_response({"values": [v for _, v in series],
                              "last": series[-1][1] if series else None})


@routes.post("/api/ping")
@need_auth
async def api_ping(request, u):
    """Приложение открыто — пользователь онлайн."""
    return web.json_response({"ok": True})


@routes.get("/api/pro")
@need_auth
async def api_pro(request, u):
    """Статус PRO + сколько ранних мест осталось + реф-ссылка."""
    st = await db.pro_status(u["id"])
    st["ref_link"] = f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}"
    st["ref_bonus_days"] = REF_BONUS_DAYS
    return web.json_response(st)


@routes.post("/api/pro/claim")
@need_auth
async def api_pro_claim(request, u):
    """Забрать ранний доступ: PRO на 30 дней, если места ещё есть."""
    st = await db.pro_status(u["id"])
    if st["pro"]:
        return web.json_response({"ok": False, "reason": "already_pro"})
    if await db.early_slots_left() <= 0:
        return web.json_response({"ok": False, "reason": "no_slots"})
    until = await db.add_pro_days(u["id"], EARLY_BIRD_DAYS, mark_early=True)
    return web.json_response({"ok": True, "pro_until": until})


@routes.post("/api/series")
@need_auth
async def api_series(request, u):
    """7-дневный график из НАШЕЙ истории цен — надёжно, без чужого API."""
    body = await request.json()
    item_id = body.get("item_id")
    if not item_id:
        return web.json_response({"error": "bad_request"}, status=400)
    rows = await db.item_series(item_id, 7)
    return web.json_response({"prices": [{"timestamp": ts, "priceAvg": price} for ts, price in rows]})


@routes.post("/api/support")
@need_auth
async def api_support(request, u):
    """Сообщение в поддержку — падает напрямую админам (владельцу) в Telegram."""
    body = await request.json()
    msg = (body.get("text") or "").strip()
    if not msg or len(msg) > 2000:
        return web.json_response({"error": "bad_request"}, status=400)
    uname = "@" + u["username"] if u.get("username") else "(без ника)"
    text = ("📩 <b>Поддержка Хабар</b>\n"
            "От: " + html.escape(u.get("first_name") or "") + " " + html.escape(uname) +
            " · id <code>" + str(u["id"]) + "</code>\n\n" + html.escape(msg) +
            "\n\n<i>↩️ Ответь на это сообщение — я перешлю ответ пользователю.</i>")
    ok = False
    if _bot:
        for aid in ADMIN_IDS:
            try:
                await _bot.send_message(aid, text)
                ok = True
            except Exception:
                log.exception("Поддержка: не смог отправить админу %s", aid)
    return web.json_response({"ok": ok})


async def start_api(bot=None) -> None:
    global _bot
    _bot = bot
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", API_PORT)
    await site.start()
    log.info("API слушает 127.0.0.1:%s", API_PORT)
