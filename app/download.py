"""Выгрузка файлов.

Два пути:
  * проксирование напрямую с HP (стриминг, поддержка Range/докачки);
  * «точечная» выгрузка на сервер (mirror) с фоновым прогрессом,
    после чего файл отдаётся уже из локального кэша.
"""

import fcntl
import json
import mimetypes
import os
import threading
import uuid
from urllib.parse import urlsplit

import requests
from flask import Response, abort, request, send_file, stream_with_context

from . import store
from .config import Config

CHUNK = Config.CHUNK_SIZE
_jobs = {}
_jobs_file_locked = threading.RLock()
# Общий пул одновременных скачиваний на сервер (файл-зеркала и массовые).
_mirror_slots = threading.BoundedSemaphore(Config.MAX_CONCURRENT_MIRRORS)


# ---------------------------------------------------------------- helpers

def human_size(n):
    if not n:
        return "—"
    size = float(n)
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % int(size)
    return "%.1f %s" % (size, units[i])


def _basename(url):
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "download"


def get_file(catalog, row_idx, file_idx):
    """Возвращает (row, file) по каноническим индексам в списке каталога."""
    rows = store.ordered_rows(catalog)
    if not 0 <= row_idx < len(rows):
        return None, None
    files = rows[row_idx].get("files", [])
    if not 0 <= file_idx < len(files):
        return None, None
    return rows[row_idx], files[file_idx]


# ------------------------------------------------------------------ jobs

def _jobs_path():
    return os.path.join(Config.DATA_DIR, "jobs.json")


def _save_jobs():
    tmp = _jobs_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_jobs, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _jobs_path())


def _load_jobs():
    try:
        with open(_jobs_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    for jid, job in data.items():
        if job.get("state") in ("pending", "running"):
            job["state"] = "interrupted"
            job["message"] = "контейнер перезапускался, повторите задачу"
    return data


def _set_job(job_id, **kw):
    with _jobs_file_locked:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(kw)
        _save_jobs()


def jobs():
    with _jobs_file_locked:
        return dict(_jobs)


def running_job_for(url):
    for j in _jobs.values():
        if j.get("url") == url and j.get("state") in ("pending", "running"):
            return j["id"]
    return None


def _create_file_job(catalog, row_idx, file_idx):
    """Создаёт (или находит уже активную) задачу зеркалирования одного файла.

    Возвращает (job_id, fresh, err): fresh=True — задача создана нами и её нужно
    запускать; fresh=False — уже активна задача с этим URL, запускать не надо.
    """
    row, file_ = get_file(catalog, row_idx, file_idx)
    if row is None or file_ is None:
        return None, False, "файл не найден в индексе (возможно, каталог обновился)"
    if not Config.ENABLE_MIRROR:
        return None, False, "зеркалирование отключено (ENABLE_MIRROR=false)"
    url = file_["url"]
    existing = running_job_for(url)
    if existing:
        return existing, False, None
    safe_id = "".join(c for c in row.get("id", "") if c.isalnum() or c in "-_") or "item"
    d = os.path.join(Config.CACHE_DIR, catalog, safe_id)
    os.makedirs(d, exist_ok=True)
    final = os.path.join(d, _basename(url))
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "catalog": catalog,
        "row": row_idx,
        "file": file_idx,
        "url": url,
        "name": row.get("name", ""),
        "label": file_.get("label", _basename(url)),
        "final": final,
        "state": "pending",
        "total": file_.get("size"),
        "got": 0,
        "started": None,
        "finished": None,
        "message": "",
    }
    with _jobs_file_locked:
        _jobs[job_id] = job
        _save_jobs()
    return job_id, True, None


def start_mirror(catalog, row_idx, file_idx):
    job_id, fresh, err = _create_file_job(catalog, row_idx, file_idx)
    if err:
        return None, err
    if fresh:
        threading.Thread(target=_run_mirror, args=(job_id,), daemon=True).start()
    return job_id, None


def _run_mirror(job_id):
    job = _jobs.get(job_id)
    if job is None:
        return
    final = job["final"]
    part = final + ".part"
    lockfile = final + ".lock"
    with open(lockfile, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            if _is_cached(final, job.get("total")):
                _set_job(job_id, state="done", got=job.get("total") or 0, finished=store.now(),
                         message="файл уже есть в кэше")
                return
            _set_job(job_id, state="running", started=store.now(), message="скачивание…", got=0)
            _mirror_slots.acquire()
            try:
                _fetch_to_file(job_id, job, part, final)
            finally:
                _mirror_slots.release()
        except Exception as exc:
            try:
                os.remove(part)
            except OSError:
                pass
            _set_job(job_id, state="error", message=str(exc), finished=store.now())
        finally:
            try:
                os.remove(lockfile)
            except OSError:
                pass


# --------------------------------------------------------- массовое зеркало

def start_bulk_mirror(catalog, key, items, name):
    """Создаёт фоновую задачу массового зеркалирования набора файлов.

    items — список (catalog, row_idx, file_idx) в каноническом порядке каталогов.
    key — идентификатор для дедупликации активных задач (например версия ThinPro).
    Возвращает (job_id, err).
    """
    if not Config.ENABLE_MIRROR:
        return None, "зеркалирование отключено (ENABLE_MIRROR=false)"
    if not items:
        return None, "не найдено ни одного файла"
    dedupe = (catalog, key)
    for j in _jobs.values():
        if j.get("kind") == "bulk" and (j.get("catalog"), j.get("key")) == dedupe \
                and j.get("state") in ("pending", "running"):
            return j["id"], None
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "kind": "bulk",
        "catalog": catalog,
        "key": key,
        "name": name,
        "items": items,
        "state": "pending",
        "total": len(items),
        "got": 0,
        "failed": 0,
        "started": None,
        "finished": None,
        "message": "",
    }
    with _jobs_file_locked:
        _jobs[job_id] = job
        _save_jobs()
    threading.Thread(target=_run_bulk, args=(job_id,), daemon=True).start()
    return job_id, None


def _run_bulk(bulk_id):
    job = _jobs.get(bulk_id)
    if job is None:
        return
    _set_job(bulk_id, state="running", started=store.now(), message="0/%d" % job["total"])
    total = job["total"]
    got = failed = 0
    for catalog, row_idx, file_idx in job["items"]:
        cur = _jobs.get(bulk_id)
        if cur is None or cur.get("stop"):
            _set_job(bulk_id, state="cancelled", message="остановлено пользователем",
                     finished=store.now())
            return
        sub_id, fresh, err = _create_file_job(catalog, row_idx, file_idx)
        if err or sub_id is None:
            failed += 1
        elif fresh:
            _run_mirror(sub_id)
            if (_jobs.get(sub_id) or {}).get("state") == "done":
                got += 1
            else:
                failed += 1
        else:
            got += 1
        _set_job(bulk_id, got=got, failed=failed, message="%d/%d" % (got + failed, total))
    state = "done" if failed == 0 else "done_with_errors"
    _set_job(bulk_id, state=state, message="готово (%d ошибок)" % failed if failed else "готово",
             finished=store.now())


def cancel_job(job_id):
    """Отменяет массовое зеркалирование (файловые задачи не прерываем)."""
    with _jobs_file_locked:
        job = _jobs.get(job_id)
        if job is None:
            return False
        if job.get("kind") != "bulk" or job.get("state") not in ("pending", "running"):
            return False
        job["stop"] = True
        job["state"] = "cancelling"
        _save_jobs()
        return True


def _is_cached(final, total):
    if not os.path.exists(final):
        return False
    if total:
        try:
            return os.path.getsize(final) == total
        except OSError:
            return False
    return True


def _fetch_to_file(job_id, job, part, final):
    url = job["url"]
    r = _open_remote(url, headers={"Accept-Encoding": "identity"}, timeout=(Config.CONNECT_TIMEOUT, Config.READ_TIMEOUT * 8))
    if r.status_code >= 400:
        r.close()
        raise RuntimeError("HP вернул HTTP %s" % r.status_code)
    cl = r.headers.get("Content-Length")
    total = job.get("total") if job.get("total") else (int(cl) if cl else None)
    got = 0
    with open(part, "wb") as out:
        try:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                out.write(chunk)
                got += len(chunk)
                if got % (CHUNK * 16) == 0:
                    _set_job(job_id, got=got)
        finally:
            r.close()
    if total is not None and got != total:
        raise RuntimeError("размер не совпал: ожидалось %d, скачано %d" % (total, got))
    os.replace(part, final)
    _set_job(job_id, state="done", got=got, finished=store.now(), message="готово")


# ---------------------------------------------------------------- network

def _open_remote(url, headers, timeout):
    """GET со стримингом и фолбэком https -> http при ошибке соединения."""
    try:
        return requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    except requests.exceptions.ConnectionError:
        alt = url.replace("https://", "http://", 1)
        if alt != url:
            return requests.get(alt, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
        raise


def proxy_response(catalog, row_idx, file_idx):
    """Формирует ответ с файлом: из кэша или прямым стримингом с HP."""
    row, file_ = get_file(catalog, row_idx, file_idx)
    if row is None or file_ is None:
        abort(404)
    url = file_["url"]
    name = file_.get("label") or _basename(url)
    final = None
    if Config.ENABLE_MIRROR:
        safe_id = "".join(c for c in row.get("id", "") if c.isalnum() or c in "-_") or "item"
        candidate = os.path.join(Config.CACHE_DIR, catalog, safe_id, name)
        if _is_cached(candidate, file_.get("size")):
            final = candidate
    if final:
        resp = send_file(final, as_attachment=True, download_name=name, conditional=True)
        return resp

    req_headers = {"Accept-Encoding": "identity"}
    rng = request.headers.get("Range")
    if rng:
        req_headers["Range"] = rng
    try:
        r = _open_remote(url, headers=req_headers,
                         timeout=(Config.CONNECT_TIMEOUT, Config.READ_TIMEOUT))
    except requests.RequestException:
        abort(502, description="не удалось подключиться к HP FTP")
    if r.status_code >= 400:
        r.close()
        abort(502, description="HP вернул HTTP %s" % r.status_code)

    def gen():
        try:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if chunk:
                    yield chunk
        finally:
            r.close()

    resp = Response(stream_with_context(gen()), status=r.status_code)
    for hd in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
               "ETag", "Last-Modified", "Cache-Control"):
        if r.headers.get(hd):
            resp.headers[hd] = r.headers[hd]
    resp.headers.setdefault("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
    resp.headers["Content-Disposition"] = 'attachment; filename="%s"' % name
    return resp


# Восстанавливаем состояние задач зеркала после рестарта.
_jobs.update(_load_jobs())