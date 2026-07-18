"""DF Аукцион — Telegram-бот экономики Delta Force. MVP.

Запуск:  python bot.py
Требует: .env с BOT_TOKEN=...
"""
import asyncio
import html as html_mod
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import api
import db
from aliases import normalize_query
from config import (ADMIN_IDS, API_ENABLED, API_POLITE_DELAY, BOT_TOKEN,
                    COLLECT_EVERY_MIN, DATA_DIR, NEWS_GIT_PUSH, WARM_EVERY_MIN,
                    WEB_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dfbot")

router = Router()

BARS = "▁▂▃▄▅▆▇█"
APP_URL = "https://habargg.ru/"


class TrackUserMiddleware(BaseMiddleware):
    """Каждое действие любого пользователя обновляет карточку клиента и last_seen."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u:
            try:
                await db.upsert_user(u.id, u.username, u.first_name, u.last_name,
                                     u.language_code, bool(getattr(u, "is_premium", False)))
            except Exception:
                log.exception("Не смог обновить карточку пользователя %s", u.id)
        return await handler(event, data)


# ---------- helpers ----------

def fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def sparkline(values: list[float], width: int = 16) -> str:
    if len(values) < 2:
        return ""
    step = max(1, len(values) // width)
    pts = values[::step][:width]
    lo, hi = min(pts), max(pts)
    if hi - lo < 1e-9:
        return BARS[3] * len(pts)
    return "".join(BARS[int((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in pts)


async def build_card(item_id: str, item_name: str) -> tuple[str, list[float]]:
    """Текст карточки цены + значения для истории."""
    price_data = await api.get_price(item_id)
    series = await api.get_series(item_id, days=7)
    avgs = [p["priceAvg"] for p in series if p.get("priceAvg")]

    if not price_data:
        return f"❌ Не удалось получить цену для «{item_name}». Попробуй позже.", []

    price = price_data["price"]
    ref = price_data.get("referencePrice")

    lines = [f"<b>{item_name}</b>", "", f"💰 Сейчас: <b>{fmt(price)}</b>"]
    if ref:
        lines.append(f"🏷 Референс: {fmt(ref)}")

    if avgs:
        # Δ за 24ч: сравниваем с точкой ~24 часа назад
        if len(avgs) >= 25:
            old = avgs[-25]
            d24 = (price - old) / old * 100 if old else 0
            arrow = "🔺" if d24 > 0 else "🔻" if d24 < 0 else "▪️"
            lines.append(f"{arrow} 24ч: {d24:+.1f}%")
        first = avgs[0]
        if first:
            d7 = (price - first) / first * 100
            arrow = "🔺" if d7 > 0 else "🔻" if d7 < 0 else "▪️"
            lines.append(f"{arrow} 7д: {d7:+.1f}%")

        lines.append(f"\n<code>{sparkline(avgs)}</code> 7 дней")

        avg7 = sum(avgs) / len(avgs)
        dev = (price - avg7) / avg7 * 100 if avg7 else 0
        if dev <= -5:
            lines.append(f"🟢 Ниже средней за 7д на {abs(dev):.0f}% — возможная точка входа")
        elif dev >= 5:
            lines.append(f"🔴 Выше средней за 7д на {dev:.0f}% — дорого")
        else:
            lines.append("⚪️ Около средней за 7 дней")

    lines.append(f"\n<i>Аукцион · глобал · {datetime.now(timezone.utc).strftime('%H:%M')} UTC</i>")
    return "\n".join(lines), avgs


def card_kb(item_id: str, watched: bool, alert=None) -> InlineKeyboardMarkup:
    star = "✓ В избранном" if watched else "⭐ В избранное"
    star_cb = f"unwatch:{item_id}" if watched else f"watch:{item_id}"
    rows = [[InlineKeyboardButton(text=star, callback_data=star_cb),
             InlineKeyboardButton(text="🔄 Обновить", callback_data=f"card:{item_id}")]]
    if alert:
        sign = "≤" if alert[0] == "below" else "≥"
        rows.append([InlineKeyboardButton(
            text=f"🔕 Отменить слежку ({sign} {fmt(alert[1])})", callback_data=f"alrm:{item_id}")])
    else:
        rows.append([InlineKeyboardButton(text="📉 Подешевеет", callback_data=f"alset:b:{item_id}"),
                     InlineKeyboardButton(text="📈 Подорожает", callback_data=f"alset:a:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- FSM ----------

class AlertForm(StatesGroup):
    threshold = State()


# ---------- handlers ----------

async def send_alert_presets(m: Message, user_id: int, item_id: str, direction: str):
    """Предлагает готовые пороги цены кнопками — без ручного ввода."""
    item = await db.get_item(item_id)
    if not item:
        await m.answer("Предмет не найден 😕")
        return
    existing = await db.alert_active_for_item(user_id, item_id)
    if existing:
        ex_word = "подешевеет до" if existing[0] == "below" else "подорожает до"
        await m.answer(
            f"⚠️ Ты уже следишь за <b>{item[1]}</b>: {ex_word} <b>{fmt(existing[1])} ₮</b>.\n"
            f"Одна слежка на предмет — сначала отмени текущую:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔕 Отменить слежку", callback_data=f"alrm:{item_id}")]]),
        )
        return
    p = await api.get_price(item_id)
    if not p:
        await m.answer("Не удалось получить цену, попробуй позже 😕")
        return
    price = p["price"]
    d = "b" if direction == "below" else "a"
    if direction == "below":
        t1, t2 = price * 0.95, price * 0.90
        word, s1, s2 = "подешевеет до", "−5%", "−10%"
    else:
        t1, t2 = price * 1.05, price * 1.10
        word, s1, s2 = "подорожает до", "+5%", "+10%"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s1} · {fmt(t1)} ₮", callback_data=f"mkal:{d}:{item_id}:{int(t1)}")],
        [InlineKeyboardButton(text=f"{s2} · {fmt(t2)} ₮", callback_data=f"mkal:{d}:{item_id}:{int(t2)}")],
        [InlineKeyboardButton(text="✍️ Указать свою цену", callback_data=f"alcust:{d}:{item_id}")],
    ])
    await m.answer(
        f"🔔 <b>{item[1]}</b>\nСейчас: <b>{fmt(price)} ₮</b>\n\n"
        f"Сообщу, когда {word}:",
        reply_markup=kb,
    )


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    args = command.args or ""
    if args.startswith("rm_"):
        item_id = args[3:]
        item = await db.get_item(item_id)
        n = await db.alerts_deactivate_item(m.from_user.id, item_id)
        name = item[1] if item else "предмет"
        if n:
            await m.answer(f"🔕 Слежка за <b>{name}</b> отменена.")
        else:
            await m.answer(f"За <b>{name}</b> активной слежки не было — всё чисто.")
        return
    # реферальная ссылка: ?start=ref_<id>
    if args.startswith("ref_") and args[4:].isdigit():
        ref_by = int(args[4:])
        if await db.set_referrer(m.from_user.id, ref_by):
            await m.answer("🎁 Ты пришёл по приглашению! Осмотрись — а когда освоишься, "
                           "твой друг получит бонус к PRO.")
    if args.startswith("al_"):
        parts = args.split("_")
        if len(parts) >= 3 and parts[1] in ("d", "u"):
            direction = "below" if parts[1] == "d" else "above"
            item_id = parts[2]
            # порог выбран прямо в Mini App -> создаём уведомление сразу
            if len(parts) >= 4 and parts[3].isdigit():
                item = await db.get_item(item_id)
                if item:
                    threshold = float(parts[3])
                    word = "подешевеет до" if direction == "below" else "подорожает до"
                    existing = await db.alert_active_for_item(m.from_user.id, item_id)
                    if existing:
                        ex_word = "подешевеет до" if existing[0] == "below" else "подорожает до"
                        await m.answer(
                            f"⚠️ Ты уже следишь за <b>{item[1]}</b>: {ex_word} <b>{fmt(existing[1])} ₮</b>.\n"
                            f"Одна слежка на предмет — сначала отмени текущую:",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="🔕 Отменить слежку", callback_data=f"alrm:{item_id}")]]),
                        )
                        return
                    await db.alert_add(m.from_user.id, item_id, direction, threshold)
                    await db.watch_add(m.from_user.id, item_id)
                    await m.answer(
                        f"✅ Принято! Сообщу, когда <b>{item[1]}</b> {word} <b>{fmt(threshold)} ₮</b>.\n"
                        f"⭐ Предмет добавлен в избранное.\n"
                        f"<i>Пуш придёт прямо сюда. Твои уведомления: /alerts</i>"
                    )
                    return
            await send_alert_presets(m, m.from_user.id, item_id, direction)
            return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton(text="🔍 Как найти цену", callback_data="howto"),
         InlineKeyboardButton(text="⏳ До вайпа", callback_data="wipebtn")],
    ])
    await m.answer(
        "🎯 <b>ХАБАР</b> — твой штаб по Delta Force\n\n"
        "Не просто цены — здесь всё для оператора:\n\n"
        "💰 Цены аукциона в реальном времени + где выгодно купить и продать\n"
        "📰 Новости и патчи игры — на русском\n"
        "⏳ Таймер до вайпа сезона\n"
        "🎒 Стоимость твоего инвентаря и статистика\n"
        "🔔 Слежка за ценами — сообщим, когда предмет подешевеет\n\n"
        "И это только старт — дальше больше 🚀\n\n"
        "<i>Всё внутри приложения — жми «Открыть приложение» 👇\n"
        "А сюда будут приходить уведомления о ценах.</i>",
        reply_markup=kb,
    )


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    """Админ-панель: сводка по базе клиентов."""
    if m.from_user.id not in ADMIN_IDS:
        return
    s = await db.user_stats()
    lines = [
        "📊 <b>База клиентов</b>\n",
        f"👥 Всего: <b>{s['total']}</b>",
        f"🟢 Онлайн сейчас: <b>{s['online']}</b> <i>(активность за 5 мин)</i>",
        f"📅 Активны за сутки: <b>{s['today']}</b> · за неделю: <b>{s['week']}</b>",
        f"✨ Новых за сутки: <b>{s['new_today']}</b>",
        "",
        f"🎮 Привязали игру: <b>{s['linked']}</b>",
        f"🔔 Следят за ценой: <b>{s['with_alerts']}</b>",
        f"⭐ С избранным: <b>{s['with_favs']}</b>",
        f"🚫 Заблокировали бота: <b>{s['blocked']}</b>",
        "\n<b>Последние активные:</b>",
    ]
    for tg_id, uname, fname, seen, cnt, df_name in await db.recent_users(8):
        who = ("@" + uname) if uname else (fname or str(tg_id))
        game = f" · 🎮 {df_name}" if df_name else ""
        lines.append(f"• {who} — {seen or '—'} <i>({cnt} действий)</i>{game}")
    await m.answer("\n".join(lines))


@router.message(Command("clear"))
async def cmd_clear(m: Message):
    """Убрать последнюю карточку из чата вручную."""
    await sweep_ephemeral(m.bot, m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass


@router.message(Command("wipe"))
async def cmd_wipe(m: Message):
    season = await api.get_current_season()
    if not season:
        await m.answer("Не удалось получить данные сезона 😕")
        return
    end = datetime.fromisoformat(season["endedAt"].replace("Z", "+00:00"))
    left = end - datetime.now(timezone.utc)
    await m.answer(
        f"⏳ Сезон {season.get('number')} «{season.get('name')}»\n"
        f"До вайпа: <b>{left.days} дн.</b> ({end.strftime('%d.%m.%Y')})\n\n"
        f"<i>Перед вайпом цены на гир обычно падают — все распродаются. "
        f"После вайпа взлетают материалы и базовый гир.</i>"
    )


@router.message(Command("watch"))
async def cmd_watch(m: Message):
    """Избранное — в приложении."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Открыть избранное",
                             web_app=WebAppInfo(url=APP_URL + "#fav"))]])
    await m.answer("⭐ Избранное и слежка за ценами — в приложении:", reply_markup=kb)


@router.message(Command("alerts"))
async def cmd_alerts(m: Message):
    rows = await db.alerts_for_user(m.from_user.id)
    if not rows:
        await m.answer("Уведомлений нет. Найди предмет и нажми «📉 Подешевеет» или «📈 Подорожает».")
        return
    kb, lines = [], ["🔔 <b>Твои уведомления</b>\n"]
    for aid, name, direction, threshold in rows:
        sign = "подешевеет до" if direction == "below" else "подорожает до"
        lines.append(f"#{aid} · {name} — {sign} {fmt(threshold)} ₮")
        kb.append([InlineKeyboardButton(text=f"❌ Удалить #{aid}", callback_data=f"delalert:{aid}")])
    await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.message(AlertForm.threshold)
async def alert_threshold(m: Message, state: FSMContext):
    text = m.text.strip().replace(" ", "").lstrip("+")
    try:
        threshold = float(text)
    except ValueError:
        await m.answer("Нужно просто число, например <code>150000</code>.")
        return
    data = await state.get_data()
    direction = data.get("direction", "below")
    word = "подешевеет до" if direction == "below" else "подорожает до"
    if await db.alert_active_for_item(m.from_user.id, data["item_id"]):
        await state.clear()
        await m.answer("⚠️ Ты уже следишь за этим предметом — одна слежка на предмет. Список: /alerts")
        return
    await db.alert_add(m.from_user.id, data["item_id"], direction, threshold)
    await db.watch_add(m.from_user.id, data["item_id"])
    await state.clear()
    await m.answer(
        f"✅ Принято! Сообщу, когда <b>{data['item_name']}</b> {word} <b>{fmt(threshold)} ₮</b>.\n"
        f"⭐ Предмет добавлен в избранное."
    )


# «одно активное сообщение»: чат не засоряется карточками.
# При новом запросе удаляем прошлую карточку и прошлый запрос пользователя.
_ephemeral: dict[int, list[int]] = {}


async def sweep_ephemeral(bot: Bot, chat_id: int) -> None:
    for mid in _ephemeral.pop(chat_id, []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


def remember_ephemeral(chat_id: int, *msg_ids) -> None:
    _ephemeral[chat_id] = [m for m in msg_ids if m]


@router.message(F.text & ~F.text.startswith("/"))
async def search(m: Message):
    """Поиск переехал в приложение — мягко направляем туда."""
    await sweep_ephemeral(m.bot, m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔍 Найти в приложении",
                             web_app=WebAppInfo(url=APP_URL + "#market"))]])
    sent = await m.answer(
        "🔍 Поиск цен, графики и слежка — в приложении.\n"
        "Там весь рынок и история цен в пару тапов 👇",
        reply_markup=kb,
    )
    remember_ephemeral(m.chat.id, m.message_id, sent.message_id)


# ---------- callbacks ----------

@router.callback_query(F.data.startswith("card:"))
async def cb_card(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    item = await db.get_item(item_id)
    if not item:
        await c.answer("Предмет не найден", show_alert=True)
        return
    text, _ = await build_card(item_id, item[1])
    watched = await db.is_watched(c.from_user.id, item_id)
    alert = await db.alert_active_for_item(c.from_user.id, item_id)
    try:
        await c.message.edit_text(text, reply_markup=card_kb(item_id, watched, alert))
    except Exception:
        await c.message.answer(text, reply_markup=card_kb(item_id, watched, alert))
    await c.answer()


@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    await db.watch_add(c.from_user.id, item_id)
    alert = await db.alert_active_for_item(c.from_user.id, item_id)
    await c.answer("Добавил в избранное ⭐")
    await c.message.edit_reply_markup(reply_markup=card_kb(item_id, True, alert))


@router.callback_query(F.data.startswith("unwatch:"))
async def cb_unwatch(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    await db.watch_remove(c.from_user.id, item_id)
    alert = await db.alert_active_for_item(c.from_user.id, item_id)
    await c.answer("Убрал из избранного")
    await c.message.edit_reply_markup(reply_markup=card_kb(item_id, False, alert))


@router.callback_query(F.data.startswith("alset:"))
async def cb_alset(c: CallbackQuery):
    _, d, item_id = c.data.split(":", 2)
    await send_alert_presets(c.message, c.from_user.id, item_id, "below" if d == "b" else "above")
    await c.answer()


@router.callback_query(F.data.startswith("alrm:"))
async def cb_alrm(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    n = await db.alerts_deactivate_item(c.from_user.id, item_id)
    item = await db.get_item(item_id)
    name = item[1] if item else "предмет"
    watched = await db.is_watched(c.from_user.id, item_id)
    try:
        await c.message.edit_reply_markup(reply_markup=card_kb(item_id, watched, None))
    except Exception:
        pass
    await c.answer("Слежка отменена 🔕" if n else "Активной слежки не было")
    if n:
        await c.message.answer(f"🔕 Слежка за <b>{name}</b> отменена.")


@router.callback_query(F.data.startswith("mkal:"))
async def cb_mkal(c: CallbackQuery):
    _, d, item_id, threshold = c.data.split(":", 3)
    direction = "below" if d == "b" else "above"
    item = await db.get_item(item_id)
    if not item:
        await c.answer("Предмет не найден", show_alert=True)
        return
    word = "подешевеет до" if direction == "below" else "подорожает до"
    if await db.alert_active_for_item(c.from_user.id, item_id):
        await c.answer("У тебя уже есть слежка на этот предмет 😉", show_alert=True)
        return
    await db.alert_add(c.from_user.id, item_id, direction, float(threshold))
    await db.watch_add(c.from_user.id, item_id)
    await c.message.edit_text(
        f"✅ Принято! Сообщу, когда <b>{item[1]}</b> {word} <b>{fmt(float(threshold))} ₮</b>.\n"
        f"⭐ Предмет добавлен в избранное.\n"
        f"<i>Проверяю цены каждые {COLLECT_EVERY_MIN} минут. Список: /alerts</i>"
    )
    await c.answer("Уведомление создано 🔔")


@router.callback_query(F.data.startswith("alcust:"))
async def cb_alcust(c: CallbackQuery, state: FSMContext):
    _, d, item_id = c.data.split(":", 2)
    item = await db.get_item(item_id)
    if not item:
        await c.answer("Предмет не найден", show_alert=True)
        return
    direction = "below" if d == "b" else "above"
    await state.set_state(AlertForm.threshold)
    await state.update_data(item_id=item_id, item_name=item[1], direction=direction)
    word = "подешевеет до этой цены" if direction == "below" else "подорожает до этой цены"
    await c.message.answer(
        f"✍️ Напиши цену числом, например <code>150000</code>.\n"
        f"Сообщу, когда <b>{item[1]}</b> {word}."
    )
    await c.answer()


@router.callback_query(F.data == "howto")
async def cb_howto(c: CallbackQuery):
    await c.answer(
        "🔍 Открой приложение → вкладка «Рынок».\n\n"
        "Найди предмет по названию (можно по-русски), смотри цену и график "
        "за 7 дней, ставь слежку — придёт уведомление, когда подешевеет.",
        show_alert=True,
    )


@router.callback_query(F.data == "wipebtn")
async def cb_wipebtn(c: CallbackQuery):
    season = await api.get_current_season()
    if not season:
        await c.answer("Не удалось получить данные сезона 😕", show_alert=True)
        return
    end = datetime.fromisoformat(season["endedAt"].replace("Z", "+00:00"))
    left = (end - datetime.now(timezone.utc)).days
    await c.answer(
        f"⏳ Сезон {season.get('number')} «{season.get('name')}»\n\n"
        f"До вайпа: {left} дн. ({end.strftime('%d.%m.%Y')})\n\n"
        f"Перед вайпом гир дешевеет — все распродаются. "
        f"После вайпа дорожают материалы и базовый гир.",
        show_alert=True,
    )


@router.callback_query(F.data == "dismiss")
async def cb_dismiss(c: CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("delalert:"))
async def cb_delalert(c: CallbackQuery):
    alert_id = int(c.data.split(":", 1)[1])
    await db.alert_delete(alert_id, c.from_user.id)
    await c.answer("Алерт удалён")
    await c.message.delete()


@router.callback_query(F.data == "digest_off")
async def cb_digest_off(c: CallbackQuery):
    """Кнопка «Отписаться» под дайджестом."""
    await db.set_digest_off(c.from_user.id, True)
    await c.answer("Отписал — дайджест больше не придёт. Включить обратно: /digest", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(Command("digest"))
async def cmd_digest(m: Message):
    """Включить/выключить ежедневный дайджест «Выгода дня»."""
    was_off = await db.get_digest_off(m.from_user.id)
    await db.set_digest_off(m.from_user.id, not was_off)
    if was_off:
        await m.answer("🔔 Дайджест <b>«Выгода дня»</b> включён — топ сделок раз в день в 19:00 МСК.")
    else:
        await m.answer("🔕 Дайджест выключен. Включить обратно — /digest")


# ---------- фоновые задачи ----------

async def refresh_items():
    """Раз в сутки обновляем справочник предметов."""
    try:
        items = await api.list_all_items()
        n = await db.replace_items(items)
        log.info("Справочник обновлён: %s предметов", n)
    except Exception:
        log.exception("Не удалось обновить справочник предметов")


REPO_DIR = Path(__file__).parent
NEWS_PATH = WEB_DIR / "news.json"
STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


def publish(rel_path: str, commit_msg: str) -> None:
    """Публикация сгенерированного файла.

    На сервере nginx раздаёт WEB_DIR напрямую — git не нужен.
    Локально пушим в GitHub Pages.
    """
    if not NEWS_GIT_PUSH:
        return
    for cmd in (["git", "add", rel_path],
                ["git", "commit", "-m", commit_msg],
                ["git", "push"]):
        res = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, timeout=120)
        if res.returncode != 0 and cmd[1] != "commit":
            log.warning("git %s: %s", cmd[1], res.stderr.decode(errors="ignore")[:200])


# русскоязычные каналы для ленты сообщества: (username, короткий бейдж)
TG_CHANNELS = [
    ("deltaforcegameofficial", "ОФИЦ."),
    ("deltaforce_ru", "DF RU"),
]
# мусорные посты канала (реклама/набор) — не тащим в ленту
TG_SKIP = ("нужен боец", "ищем человека", "вакансия", "требуется", "@vavilonys",
           "розыгрыш", "конкурс", "промокод", "реклама")
# кэш переводов лежит рядом с базой (папка с кодом на сервере read-only)
TR_CACHE_PATH = DATA_DIR / "trcache.json"
_tr_cache: dict | None = None


async def _translate_en_ru(text: str) -> str:
    """Перевод en->ru через публичный gtx-эндпоинт, с кэшем на диске.

    Сбой записи кэша НЕ должен терять готовый перевод — кэш это ускорение,
    а не источник данных.
    """
    global _tr_cache
    if _tr_cache is None:
        try:
            _tr_cache = json.loads(TR_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _tr_cache = {}
    if text in _tr_cache:
        return _tr_cache[text]

    out = ""
    try:
        r = await api._client.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": text},
        )
        r.raise_for_status()
        out = "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()
    except Exception:
        log.warning("Перевод не удался, оставляю оригинал: %s", text[:40])
        return text

    if not out:
        return text
    _tr_cache[text] = out
    try:
        TR_CACHE_PATH.write_text(json.dumps(_tr_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("Не смог сохранить кэш переводов в %s (перевод не потерян)", TR_CACHE_PATH)
    return out


_IMG_RE = re.compile(r"(\{STEAM_CLAN_IMAGE\}[^\s\]\[\"'<>]+|https?://[^\s\]\[\"'<>]+?\.(?:jpg|jpeg|png|gif))")


def _steam_img(contents: str) -> str:
    m = _IMG_RE.search(contents or "")
    if not m:
        return ""
    return m.group(1).replace("{STEAM_CLAN_IMAGE}", "https://clan.akamai.steamstatic.com/images")


def _clean_steam_body(contents: str) -> str:
    """BBCode/HTML новости Steam -> чистый текст с абзацами."""
    t = contents or ""
    t = re.sub(r"\[img\][^\[]*\[/img\]", "", t, flags=re.I)
    t = re.sub(r"\[previewyoutube[^\]]*\].*?\[/previewyoutube\]", "", t, flags=re.S | re.I)
    t = re.sub(r"\[url=[^\]]*\]", "", t, flags=re.I).replace("[/url]", "")
    t = t.replace("[*]", "\n• ")
    t = re.sub(r"\[/?[a-zA-Z][^\]]*\]", "\n", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html_mod.unescape(t)
    t = t.replace("<", "").replace(">", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()[:6000]


async def _translate_long_en_ru(text: str) -> str:
    """Перевод длинного текста кусками (по абзацам, до ~1500 символов)."""
    chunks, buf = [], ""
    for p in text.split("\n"):
        if len(buf) + len(p) > 1500 and buf:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    out = []
    for ch in chunks:
        out.append(await _translate_en_ru(ch))
        await asyncio.sleep(0.2)
    return "\n".join(out)


def _parse_tg_posts(page: str, src: str, channel_limit: int = 5) -> list[dict]:
    """Посты с фото из публичного TG-канала (t.me/s/...) для ленты сообщества."""
    posts = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        m_post = re.search(r'data-post="([^"]+)"', block)
        m_text = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        m_time = re.search(r'<time datetime="([^"]+)"', block)
        m_photo = re.search(r"background-image:url\('([^']+)'", block)
        if not (m_post and m_text):
            continue
        text = re.sub(r"<br/?>", " ", m_text.group(1))
        text = re.sub(r"<[^>]+>", "", text)
        text = html_mod.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        for cut in ("🦾", "💎", "💙"):  # рекламный футер канала
            i = text.find(cut)
            if i > 20:
                text = text[:i].strip()
        low = text.lower()
        if len(text) < 15 or not m_photo:            # для баннеров нужно фото
            continue
        if any(w in low for w in TG_SKIP):           # реклама/набор — пропускаем
            continue
        ts = 0
        if m_time:
            try:
                ts = int(datetime.fromisoformat(m_time.group(1).replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        posts.append({"title": text[:120], "url": f"https://t.me/{m_post.group(1)}", "date": ts,
                      "img": m_photo.group(1), "src": src})
    return posts[-channel_limit:][::-1]  # свежие сверху


async def fetch_community() -> list[dict]:
    """Лента сообщества: посты из русских каналов, склеены, без дублей, свежие первыми."""
    all_posts = []
    for username, badge in TG_CHANNELS:
        try:
            r = await api._client.get(f"https://t.me/s/{username}", headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            all_posts += _parse_tg_posts(r.text, badge)
        except Exception:
            log.exception("Канал %s недоступен", username)
    # дедуп по началу заголовка (репосты между каналами)
    seen, uniq = set(), []
    for p in sorted(all_posts, key=lambda x: -x["date"]):
        key = re.sub(r"\W+", "", p["title"].lower())[:40]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq[:12]


RUNAMES_PATH = WEB_DIR / "names_ru.json"


async def refresh_ru_names():
    """Переводит названия всех предметов на русский -> docs/names_ru.json -> git push.

    Переводятся только новые (словарь — накопительный кэш).
    """
    try:
        existing = {}
        if RUNAMES_PATH.exists():
            try:
                existing = json.loads(RUNAMES_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        rows = await db.all_items()
        out, new_cnt = {}, 0
        for item_id, name in rows:
            if item_id in existing:
                out[item_id] = existing[item_id]
                continue
            ru = await _translate_en_ru(name)
            out[item_id] = ru if ru and ru != name else name
            new_cnt += 1
            if new_cnt % 200 == 0:
                log.info("Перевод названий: %s новых...", new_cnt)
            await asyncio.sleep(0.12)
        if new_cnt == 0 and RUNAMES_PATH.exists():
            return
        RUNAMES_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        publish("docs/names_ru.json", "names: ru translations update")
        log.info("Русские названия готовы: всего %s, новых %s", len(out), new_cnt)
    except Exception:
        log.exception("Не удалось обновить русские названия")


async def refresh_news():
    """Каждые 2 часа: Steam-патчи + RU-канал -> docs/news.json -> git push.

    GitHub Pages раздаёт файл приложению (обход CORS без своего сервера).
    """
    try:
        steam = []
        try:
            r = await api._client.get(STEAM_NEWS_URL, params={"appid": 2507950, "count": 8})
            r.raise_for_status()
            for i in r.json()["appnews"]["newsitems"]:
                body = _clean_steam_body(i.get("contents", ""))
                steam.append({
                    "title": i["title"],
                    "title_ru": await _translate_en_ru(i["title"]),
                    "url": i["url"],
                    "date": i["date"],
                    "img": _steam_img(i.get("contents", "")),
                    "body_ru": await _translate_long_en_ru(body) if body else "",
                })
        except Exception:
            log.exception("Steam-новости недоступны")
        community = await fetch_community()
        if not steam and not community:
            return
        news = {"community": community, "steam": steam}
        if NEWS_PATH.exists():
            try:
                old = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
                if old.get("steam") == steam and old.get("community") == community:
                    return  # ничего нового
            except Exception:
                pass
        NEWS_PATH.write_text(
            json.dumps({"updated": datetime.now(timezone.utc).isoformat(), **news}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        publish("docs/news.json", "news: auto-update")
        log.info("Новости обновлены: steam=%s, сообщество=%s", len(steam), len(community))
    except Exception:
        log.exception("Не удалось обновить новости")


# ---------- сканер рынка и флипы ----------
FLIPS_PATH = WEB_DIR / "flips.json"
FLIP_MIN_PRICE = 3000       # дешевле — пыль, комиссия съест всё
FLIP_MIN_POINTS = 48        # минимум ~2 суток истории — чтобы медиана была надёжной
FLIP_DIP_MIN = 8            # просадка ниже 8% не покрывает комиссию
FLIP_DIP_MAX = 35           # выше 35% — почти всегда обвал/выброс, а не выгода
FLIP_MOVE_MAX = 60          # рост >60%/сутки — тоже аномалия данных


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


async def backfill_history():
    """Разовый импорт 7-дневной истории всех предметов в НАШУ базу.

    После него флипы работают сразу, а не через неделю накопления.
    """
    if await db.history_count() > 30000:
        return
    log.info("Бэкфилл истории цен: импортирую 7 дней по всем предметам...")
    ids = await db.all_item_ids()
    total = 0
    for i, item_id in enumerate(ids):
        try:
            series = await api.get_series(item_id, days=7)
            rows = [(item_id, p["timestamp"], p["priceAvg"])
                    for p in series if p.get("priceAvg")]
            if rows:
                await db.history_add_many(rows)
                total += len(rows)
        except Exception:
            pass
        if (i + 1) % 300 == 0:
            log.info("Бэкфилл: %s/%s предметов...", i + 1, len(ids))
        await asyncio.sleep(0.1)
    log.info("Бэкфилл готов: %s точек истории", total)


PRICES_PATH = WEB_DIR / "prices.json"


async def write_prices_json(refs: dict):
    """Прайс-лист всего рынка для приложения: {id: [price, ref, ch24]}.

    Приложение грузит его одним файлом — цены у ВСЕХ предметов сразу,
    без 30 запросов к чужому API.
    """
    try:
        rows = await db.history_rows_7d()
        by_item: dict[str, list[float]] = {}
        for item_id, _ts, price in rows:
            by_item.setdefault(item_id, []).append(price)
        out = {}
        for item_id, pts in by_item.items():
            price = pts[-1]
            ch24 = None
            if len(pts) >= 25 and pts[-25]:
                ch24 = round((price - pts[-25]) / pts[-25] * 100, 1)
            out[item_id] = [int(price), int(refs.get(item_id) or 0), ch24]
        tmp = PRICES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PRICES_PATH)
        log.info("Прайс-лист: %s предметов", len(out))
    except Exception:
        log.exception("Не смог записать прайс-лист")


async def market_snapshot(bot: Bot = None):
    """Каждый час: снимок цен ВСЕХ предметов в нашу базу."""
    ids = await db.all_item_ids()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    rows, refs, fails = [], {}, 0
    for item_id in ids:
        try:
            p = await api.get_price(item_id)
            if p and p.get("price"):
                rows.append((item_id, now, p["price"]))
                refs[item_id] = p.get("referencePrice")
        except Exception:
            fails += 1
        await asyncio.sleep(0.1)
    if rows:
        await db.history_add_many(rows)
    log.info("Снимок рынка: %s цен (%s ошибок)", len(rows), fails)
    await write_prices_json(refs)
    await compute_flips()
    if bot:
        await check_hot_deals(bot)


def _spark(pts: list[float], n: int = 16) -> list[int]:
    if len(pts) <= n:
        return [int(p) for p in pts]
    step = len(pts) / n
    return [int(pts[int(i * step)]) for i in range(n)]


async def compute_flips():
    """Считает флипы из НАШЕЙ истории и публикует web/flips.json."""
    try:
        rows = await db.history_rows_7d()
        by_item: dict[str, list[float]] = {}
        for item_id, _ts, price in rows:
            by_item.setdefault(item_id, []).append(price)

        dips, movers, volatile, forecast = [], [], [], []
        for item_id, pts in by_item.items():
            if len(pts) < FLIP_MIN_POINTS:
                continue
            cur = pts[-1]
            if cur < FLIP_MIN_PRICE:
                continue

            # МЕДИАНА, а не среднее: одна аномальная сделка не ломает «типичную цену»
            typical = _median(pts)
            if typical <= 0:
                continue

            # просадка: купить сейчас, продать по типичной цене (минус комиссия 15%)
            margin = typical * 0.85 - cur
            pct = margin / cur * 100
            # фильтр реалистичности: 8..35% — реальный отскок; выше — обвал/выброс
            if FLIP_DIP_MIN <= pct <= FLIP_DIP_MAX:
                dips.append({"id": item_id, "price": int(cur), "avg": int(typical),
                             "margin": int(margin), "pct": round(pct, 1),
                             "spark": _spark(pts)})

            # рост за сутки (тоже с потолком — резкий скачок часто выброс)
            if len(pts) >= 25:
                prev = pts[-25]
                mp = (cur - prev) / prev * 100 if prev else 0
                if 10 <= mp <= FLIP_MOVE_MAX:
                    movers.append({"id": item_id, "price": int(cur), "prev": int(prev),
                                   "pct": round(mp, 1), "spark": _spark(pts)})

            # внутридневные качели — на медиане окна, устойчиво к выбросам
            last24 = pts[-24:]
            lo, hi = min(last24), max(last24)
            mid = _median(last24)
            rng = (hi - lo) / mid * 100 if mid else 0
            # 18..80%: перекрывает комиссию, но не абсурд
            if 18 <= rng <= 80:
                volatile.append({"id": item_id, "price": int(cur), "lo": int(lo),
                                 "hi": int(hi), "pct": round(rng, 1), "spark": _spark(last24)})

            # прогноз (возврат к медиане, точность ~60%): вверх если дёшево, вниз если дорого
            devMed = (cur - typical) / typical * 100
            if -50 <= devMed <= -8:
                forecast.append({"id": item_id, "price": int(cur), "target": int(typical),
                                 "dir": "up", "pct": round(abs(devMed), 1), "spark": _spark(pts)})
            elif 8 <= devMed <= 50:
                forecast.append({"id": item_id, "price": int(cur), "target": int(typical),
                                 "dir": "down", "pct": round(devMed, 1), "spark": _spark(pts)})

        dips.sort(key=lambda x: -x["pct"])
        movers.sort(key=lambda x: -x["pct"])
        volatile.sort(key=lambda x: -x["pct"])
        forecast.sort(key=lambda x: -x["pct"])
        payload = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "scanned": len(by_item),
            "dips": dips[:15], "movers": movers[:15], "volatile": volatile[:15],
            "forecast": forecast[:20],
        }
        tmp = FLIPS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FLIPS_PATH)
        log.info("Флипы: %s просадок, %s растущих, %s качелей (из %s предметов)",
                 len(dips), len(movers), len(volatile), len(by_item))
    except Exception:
        log.exception("Не смог посчитать флипы")


async def bootstrap_market():
    """При старте: бэкфилл (если базы мало) -> прайс-лист -> флипы."""
    try:
        await backfill_history()
        if not PRICES_PATH.exists():
            await write_prices_json({})   # ref подтянется при первом снимке
        await compute_flips()
    except Exception:
        log.exception("Бутстрап рынка не удался")


async def credit_referrals(bot: Bot):
    """Каждые 30 мин: засчитываем активных приглашённых, дарим рефереру дни PRO."""
    for friend_id, referrer_id in await db.pending_referrals():
        try:
            new_until = await db.add_pro_days(referrer_id, REF_BONUS_DAYS)
            await db.mark_referral_credited(friend_id, referrer_id)
            until = datetime.fromisoformat(new_until).strftime("%d.%m.%Y")
            await bot.send_message(
                referrer_id,
                f"🎉 Твой друг активно пользуется Хабаром!\n"
                f"Тебе начислено <b>+{REF_BONUS_DAYS} дней PRO</b> 🔥\n"
                f"<i>PRO активен до {until}. Зови ещё друзей — продлевай бесплатно.</i>")
        except Exception as e:
            if "bot was blocked" in str(e).lower():
                await db.mark_referral_credited(friend_id, referrer_id)
            else:
                log.exception("Не смог засчитать реферала %s -> %s", friend_id, referrer_id)


async def warm_profiles():
    """Каждый час: опрашиваем привязанных игроков.

    Это (1) заставляет внешний сервис начать/продолжить трекинг игрока —
    стэши появляются быстрее, и (2) копит НАШУ историю стэшей,
    независимую от чужого API.
    """
    players = await db.tracked_players()
    if not players:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    ok = 0
    for pid in players:
        try:
            val = await api.get_stash_value(pid)
            if val is not None:
                await db.stash_add(pid, now, val)
                ok += 1
        except Exception:
            log.exception("Прогрев профиля %s не удался", pid)
        await asyncio.sleep(API_POLITE_DELAY)
    log.info("Прогрев профилей: %s из %s с данными", ok, len(players))


async def collect_and_check(bot: Bot):
    """Каждые N минут: снапшот цен отслеживаемых предметов + проверка алертов."""
    ids = await db.tracked_item_ids()
    if not ids:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    prices: dict[str, float] = {}
    for item_id in ids:
        p = await api.get_price(item_id)
        if p:
            prices[item_id] = p["price"]
            await db.history_add(item_id, now, p["price"])
        await asyncio.sleep(API_POLITE_DELAY)
    log.info("Снапшот цен: %s предметов", len(prices))

    notified = set()   # (tg_id, item_id) — чтобы дубли не слали повторно за цикл
    for alert_id, tg_id, item_id, name, direction, threshold in await db.active_alerts():
        price = prices.get(item_id)
        if price is None:
            continue
        hit = price <= threshold if direction == "below" else price >= threshold
        if not hit:
            continue
        # одно уведомление на предмет: гасим ВСЕ алерты юзера на него сразу
        if (tg_id, item_id) in notified:
            await db.alert_deactivate(alert_id)
            continue
        notified.add((tg_id, item_id))
        emoji = "📉" if direction == "below" else "📈"
        word = "подешевел до" if direction == "below" else "подорожал до"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👌 Убрать", callback_data="dismiss")]])
        try:
            await bot.send_message(
                tg_id,
                f"{emoji} <b>{name}</b> {word} <b>{fmt(price)} ₮</b>!\n"
                f"<i>Твой порог: {fmt(threshold)} ₮</i>",
                reply_markup=kb,
            )
            await db.alerts_deactivate_item(tg_id, item_id)
        except Exception as e:
            if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                await db.mark_blocked(tg_id)
                await db.alerts_deactivate_item(tg_id, item_id)
                log.info("Пользователь %s заблокировал бота", tg_id)
            else:
                log.exception("Не смог отправить алерт tg_id=%s", tg_id)


def _load_ru_names() -> dict:
    try:
        return json.loads(RUNAMES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def build_digest_text():
    """Собирает текст «Выгоды дня» из flips.json.

    Возвращает (text, n_dips, n_movers) или None, если стоящей выгоды мало
    (лучше пропустить день, чем прислать пустой дайджест).
    """
    try:
        data = json.loads(FLIPS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    top_dips = sorted(data.get("dips", []), key=lambda x: -x.get("margin", 0))[:3]
    top_movers = sorted(data.get("movers", []), key=lambda x: -x.get("pct", 0))[:2]
    if len(top_dips) < 2 and not top_movers:
        return None

    ru = _load_ru_names()

    async def name_of(item_id: str) -> str:
        nm = ru.get(item_id)
        if not nm:
            it = await db.get_item(item_id)
            nm = it[1] if it else "предмет"
        return html_mod.escape(nm)

    today = datetime.now(timezone.utc).strftime("%d.%m")
    lines = [f"🔥 <b>ВЫГОДА ДНЯ</b> · {today}", ""]
    if top_dips:
        lines += ["Пока цены на дне — момент закупиться 👇", "", "🟢 <b>БЕРИ ДЁШЕВО:</b>"]
        for i, d in enumerate(top_dips, 1):
            row = f"{i}. {await name_of(d['id'])} — <b>{fmt(d['price'])} ₮</b>"
            if d.get("avg") and d.get("margin", 0) > 0:
                row += f"\n    обычно ~{fmt(d['avg'])} → заработок <b>+{fmt(d['margin'])}</b>"
            lines.append(row)
    if top_movers:
        lines += ["", "🔴 <b>ПРОДАВАЙ, ПОКА ДОРОГО</b> (взлёт за сутки):"]
        for i, m in enumerate(top_movers, 1):
            lines.append(f"{i}. {await name_of(m['id'])} — <b>{fmt(m['price'])} ₮</b> (+{m.get('pct', 0):.0f}%)")
    lines += ["", "⏳ <i>Успей, пока не откупили · обновляется каждый час</i>"]
    return "\n".join(lines), len(top_dips), len(top_movers)


async def send_daily_digest(bot: Bot):
    """Раз в день (16:00 UTC = 19:00 МСК): пуш «Выгода дня» подписчикам."""
    built = await build_digest_text()
    if not built:
        log.info("Дайджест: мало выгоды сегодня — не шлю")
        return
    text, n_dips, n_movers = built
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Открыть Выгоду", web_app=WebAppInfo(url=APP_URL + "#flips"))],
        [InlineKeyboardButton(text="🔕 Отписаться", callback_data="digest_off")],
    ])
    sent = await _broadcast(bot, text, kb)
    log.info("Дайджест разослан: %s (просадок %s, растущих %s)", sent, n_dips, n_movers)


async def _broadcast(bot: Bot, text: str, kb) -> int:
    """Рассылает сообщение всем подписчикам; помечает заблокировавших бота."""
    recipients = await db.digest_recipients()
    sent = 0
    for tg_id in recipients:
        try:
            await bot.send_message(tg_id, text, reply_markup=kb, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            low = str(e).lower()
            if "bot was blocked" in low or "user is deactivated" in low:
                await db.mark_blocked(tg_id)
            else:
                log.exception("Рассылка: не отправил tg_id=%s", tg_id)
        await asyncio.sleep(0.05)
    return sent


# «Горячая выгода» — real-time алерт об исключительной просадке
HOT_MIN_MARGIN = 50000          # навар от N ₮ — только реально жирные сделки
HOT_MAX_PER_DAY = 3             # не больше N горячих в день (анти-спам)
HOT_QUIET_UTC = {20, 21, 22, 23, 0, 1, 2, 3, 4}   # 23:00–08:00 МСК — тихо, не будим
_hot_seen: set = set()          # id предметов, уже разосланных сегодня (память процесса)
_hot_day = None
_hot_count = 0


async def check_hot_deals(bot: Bot):
    """После часового пересчёта рынка: исключительная просадка — шлём сразу.

    Состояние в памяти процесса (сбрасывается при рестарте — это ок, рестарт редок).
    """
    global _hot_day, _hot_count
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if _hot_day != today:                  # новый день — сброс счётчиков
        _hot_day, _hot_count = today, 0
        _hot_seen.clear()
    if now.hour in HOT_QUIET_UTC:          # ночь по МСК — молчим
        return
    if _hot_count >= HOT_MAX_PER_DAY:      # дневной лимит исчерпан
        return
    try:
        data = json.loads(FLIPS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    fresh = [d for d in data.get("dips", [])
             if d.get("margin", 0) >= HOT_MIN_MARGIN and d["id"] not in _hot_seen]
    if not fresh:
        return
    deal = max(fresh, key=lambda x: x["margin"])   # одна, самая жирная за проход
    _hot_seen.add(deal["id"])
    _hot_count += 1

    ru = _load_ru_names()
    nm = ru.get(deal["id"])
    if not nm:
        it = await db.get_item(deal["id"])
        nm = it[1] if it else "предмет"
    nm = html_mod.escape(nm)
    text = (f"🔥🔥 <b>ГОРЯЧАЯ ВЫГОДА — успей!</b>\n\n"
            f"<b>{nm}</b>\n"
            f"Сейчас {fmt(deal['price'])} ₮ · обычно ~{fmt(deal.get('avg', 0))}\n"
            f"💰 Навар <b>+{fmt(deal['margin'])} ₮</b> после комиссии\n\n"
            f"<i>Успей забрать, пока не откупили</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Открыть Выгоду", web_app=WebAppInfo(url=APP_URL + "#flips"))],
        [InlineKeyboardButton(text="🔕 Отписаться", callback_data="digest_off")],
    ])
    sent = await _broadcast(bot, text, kb)
    log.info("Горячая выгода: %s (навар +%s) — %s получателей, за день %s/%s",
             nm, deal["margin"], sent, _hot_count, HOT_MAX_PER_DAY)


# ---------- main ----------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN. Создай файл .env со строкой BOT_TOKEN=твой_токен")

    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(TrackUserMiddleware())
    dp.callback_query.middleware(TrackUserMiddleware())
    dp.include_router(router)

    if await db.items_count() == 0:
        log.info("Первый запуск: загружаю справочник предметов (может занять минуту)...")
        await refresh_items()

    await refresh_news()  # свежие новости при каждом старте

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(refresh_items, "interval", hours=24)
    scheduler.add_job(refresh_news, "interval", hours=2)
    scheduler.add_job(refresh_ru_names, "interval", hours=24)
    scheduler.add_job(collect_and_check, "interval", minutes=COLLECT_EVERY_MIN, args=[bot])
    scheduler.add_job(warm_profiles, "interval", minutes=WARM_EVERY_MIN)
    scheduler.add_job(credit_referrals, "interval", minutes=30, args=[bot])
    scheduler.add_job(market_snapshot, "interval", hours=1, args=[bot])
    scheduler.add_job(db.history_cleanup, "interval", hours=24)
    scheduler.add_job(send_daily_digest, "cron", hour=16, minute=0, args=[bot])  # 19:00 МСК
    scheduler.start()
    asyncio.get_running_loop().create_task(refresh_ru_names())
    asyncio.get_running_loop().create_task(bootstrap_market())

    if API_ENABLED:
        from web_api import start_api
        await start_api()
        asyncio.get_running_loop().create_task(warm_profiles())

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
