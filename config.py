import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = str(Path(__file__).parent / "df.sqlite3")
API_BASE = "https://api.deltaforceapi.com/deltaforceapi.gateway.v1.ApiService"

# как часто собирать цены отслеживаемых предметов (минуты)
COLLECT_EVERY_MIN = 15
# пауза между запросами к API, чтобы не долбить сервис (секунды)
API_POLITE_DELAY = 0.15
