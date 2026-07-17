import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# на сервере база и веб-файлы лежат отдельно от кода
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent))
WEB_DIR = Path(os.getenv("WEB_DIR", Path(__file__).parent / "docs"))
DB_PATH = str(DATA_DIR / "df.sqlite3")
API_BASE = "https://api.deltaforceapi.com/deltaforceapi.gateway.v1.ApiService"
# кто видит /stats (свой Telegram ID)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "1034454256").split(",") if x.strip()}
# на сервере пушить в git не надо — nginx раздаёт файлы напрямую
NEWS_GIT_PUSH = os.getenv("NEWS_GIT_PUSH", "1") == "1"
# HTTP API для Mini App (nginx проксирует /api сюда)
API_PORT = int(os.getenv("API_PORT", "8081"))
API_ENABLED = os.getenv("API_ENABLED", "0") == "1"
# как часто прогревать профили привязанных игроков (минуты)
WARM_EVERY_MIN = int(os.getenv("WARM_EVERY_MIN", "60"))

# как часто собирать цены отслеживаемых предметов (минуты)
COLLECT_EVERY_MIN = 15
# пауза между запросами к API, чтобы не долбить сервис (секунды)
API_POLITE_DELAY = 0.15
