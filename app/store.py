"""Хранилище: скачивание каталогов HP, разбор, персистентный JSON-индекс.

За кулисами используется LINUX_ONLY-фильтр: Windows IoT-образы и
Windows-addon'ы в индекс не попадают.
"""

import json
import os
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from . import catalog as catalog_mod
from .config import Config

_lock = threading.RLock()
_index = None
_last_refresh = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now():
    return _now()


def catalogs_dir():
    d = os.path.join(Config.DATA_DIR, "catalogs")
    os.makedirs(d, exist_ok=True)
    return d


def index_path():
    return os.path.join(Config.DATA_DIR, "index.json")


def _raw_path(name):
    return os.path.join(catalogs_dir(), name + ".xml")


def _save(index):
    tmp = index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, index_path())


_32BIT_X86 = {"386", "i386", "i486", "i586", "i686", "x86", "x86_32", "ia32"}


def _arch_ok(a):
    a = (a or "").strip().lower()
    if not a:
        return True
    if a in _32BIT_X86:
        return False
    if "86" in a and "64" not in a and "amd64" not in a:
        return False
    return True


def _filter_rows(rows):
    rows = [r for r in rows if _arch_ok(r.get("architecture"))]
    if Config.THINPRO_ONLY:
        rows = [r for r in rows if r.get("os_family") == "thinpro"]
    elif Config.LINUX_ONLY:
        rows = [r for r in rows if r.get("os_family") in ("thinpro", "smart-zero")]
    return rows


def _parse_from_file(name, url):
    with open(_raw_path(name), encoding="utf-8", errors="replace") as fh:
        xml_text = fh.read()
    rows = catalog_mod.parse_text(xml_text, url)
    return _filter_rows(rows)


def _ensure_index():
    global _index
    if _index is not None:
        return _index
    if os.path.exists(index_path()):
        try:
            with open(index_path(), encoding="utf-8") as fh:
                _index = json.load(fh)
            return _index
        except Exception:
            pass
    index = {"catalogs": {}}
    for name, url in Config.CATALOGS.items():
        if os.path.exists(_raw_path(name)):
            try:
                with open(_raw_path(name), encoding="utf-8", errors="replace") as fh:
                    xml_text = fh.read()
                rows = catalog_mod.parse_text(xml_text, url)
                index["catalogs"][name] = {
                    "url": url,
                    "fetched_at": _now(),
                    "rows": _filter_rows(rows),
                }
            except Exception:
                continue
    _save(index)
    _index = index
    return index


def index():
    with _lock:
        return _ensure_index()


def ordered_rows(name):
    """Канонический порядок рядов каталога (используется и в UI, и в скачивании)."""
    idx = index()
    rows = (idx.get("catalogs", {}).get(name, {}) or {}).get("rows", []) or []
    if name == "hp":
        return sorted(rows, key=lambda r: (
            0 if not r.get("platform_id") or r.get("platform_id") == "unknown" else 1,
            r.get("platform_id") or "",
            r.get("os_version") or "",
            r.get("id") or "",
        ))
    if name == "addons":
        return sorted(rows, key=lambda r: (
            _version_key(r.get("tp_version") or ""),
            (r.get("name") or "").lower(),
            r.get("id") or "",
        ))
    return sorted(rows, key=lambda r: ((r.get("name") or "").lower(), r.get("id") or ""))


def _version_key(v):
    parts = []
    for p in v.split("."):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))
    return tuple(parts)


def addon_groups():
    """Аддоны сгруппированы по версии ThinPro: [(version, [rows]), ...] (новые сверху)."""
    groups = {}
    for r in ordered_rows("addons"):
        groups.setdefault(r.get("tp_version") or "", []).append(r)
    versions = sorted(groups, key=_version_key, reverse=True)
    return [(v, groups[v]) for v in versions]


def _is_deb(f):
    name = (f.get("label") or url_last(f)) or ""
    return name.lower().endswith(".deb")


def url_last(f):
    return urlsplit(f.get("url", "")).path.rstrip("/").rsplit("/", 1)[-1]


def _files_with_extension(ext):
    """Все файлы из каталогов с заданным расширением и каноническими индексами."""
    out = []
    for catalog in ("hp", "addons"):
        for i, r in enumerate(ordered_rows(catalog)):
            for j, f in enumerate(r.get("files", [])):
                name = (f.get("label") or url_last(f)) or ""
                if not name.lower().endswith(ext):
                    continue
                out.append({
                    "catalog": catalog,
                    "row": i,
                    "file": j,
                    "label": name,
                    "url": f.get("url", ""),
                    "size": f.get("size"),
                    "version": r.get("tp_version") or r.get("os_version") or "",
                    "date": r.get("available") or "",
                    "arch": r.get("architecture") or "",
                    "source": r.get("name") or "",
                    "source_id": r.get("id") or "",
                })
    return out


def deb_entries():
    return _files_with_extension(".deb")


def xar_entries():
    return _files_with_extension(".xar")


def has_data():
    with _lock:
        idx = _ensure_index()
        return any(c.get("rows") for c in idx.get("catalogs", {}).values())


def last_refresh():
    with _lock:
        return _last_refresh


def _fetch_catalog(url):
    """GET урла каталога с фолбэком https -> http. Возвращает (текст, итоговый url)."""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append(url.replace("https://", "http://", 1))
    last = None
    for cand in candidates:
        try:
            r = requests.get(
                cand,
                timeout=(Config.CONNECT_TIMEOUT, Config.READ_TIMEOUT),
                headers={"Accept": "application/xml, text/xml, text/*;q=0.9, */*;q=0.1"},
            )
            r.raise_for_status()
            return r.text, r.url
        except requests.RequestException as exc:
            last = exc
            continue
    raise last


def refresh():
    """Скачивает оба каталога, парсит и обновляет индекс. Возвращает сводку."""
    global _index, _last_refresh
    with _lock:
        idx = _ensure_index()
        fetched = _now()
        errors = {}
        counts = {}
        for name, url in Config.CATALOGS.items():
            try:
                text, used_url = _fetch_catalog(url)
                with open(_raw_path(name), "w", encoding="utf-8") as fh:
                    fh.write(text)
                rows = catalog_mod.parse_text(text, used_url)
                idx.setdefault("catalogs", {})[name] = {
                    "url": used_url,
                    "fetched_at": fetched,
                    "rows": _filter_rows(rows),
                }
                counts[name] = len(_filter_rows(rows))
            except Exception as exc:
                errors[name] = str(exc)
        _save(idx)
        _index = idx
        summary = {
            "fetched_at": fetched,
            "counts": {
                k: v.get("rows", []) if isinstance(v, dict) else 0
                for k, v in idx.get("catalogs", {}).items()
            },
            "errors": errors,
        }
        _last_refresh = summary
        return summary