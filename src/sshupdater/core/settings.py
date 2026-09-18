from pathlib import Path
import logging
from . import storage

APP_NAME = "sshupdater"
DATA_DIR = Path.home() / f".{APP_NAME}"
LOG_FILE = DATA_DIR / "app.log"
DB_PATH = DATA_DIR / "app.db"
CONFIG_ENC = DATA_DIR / "config.enc"
KNOWN_HOSTS = DATA_DIR / "known_hosts"
THEME = "light"


def initialize():
    """Called before logging, vault or database access, never at import time."""
    global THEME
    storage.initialize(DATA_DIR)
    storage.secure_file(LOG_FILE, create=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )
    theme_file = DATA_DIR / "theme.txt"
    if theme_file.exists():
        THEME = storage.read_private(theme_file).decode("utf-8").strip().lower()
