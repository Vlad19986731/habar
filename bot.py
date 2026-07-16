"""DF Аукцион — Telegram-бот экономики Delta Force. MVP.

Запуск:  python bot.py
Требует: .env с BOT_TOKEN=...
"""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import api
import db
from aliases import normalize_query
from config import API_POLITE_DELAY, BOT_TOKEN, COLLECT_EVERY_MIN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dfbot")

router = Router()

BARS = "▁▂▃▄▅▆▇█"


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


def card_kb(item_id: str, watched: bool) -> InlineKeyboardMarkup:
    star = "✓ В избранном" if watched else "⭐ В избранное"
    star_cb = f"unwatch:{item_id}" if watched else f"watch:{item_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=star, callback_data=star_cb),
         InlineKeyboardButton(text="🔄 Обновить", callback_data=f"card:{item_id}")],
        [InlineKeyboardButton(text="📉 Подешевеет", callback_data=f"alset:b:{item_id}"),
         InlineKeyboardButton(text="📈 Подорожает", callback_data=f"alset:a:{item_id}")],
    ])


# ---------- FSM ----------

class AlertForm(StatesGroup):
    threshold = State()


# ---------- handlers ----------

async def send_alert_presets(m: Message, item_id: str, direction: str):
    """Предлагает готовые пороги цены кнопками — без ручного ввода."""
    item = await db.get_item(item_id)
    if not item:
        await m.answer("Предмет не найден 😕")
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
                    if await db.alert_exists(m.from_user.id, item_id, direction, threshold):
                        await m.answer(
                            f"🔁 Это уведомление уже стоит: <b>{item[1]}</b> {word} <b>{fmt(threshold)} ₮</b>. "
                            f"Всё под контролем! Список: /alerts"
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
            await send_alert_presets(m, item_id, direction)
            return
    n = await db.items_count()
    await m.answer(
        "🎯 <b>Хабар</b> — цены аукциона Delta Force прямо в Telegram.\n\n"
        "Просто напиши название предмета (можно по-русски):\n"
        "<code>вектор</code>, <code>дигл</code>, <code>gold ammo</code>, <code>helmet</code>\n\n"
        "Команды:\n"
        "/watch — избранное\n"
        "/alerts — уведомления о ценах\n"
        "/wipe — сколько до вайпа\n\n"
        f"<i>В базе {n} предметов · данные: deltaforceapi.com</i>"
    )


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
    rows = await db.watch_list(m.from_user.id)
    if not rows:
        await m.answer("В избранном пусто. Найди предмет и нажми «⭐ В избранное».")
        return
    lines = ["⭐ <b>Избранное</b>\n"]
    for item_id, name in rows[:20]:
        p = await api.get_price(item_id)
        lines.append(f"• {name} — <b>{fmt(p['price'])}</b>" if p else f"• {name} — н/д")
        await asyncio.sleep(API_POLITE_DELAY)
    await m.answer("\n".join(lines))


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
    if await db.alert_exists(m.from_user.id, data["item_id"], direction, threshold):
        await state.clear()
        await m.answer(f"🔁 Это уведомление уже стоит. Список: /alerts")
        return
    await db.alert_add(m.from_user.id, data["item_id"], direction, threshold)
    await db.watch_add(m.from_user.id, data["item_id"])
    await state.clear()
    await m.answer(
        f"✅ Принято! Сообщу, когда <b>{data['item_name']}</b> {word} <b>{fmt(threshold)} ₮</b>.\n"
        f"⭐ Предмет добавлен в избранное."
    )


@router.message(F.text & ~F.text.startswith("/"))
async def search(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.username)
    results = []
    for variant in normalize_query(m.text):
        results = await db.search_items(variant)
        if results:
            break
    if not results:
        await m.answer(
            "Не нашёл 😕 Попробуй по-английски или короче: "
            "<code>vector</code>, <code>helmet</code>, <code>gold</code>\n\n"
            "<i>Если предмет точно есть — напиши как ты его искал, добавим алиас.</i>"
        )
        return
    if len(results) == 1:
        item_id, name = results[0]
        text, _ = await build_card(item_id, name)
        watched = await db.is_watched(m.from_user.id, item_id)
        await m.answer(text, reply_markup=card_kb(item_id, watched))
        return
    kb = [[InlineKeyboardButton(text=name, callback_data=f"card:{item_id}")]
          for item_id, name in results]
    await m.answer("Уточни, что именно:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


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
    try:
        await c.message.edit_text(text, reply_markup=card_kb(item_id, watched))
    except Exception:
        await c.message.answer(text, reply_markup=card_kb(item_id, watched))
    await c.answer()


@router.callback_query(F.data.startswith("watch:"))
async def cb_watch(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    await db.watch_add(c.from_user.id, item_id)
    await c.answer("Добавил в вотчлист ⭐")
    await c.message.edit_reply_markup(reply_markup=card_kb(item_id, True))


@router.callback_query(F.data.startswith("unwatch:"))
async def cb_unwatch(c: CallbackQuery):
    item_id = c.data.split(":", 1)[1]
    await db.watch_remove(c.from_user.id, item_id)
    await c.answer("Убрал из вотчлиста")
    await c.message.edit_reply_markup(reply_markup=card_kb(item_id, False))


@router.callback_query(F.data.startswith("alset:"))
async def cb_alset(c: CallbackQuery):
    _, d, item_id = c.data.split(":", 2)
    await send_alert_presets(c.message, item_id, "below" if d == "b" else "above")
    await c.answer()


@router.callback_query(F.data.startswith("mkal:"))
async def cb_mkal(c: CallbackQuery):
    _, d, item_id, threshold = c.data.split(":", 3)
    direction = "below" if d == "b" else "above"
    item = await db.get_item(item_id)
    if not item:
        await c.answer("Предмет не найден", show_alert=True)
        return
    word = "подешевеет до" if direction == "below" else "подорожает до"
    if await db.alert_exists(c.from_user.id, item_id, direction, float(threshold)):
        await c.answer("Уже стоит 😉", show_alert=False)
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


@router.callback_query(F.data.startswith("delalert:"))
async def cb_delalert(c: CallbackQuery):
    alert_id = int(c.data.split(":", 1)[1])
    await db.alert_delete(alert_id, c.from_user.id)
    await c.answer("Алерт удалён")
    await c.message.delete()


# ---------- фоновые задачи ----------

async def refresh_items():
    """Раз в сутки обновляем справочник предметов."""
    try:
        items = await api.list_all_items()
        n = await db.replace_items(items)
        log.info("Справочник обновлён: %s предметов", n)
    except Exception:
        log.exception("Не удалось обновить справочник предметов")


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

    for alert_id, tg_id, item_id, name, direction, threshold in await db.active_alerts():
        price = prices.get(item_id)
        if price is None:
            continue
        hit = price <= threshold if direction == "below" else price >= threshold
        if hit:
            emoji = "📉" if direction == "below" else "📈"
            word = "подешевел до" if direction == "below" else "подорожал до"
            try:
                await bot.send_message(
                    tg_id,
                    f"{emoji} <b>{name}</b> {word} <b>{fmt(price)} ₮</b>!\n"
                    f"<i>Твой порог: {fmt(threshold)} ₮</i>",
                )
                await db.alert_deactivate(alert_id)
            except Exception:
                log.exception("Не смог отправить алерт tg_id=%s", tg_id)


# ---------- main ----------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN. Создай файл .env со строкой BOT_TOKEN=твой_токен")

    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    if await db.items_count() == 0:
        log.info("Первый запуск: загружаю справочник предметов (может занять минуту)...")
        await refresh_items()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(refresh_items, "interval", hours=24)
    scheduler.add_job(collect_and_check, "interval", minutes=COLLECT_EVERY_MIN, args=[bot])
    scheduler.start()

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
