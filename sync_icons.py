"""Скачивает иконки предметов с CDN игры на НАШ сервер: web/icons/<gameId>.png.

Зачем: раздача иконок с habargg.ru надёжнее, чем с чужого CDN — переживает их
сбои (как авария 19.07) и без Referer/CORS-проблем. Приложение сначала берёт
icons/<id>.png с нашего домена, а если её нет — фолбэком идёт на их CDN.

Идемпотентно: уже скачанные пропускает. Крон раз в сутки подхватывает новые.
Запуск: /opt/habar/venv/bin/python /opt/habar/app/sync_icons.py
"""
import os
import re
import sqlite3
import sys
import time

import httpx

from config import DB_PATH

ICON_DIR = os.environ.get("HABAR_ICON_DIR", "/opt/habar/web/icons")
_ID_RE = re.compile(r"auction-item/(\d+)/")


def main() -> int:
    os.makedirs(ICON_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT icon_url FROM items WHERE icon_url IS NOT NULL AND icon_url != ''").fetchall()
    con.close()

    ok = skip = fail = 0
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for (url,) in rows:
            m = _ID_RE.search(url or "")
            if not m:
                continue
            path = os.path.join(ICON_DIR, m.group(1) + ".png")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                skip += 1
                continue
            try:
                r = client.get(url)
                if r.status_code == 200 and r.content:
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    os.replace(tmp, path)   # атомарно: без «полускачанных» файлов
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            time.sleep(0.03)   # вежливо к их CDN

    print(f"icons sync: {ok} new, {skip} existed, {fail} failed / {len(rows)} total -> {ICON_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
