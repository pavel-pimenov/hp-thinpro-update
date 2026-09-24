"""Парсеры каталогов HP EasyUpdate.

Два известных формата:
  * hpcatalog.xml      - корень <ThinUpdate>, образы ОС (Linux ThinPro / Windows IoT)
  * addoncatalog.xml   - корень <AddOns>, аддоны/приложения

Относительные пути файлов резолвятся против URL самого каталога
(например "../../../tcdebian/OSImages/T9X90015.dd.gz"),
urllib.parse.urljoin умеет корректно подниматься по "..".
"""

import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit


def _txt(el, name):
    node = el.find(name)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _num(value):
    if value is None:
        return None
    value = str(value).strip().replace(",", ".")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def _basename(path):
    return urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]


def parse_text(xml_text, catalog_url):
    root = ET.fromstring(xml_text)
    tag = root.tag.lower()
    if tag in ("addons", "addoncatalog", "easyupdatecatalog"):
        return parse_addons(root, catalog_url)
    if tag in ("thinupdate", "hpcatalog"):
        return parse_thinupdate(root, catalog_url)
    rows = parse_thinupdate(root, catalog_url)
    if not rows:
        rows = parse_addons(root, catalog_url)
    return rows


def parse_thinupdate(root, catalog_url):
    rows = []
    platforms = root.find("Platforms")
    if platforms is None:
        platforms = root
    for platform in platforms.findall("platform"):
        pid = platform.get("ID", "")
        pdesc = platform.get("Description", "")
        for img in platform.findall("images/image"):
            os_type = _txt(img, "ostype").lower()
            name = img.get("Description", "")
            if os_type == "linux":
                os_family = "smart-zero" if "smart zero" in name.lower() else "thinpro"
            elif os_type == "windows":
                os_family = "windows"
            else:
                os_family = os_type
            files = []
            froot = img.find("files")
            if froot is not None:
                for f in froot.findall("file"):
                    path = (f.text or "").strip()
                    if not path:
                        continue
                    files.append(_make_file("image", path, f.get("size"), catalog_url,
                                            f.get("hash") or "", f.get("algo") or ""))
            usb = img.find("USBdrive")
            if usb is not None:
                for f in usb.findall("files/file"):
                    path = (f.text or "").strip()
                    if not path:
                        continue
                    files.append(_make_file("usb", path, f.get("size"), catalog_url))
            row = {
                "id": img.get("ID", ""),
                "name": img.get("Description", ""),
                "platform_id": pid,
                "platform": pdesc,
                "os_version": _txt(img, "osversion"),
                "os_type": _txt(img, "ostype"),
                "architecture": _txt(img, "architecture"),
                "available": img.get("AvailableDate", ""),
                "expires": img.get("ExpirationDate", ""),
                "linux": os_type == "linux",
                "os_family": os_family,
                "files": files,
            }
            row["main_file"] = next((x for x in row["files"] if x["kind"] == "image"), None)
            rows.append(row)
    return rows


def parse_addons(root, catalog_url):
    rows = []
    for addon in root.findall("addon"):
        oses = [
            {
                "version": o.get("Version", ""),
                "type": o.get("Type", ""),
                "description": o.get("Description", ""),
            }
            for o in addon.findall("OSes/OS")
        ]
        linux = any(o["type"].strip().lower() == "linux" for o in oses)
        tp_version = next((o.get("version", "") for o in oses if o.get("type", "").strip().lower() == "linux"), "")
        platforms = [p.get("ID", "") for p in addon.findall("SupportedPlatforms/platform")]
        files = []
        froot = addon.find("files")
        if froot is not None:
            for child in froot:
                path = (child.text or "").strip()
                if not path:
                    continue
                files.append(_make_file(child.tag, path, child.get("size"), catalog_url))
        rows.append({
            "id": addon.get("ID", ""),
            "name": addon.get("Description", ""),
            "version": addon.get("Version", ""),
            "architecture": _txt(addon, "architecture"),
            "install_command": _txt(addon, "install_command"),
            "product_version": _txt(addon, "ProductVersion"),
            "available": addon.get("AvailableDate", ""),
            "expires": addon.get("ExpirationDate", ""),
            "platforms": platforms,
            "oses": oses,
            "linux": linux,
            "tp_version": tp_version,
            "os_family": "thinpro" if linux else "windows",
            "files": files,
        })
    return rows


def _make_file(kind, path, size, catalog_url, hash_="", algo=""):
    return {
        "kind": kind,
        "label": _basename(path),
        "path": path,
        "url": urljoin(catalog_url, path),
        "size": _num(size),
        "hash": hash_ or "",
        "algo": algo or "",
    }