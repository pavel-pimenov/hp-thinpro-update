"""Фоновый планировщик: периодическое обновление каталогов HP."""

import threading

from . import store
from .config import Config

_thread = None
_stop = threading.Event()


def start(app):
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_run, daemon=True, name="catalog-refresher")
    _thread.start()


def _run():
    try:
        if not store.has_data():
            store.refresh()
    except Exception:
        pass
    while not _stop.wait(Config.REFRESH_INTERVAL_HOURS * 3600):
        try:
            store.refresh()
        except Exception:
            pass