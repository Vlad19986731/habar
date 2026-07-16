"""Русские алиасы и транслит для поиска предметов.

API отдаёт английские названия, а игроки пишут по-русски.
Словарь пополняем по мере жалоб пользователей «не нашёл предмет».
"""

# запрос (в нижнем регистре) -> подстрока английского названия
ALIASES: dict[str, str] = {
    "дигл": "desert eagle",
    "деагл": "desert eagle",
    "дезерт игл": "desert eagle",
    "вектор": "vector",
    "векторе": "vector",
    "м4": "m4",
    "м16": "m16",
    "ак": "ak",
    "калаш": "ak",
    "авп": "awm",
    "авм": "awm",
    "глок": "glock",
    "узи": "uzi",
    "мп5": "mp5",
    "мп7": "mp7",
    "п90": "p90",
    "шлем": "helmet",
    "каска": "helmet",
    "броня": "armor",
    "бронежилет": "armor",
    "бронник": "armor",
    "жилет": "vest",
    "разгрузка": "rig",
    "рюкзак": "backpack",
    "сумка": "bag",
    "ключ": "key",
    "карта": "keycard",
    "аптечка": "first aid",
    "стим": "stim",
    "патроны": "ammo",
    "глушитель": "suppressor",
    "прицел": "scope",
    "ствол": "barrel",
    "приклад": "stock",
    "магазин": "mag",
    "золотые": "gold",
    "сердце африки": "heart of africa",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u", "я": "ya",
}


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def normalize_query(text: str) -> list[str]:
    """Варианты запроса для поиска: как есть, алиас, транслит."""
    q = text.strip().lower()
    variants = [q]
    if q in ALIASES:
        variants.append(ALIASES[q])
    else:
        for ru, en in ALIASES.items():
            if ru in q:
                variants.append(q.replace(ru, en))
    tr = translit(q)
    if tr != q:
        variants.append(tr)
    # убираем дубли, сохраняя порядок
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
