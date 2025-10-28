from pathlib import Path
import logging

APP_NAME = "sshupdater"
DATA_DIR = Path.home() / f".{APP_NAME}"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = DATA_DIR / "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

DB_PATH = DATA_DIR / "app.db"
CONFIG_ENC = DATA_DIR / "config.enc"

# Theme aus gespeicherter Datei lesen, falls vorhanden
_theme_file = DATA_DIR / "theme.txt"
if _theme_file.exists():
    try:
        THEME = _theme_file.read_text(encoding="utf-8").strip().lower()
    except Exception:
        THEME = "light"
else:
    THEME = "light"  # Fallback-Theme
