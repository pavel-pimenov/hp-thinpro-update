import os


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-before-prod")
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
    CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(DATA_DIR, "mirror")

    UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://ftp.hp.com").rstrip("/")

    CATALOGS = {
        "hp": UPSTREAM_BASE + "/pub/tcimages/EasyUpdate/Images/hpcatalog.xml",
        "addons": UPSTREAM_BASE + "/pub/tcimages/EasyUpdate/Images/addoncatalog.xml",
    }

    # Фильтры выдачи:
    #  * LINUX_ONLY   — не показывать Windows (по умолчанию вкл);
    #  * THINPRO_ONLY — показывать только ThinPro (Smart Zero Core тоже скрыт).
    LINUX_ONLY = _env_bool("LINUX_ONLY", True)
    THINPRO_ONLY = _env_bool("THINPRO_ONLY", True)

    REFRESH_INTERVAL_HOURS = float(os.environ.get("REFRESH_INTERVAL_HOURS", "12"))
    ENABLE_MIRROR = _env_bool("ENABLE_MIRROR", bool(CACHE_DIR))
    # Сколько файлов качать на сервер одновременно (общий пул для
    # отдельных и массовых «зеркалирований»).
    MAX_CONCURRENT_MIRRORS = _env_int("MAX_CONCURRENT_MIRRORS", 4)

    # Таймауты на запросы к HP (сек).
    CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "30"))
    READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "120"))

    CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(256 * 1024)))