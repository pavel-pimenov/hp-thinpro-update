import json
import os
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for

from . import download, store
from .config import Config

bp = Blueprint("app", __name__)


def human_size(n):
    return download.human_size(n)


def iso_date(s):
    """MM/DD/YYYY (US-формат из каталога HP) -> YYYY-MM-DD."""
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _catalogs():
    return store.index().get("catalogs", {})


@bp.get("/healthz")
def health():
    return "ok"


@bp.get("/")
def index():
    cats = _catalogs()
    img_count = len(cats.get("hp", {}).get("rows", []))
    addon_count = len(cats.get("addons", {}).get("rows", []))
    deb_count = len(store.deb_entries())
    xar_count = len(store.xar_entries())
    summary = store.last_refresh()
    running = sum(1 for j in download.jobs().values() if j.get("state") in ("pending", "running"))
    return render_template(
        "index.html",
        img_count=img_count,
        addon_count=addon_count,
        deb_count=deb_count,
        xar_count=xar_count,
        cats=cats,
        summary=summary,
        running=running,
        has_data=store.has_data(),
    )


@bp.post("/refresh")
def refresh():
    summary = store.refresh()
    if summary["errors"]:
        flash("Ошибки при обновлении: " + "; ".join(f"{k}: {v}" for k, v in summary["errors"].items()), "error")
    else:
        flash("Каталоги обновлены: " + ", ".join(f"{k}={v}" for k, v in summary["counts"].items()), "ok")
    return redirect(url_for("app.index"))


@bp.get("/catalogs/<name>.xml")
def raw_catalog(name):
    if name not in Config.CATALOGS:
        abort(404)
    path = _raw_path_ok(name)
    return send_file(path, mimetype="application/xml", as_attachment=True,
                     download_name=name + ".xml")


def _raw_path_ok(name):
    path = os.path.join(store.catalogs_dir(), name + ".xml")
    if not os.path.exists(path):
        abort(404, description="каталог ещё не скачан — нажмите «Обновить каталоги»")
    return path


def _file_indexes(rows):
    for i, r in enumerate(rows):
        for j, f in enumerate(r.get("files", [])):
            f["_j"] = j
    return rows


def _display_rows(catalog, q="", platform="", arch=""):
    """Возвращает ряды для показа (отфильтрованные) с корректными индексами _i
    из канонического (одинакового для UI и скачивания) порядка."""
    full = store.ordered_rows(catalog)
    pos = {id(r): i for i, r in enumerate(full)}
    rows = full
    if catalog == "hp":
        if platform:
            rows = [r for r in rows if r.get("platform_id") == platform]
        if q:
            rows = [r for r in rows if q in (r.get("id") + " " + r.get("name") + " " + r.get("platform")).lower()]
    else:
        if arch:
            rows = [r for r in rows if r.get("architecture") == arch]
        if q:
            rows = [r for r in rows
                    if q in (r.get("id") + " " + r.get("name") + " " + " ".join(r.get("platforms", []))).lower()]
    _file_indexes(rows)
    for r in rows:
        r["_i"] = pos[id(r)]
    return rows


@bp.get("/images")
def images():
    q = (request.args.get("q") or "").strip().lower()
    platform = (request.args.get("platform") or "").strip()
    rows = _display_rows("hp", q=q, platform=platform)
    platforms = sorted({r.get("platform_id") for r in store.ordered_rows("hp") if r.get("platform_id")})
    return render_template("images.html", rows=rows, platforms=platforms, q=q, platform=platform)


@bp.get("/addons")
def addons():
    q = (request.args.get("q") or "").strip().lower()
    arch = (request.args.get("arch") or "").strip()
    shown = _display_rows("addons", q=q, arch=arch)
    shown_ids = {id(r) for r in shown}
    groups = []
    for version, rows in store.addon_groups():
        shown_rows = [r for r in shown if id(r) in shown_ids and r.get("tp_version") == version]
        file_count = sum(len(r.get("files", [])) for r in rows)
        total_size = sum(
            f.get("size") or 0 for r in rows for f in r.get("files", []) if f.get("size")
        )
        groups.append({
            "version": version,
            "rows": shown_rows,
            "addon_count": len(rows),
            "file_count": file_count,
            "total_size": total_size,
        })
    arches = sorted({r.get("architecture") for r in store.ordered_rows("addons") if r.get("architecture")})
    return render_template("addons.html", groups=groups, arches=arches, q=q, arch=arch)


@bp.post("/mirror_all/<version>")
def mirror_all(version):
    if version not in {v for v, _ in store.addon_groups()}:
        abort(404, description="версия не найдена")
    items = []
    for i, r in enumerate(store.ordered_rows("addons")):
        if (r.get("tp_version") or "") != version:
            continue
        for j, f in enumerate(r.get("files", [])):
            items.append(("addons", i, j))
    job_id, err = download.start_bulk_mirror(
        "addons", version, items, "Массовое зеркалирование ThinPro %s" % version)
    if err:
        flash(err, "error")
        return redirect(request.referrer or url_for("app.index"))
    flash("Массовое зеркалирование ThinPro %s запущено (%d файлов)" % (version, len(items)), "ok")
    return redirect(url_for("app.jobs"))


@bp.get("/debs")
def debs():
    entries = store.deb_entries()
    q = (request.args.get("q") or "").strip().lower()
    if q:
        entries = [e for e in entries
                   if q in (e["label"] + " " + e["source"] + " " + e["source_id"] +
                            e["version"] + " " + e["arch"]).lower()]
    entries.sort(key=lambda e: (e["version"], e["label"].lower()))
    total_size = sum(e["size"] or 0 for e in entries if e["size"])
    return render_template("debs.html", entries=entries, q=q, total_size=total_size)


@bp.post("/mirror_all_debs")
def mirror_all_debs():
    entries = store.deb_entries()
    items = [(e["catalog"], e["row"], e["file"]) for e in entries]
    job_id, err = download.start_bulk_mirror(
        "debs", "all-debs", items, "Все пакеты .deb")
    if err:
        flash(err, "error")
        return redirect(request.referrer or url_for("app.index"))
    flash("Массовое зеркалирование всех .deb запущено (%d файлов)" % len(items), "ok")
    return redirect(url_for("app.jobs"))


@bp.get("/xars")
def xars():
    entries = store.xar_entries()
    q = (request.args.get("q") or "").strip().lower()
    if q:
        entries = [e for e in entries
                   if q in (e["label"] + " " + e["source"] + " " + e["source_id"] +
                            e["version"] + " " + e["arch"]).lower()]
    entries.sort(key=lambda e: (e["version"], e["label"].lower()))
    total_size = sum(e["size"] or 0 for e in entries if e["size"])
    return render_template("xars.html", entries=entries, q=q, total_size=total_size)


@bp.post("/mirror_all_xars")
def mirror_all_xars():
    entries = store.xar_entries()
    items = [(e["catalog"], e["row"], e["file"]) for e in entries]
    job_id, err = download.start_bulk_mirror(
        "xars", "all-xars", items, "Все пакеты .xar")
    if err:
        flash(err, "error")
        return redirect(request.referrer or url_for("app.index"))
    flash("Массовое зеркалирование всех .xar запущено (%d файлов)" % len(items), "ok")
    return redirect(url_for("app.jobs"))


@bp.post("/cancel/<job_id>")
def cancel(job_id):
    if download.cancel_job(job_id):
        flash("Массовое зеркалирование остановлено", "ok")
    else:
        flash("Отменить можно только активное массовое зеркалирование", "error")
    return redirect(url_for("app.jobs"))


@bp.get("/file/<catalog>/<int:row>/<int:file>")
def file_download(catalog, row, file):
    if catalog not in ("hp", "addons"):
        abort(404)
    return download.proxy_response(catalog, row, file)


@bp.post("/mirror/<catalog>/<int:row>/<int:file>")
def mirror(catalog, row, file):
    if catalog not in ("hp", "addons"):
        abort(404)
    job_id, err = download.start_mirror(catalog, row, file)
    if err:
        flash(err, "error")
        return redirect(request.referrer or url_for("app.index"))
    flash("Задача зеркалирования запущена", "ok")
    return redirect(url_for("app.jobs"))


@bp.get("/jobs")
def jobs():
    jobs_ = sorted(download.jobs().values(), key=lambda j: j.get("started") or "", reverse=True)
    any_running = any(j.get("state") in ("pending", "running") for j in jobs_)
    return render_template("jobs.html", jobs=jobs_, any_running=any_running)


@bp.get("/api/index.json")
def api_index():
    return current_app.response_class(
        json.dumps(_catalogs(), ensure_ascii=False, indent=2),
        mimetype="application/json",
    )