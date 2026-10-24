#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MINDUSTRY // LAUNCHER — терминальный лаунчер для Mindustry (бесплатная версия).

* версии берутся из https://github.com/Anuken/Mindustry/releases
* контейнер = отдельная копия настроек + отдельные сохранения/моды (своя папка data)
* настройки лежат в settings.ini, контейнеры — в containers/<имя>/container.ini

Нужны Python 3.8+ (tkinter входит в поставку) и Java 17+. Сторонних библиотек нет.
"""
import base64
import configparser
import io
import json
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tarfile
import traceback
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont

try:  # необязательно: нужен Pillow, чтобы показывать JPEG-превью мастерской
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

APP_VER = "1.0"
API_URL = "https://api.github.com/repos/Anuken/Mindustry/releases?per_page=100&page={page}"
JAR_NAMES = ("Mindustry.jar", "desktop-release.jar")
UA = {"User-Agent": "MindustryLauncher/" + APP_VER}

IS_WIN = platform.system() == "Windows"
NOWIN = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

CONF_DIR = os.path.join(os.path.expanduser("~"), ".mindustry-launcher")
SETTINGS_FILE = os.path.join(CONF_DIR, "settings.ini")
CACHE_FILE = os.path.join(CONF_DIR, "releases_cache.json")
GLOBAL_STATS = os.path.join(CONF_DIR, "stats.json")  # общее время игры и число запусков

THEMES = {
    "AMBER":    dict(bg="#0b0905", panel="#120e06", fg="#ffc21a", dim="#8a6a14", acc="#ff3b2f"),
    "PHOSPHOR": dict(bg="#040a05", panel="#07110a", fg="#33ff66", dim="#1b8a38", acc="#ffe14d"),
    "ICE":      dict(bg="#04080c", panel="#07111a", fg="#4fd8ff", dim="#21728a", acc="#ff4fa3"),
    "CRIMSON":  dict(bg="#0c0505", panel="#150808", fg="#ff5252", dim="#8f2c2c", acc="#ffc21a"),
    "MONO":     dict(bg="#0a0a0a", panel="#111111", fg="#e6e6e6", dim="#777777", acc="#ffffff"),
}

NAV = [("containers", "CONTAINERS.RSU"), ("versions", "VERSIONS.DAT"),
       ("workshop", "WORKSHOP.SYS"), ("settings", "SETTINGS.INI")]

APP_ID = 1127400  # Mindustry в Steam
STEAMCMD = {
    "Windows": ("https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip", "steamcmd.exe"),
    "Linux": ("https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz", "steamcmd.sh"),
    "Darwin": ("https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz", "steamcmd.sh"),
}
MOD_MARKERS = ("mod.json", "mod.hjson", "plugin.json", "plugin.hjson")

SECTIONS = {"install_dir": "launcher", "theme": "launcher", "show_pre": "launcher", "ws_sort": "launcher",
            "java_path": "java", "ram_min": "java", "ram_max": "java",
            "jvm_args": "java", "game_args": "java", "steam_user": "steam"}
DEFAULTS = {"install_dir": CONF_DIR, "theme": "AMBER", "show_pre": "0", "ws_sort": "name_asc",
            "java_path": "", "ram_min": "256M", "ram_max": "2G",
            "jvm_args": "", "game_args": "", "steam_user": ""}
CKEYS = ("version", "ram_min", "ram_max", "jvm_args", "game_args", "icon")


# ====================================================================== утилиты
class Settings:
    def __init__(self):
        self.d = dict(DEFAULTS)
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(SETTINGS_FILE, encoding="utf-8")
            for k, sec in SECTIONS.items():
                if cp.has_option(sec, k):
                    self.d[k] = cp.get(sec, k)
        except Exception:
            pass
        if self.d["theme"] not in THEMES:
            self.d["theme"] = "AMBER"
        if self.d["ws_sort"] not in ("name_asc", "name_desc", "type"):
            self.d["ws_sort"] = "name_asc"

    def get(self, k):
        return self.d[k]

    def set(self, k, v):
        self.d[k] = v

    def save(self):
        cp = configparser.ConfigParser(interpolation=None)
        for k, sec in SECTIONS.items():
            if not cp.has_section(sec):
                cp.add_section(sec)
            cp.set(sec, k, self.d[k])
        try:
            os.makedirs(CONF_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                cp.write(f)
        except OSError:
            pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def mix(c1, c2, a):
    p1 = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    p2 = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * a) for x, y in zip(p1, p2))


def parse_ram(s):
    """'512', '512M', '2G' -> МБ (int) или None."""
    m = re.fullmatch(r"(\d+)\s*([MG])?B?", (s or "").strip().upper())
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2) == "G" else n


def fmt_ram(mb):
    return f"{mb // 1024}G" if mb % 1024 == 0 else f"{mb}M"


def split_args(s):
    s = (s or "").strip()
    if not s:
        return []
    try:
        parts = shlex.split(s, posix=not IS_WIN)
    except ValueError:
        parts = s.split()
    if IS_WIN:
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return parts


def fmt_size(n):
    return f"{n / 1024 / 1024:.1f}MB" if n else "?"


def fmt_dur(sec):
    m = int(sec // 60)
    if m < 1:
        return "0m" if sec < 1 else "<1m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def container_stats(root, name):
    """-> (секунд наиграно, число запусков) для контейнера."""
    d = load_json(os.path.join(root, "containers", name, "stats.json"), {})
    return float(d.get("seconds", 0) or 0), int(d.get("launches", 0) or 0)


def add_playtime(root, name, secs=0.0, launches=0):
    """Добавляет время/запуски в статистику контейнера и в общий счётчик."""
    if secs <= 0 and not launches:
        return
    for path in (os.path.join(root, "containers", name, "stats.json"), GLOBAL_STATS):
        if path != GLOBAL_STATS and not os.path.isdir(os.path.dirname(path)):
            continue
        d = load_json(path, {})
        d["seconds"] = float(d.get("seconds", 0) or 0) + max(0.0, secs)
        d["launches"] = int(d.get("launches", 0) or 0) + launches
        save_json(path, d)


def safe_tag(tag):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in tag)


def versions_dir(root):
    return os.path.join(root, "versions")


def containers_dir(root):
    return os.path.join(root, "containers")


def version_dir(root, tag):
    return os.path.join(versions_dir(root), safe_tag(tag))


def jar_path(root, tag):
    return os.path.join(version_dir(root, tag), "Mindustry.jar")


def is_installed(root, tag):
    p = jar_path(root, tag)
    return os.path.isfile(p) and os.path.getsize(p) > 0


def container_dir(root, name):
    return os.path.join(containers_dir(root), name)


def load_containers(root):
    res = []
    base = containers_dir(root)
    try:
        names = sorted(os.listdir(base), key=str.lower)
    except OSError:
        return res
    for n in names:
        ini = os.path.join(base, n, "container.ini")
        if not os.path.isfile(ini):
            continue
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(ini, encoding="utf-8")
        except Exception:
            continue
        c = {"name": n}
        for k in CKEYS:
            c[k] = cp.get("container", k, fallback="")
        res.append(c)
    return res


def save_container(root, c):
    d = container_dir(root, c["name"])
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    cp = configparser.ConfigParser(interpolation=None)
    cp["container"] = {k: c.get(k, "") for k in CKEYS}
    with open(os.path.join(d, "container.ini"), "w", encoding="utf-8") as f:
        cp.write(f)


def installed_tags(root, releases):
    tags = [r["tag"] for r in releases if is_installed(root, r["tag"])]
    try:
        for n in os.listdir(versions_dir(root)):
            if n not in tags and is_installed(root, n):
                tags.append(n)
    except OSError:
        pass
    return tags


def find_java(custom=""):
    if custom and os.path.isfile(custom):
        return custom
    p = shutil.which("java")
    if p:
        return p
    jh = os.environ.get("JAVA_HOME")
    if jh:
        p = os.path.join(jh, "bin", "java.exe" if IS_WIN else "java")
        if os.path.isfile(p):
            return p
    return None


def java_version(path):
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL, creationflags=NOWIN)
        out = (r.stderr or r.stdout).strip().splitlines()
        for line in out:  # пропускаем "Picked up JAVA_TOOL_OPTIONS: ..."
            m = re.match(r'(\S+)\s+version\s+"([^"]+)"', line.strip())
            if m:
                return f"{m.group(1)} {m.group(2)}"
        return next((l.strip() for l in out if not l.startswith("Picked up")), "?")[:40]
    except Exception:
        return "?"


def open_path(path):
    os.makedirs(path, exist_ok=True)
    if IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def fetch_releases(max_pages=3):
    result = []
    for page in range(1, max_pages + 1):
        req = urllib.request.Request(API_URL.format(page=page), headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not data:
            break
        for rel in data:
            asset = next((a for a in rel.get("assets", []) if a["name"] in JAR_NAMES), None)
            if asset is None:
                continue
            result.append({
                "tag": rel["tag_name"],
                "date": (rel.get("published_at") or "")[:10],
                "prerelease": bool(rel.get("prerelease")),
                "url": asset["browser_download_url"],
                "size": asset.get("size", 0),
            })
        if len(data) < 100:
            break
    return result


def safe_file(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "").strip(" .")[:60]


def parse_workshop_ids(text):
    """Из строки со ссылками и/или числами достаёт ID элементов мастерской."""
    ids = []
    for tok in re.split(r"[\s,;]+", (text or "").strip()):
        m = (re.search(r"[?&]id=(\d+)", tok) or re.search(r"/filedetails/(\d+)", tok)
             or re.fullmatch(r"(\d{5,})", tok))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def workshop_details(wid):
    """Публичный API Steam (ключ не нужен). -> {'title','app'} или None."""
    body = urllib.parse.urlencode({"itemcount": 1, "publishedfileids[0]": wid}).encode()
    req = urllib.request.Request(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    d = j["response"]["publishedfiledetails"][0]
    if d.get("result") != 1:
        return None
    return {"title": d.get("title") or "",
            "app": int(d.get("consumer_app_id") or d.get("creator_app_id") or 0),
            "visibility": int(d.get("visibility") or 0), "banned": bool(d.get("banned"))}


BROWSE_SORTS = {  # название -> (browsesort, доп. параметры)
    "TRENDING": ("trend", "&days=7"),
    "NEWEST": ("mostrecent", ""),
    "POPULAR": ("totaluniquesubscribers", "&days=-1"),
    "UPDATED": ("lastupdated", ""),
}


def fmt_num(n):
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def clean_bbcode(text):
    text = re.sub(r"\[/?[A-Za-z0-9*]+(?:=[^\]]*)?\]", "", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:700]


def fetch_bytes(url, timeout=20, limit=6_000_000):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read(limit)


def workshop_search(query, sort, page):
    """Страница обзора мастерской Mindustry -> список ID (в порядке выдачи)."""
    key, extra = BROWSE_SORTS[sort]
    url = ("https://steamcommunity.com/workshop/browse/?appid=%d&section=readytouseitems"
           "&numperpage=30&p=%d&browsesort=%s&actualsort=%s%s&searchtext=%s"
           % (APP_ID, page, key, key, extra, urllib.parse.quote(query)))
    req = urllib.request.Request(url, headers=dict(UA, **{"Accept-Language": "en"}))
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")
    ids = []
    for m in re.finditer(r"sharedfiles/filedetails/\?id=(\d+)", html):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids[:30]


def workshop_details_many(ids):
    """Один запрос к публичному API Steam на все ID. -> {id: данные}"""
    fields = {"itemcount": len(ids)}
    for i, w in enumerate(ids):
        fields[f"publishedfileids[{i}]"] = w
    req = urllib.request.Request(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data=urllib.parse.urlencode(fields).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.loads(r.read().decode("utf-8"))
    res = {}
    for d in j["response"]["publishedfiledetails"]:
        if d.get("result") != 1:
            continue
        res[str(d.get("publishedfileid"))] = {
            "title": d.get("title") or "?",
            "app": int(d.get("consumer_app_id") or d.get("creator_app_id") or 0),
            "desc": clean_bbcode(d.get("description", "")),
            "preview": d.get("preview_url", ""),
            "tags": [t.get("tag", "") for t in d.get("tags", []) if t.get("tag")],
            "subs": int(d.get("subscriptions") or 0),
            "size": int(d.get("file_size") or 0),
            "updated": int(d.get("time_updated") or 0),
        }
    return res


def fetch_preview(url):
    """Превью предмета; сначала просим у Steam уменьшенную копию."""
    if not url:
        return b""
    small = url + ("&" if "?" in url else "?") + "imw=256&imh=256&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=false"
    try:
        return fetch_bytes(small)
    except Exception:
        return fetch_bytes(url)


def run_steamcmd(exe, args, log):
    """Запускает steamcmd, пишет важные строки в log, возвращает строки с ошибками."""
    kw = dict(cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if IS_WIN:
        kw["creationflags"] = NOWIN
    p = subprocess.Popen([exe] + args, **kw)
    errors, updating, last = [], False, 0.0
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if re.match(r"^\[\s*\d+%\]", line):
            if not updating:
                updating = True
                log("[steamcmd] UPDATING ITSELF (FIRST RUN TAKES A FEW MINUTES) ...", "game")
            continue
        if line.startswith("Update state"):
            if time.time() - last < 3:
                continue
            last = time.time()
        if "ERROR" in line.upper() or "failed" in line.lower():
            errors.append(line)
        log("[steamcmd] " + line, "game")
    p.wait()
    return errors


def steam_dir(root):
    """SteamCMD падает с 'incompatible path', если в пути есть не-ASCII символы
    (например, русское имя пользователя). Тогда используем безопасную папку."""
    p = os.path.join(root, "steamcmd")
    if p.isascii():
        return p
    if IS_WIN:
        base = os.environ.get("ProgramData") or "C:\\ProgramData"
        alt = os.path.join(base, "MindustryLauncher", "steamcmd")
        return alt if alt.isascii() else os.path.join("C:\\", "MindustryLauncher", "steamcmd")
    return os.path.join(os.sep, "tmp", "mindustry-launcher", "steamcmd")


def ensure_steamcmd(dirpath, log):
    url, exe_name = STEAMCMD.get(platform.system(), (None, None))
    if not url:
        raise RuntimeError("UNSUPPORTED OS")
    exe = os.path.join(dirpath, exe_name)
    if os.path.isfile(exe):
        return exe
    os.makedirs(dirpath, exist_ok=True)
    log("DOWNLOADING STEAMCMD (OFFICIAL VALVE TOOL) ...")
    pkg = os.path.join(dirpath, "steamcmd.pkg")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, \
            open(pkg, "wb") as f:
        shutil.copyfileobj(r, f)
    if url.endswith(".zip"):
        with zipfile.ZipFile(pkg) as z:
            z.extractall(dirpath)
    else:
        with tarfile.open(pkg) as tf:
            tf.extractall(dirpath)
    os.remove(pkg)
    if not os.path.isfile(exe):
        raise RuntimeError("steamcmd NOT FOUND IN ARCHIVE")
    if not IS_WIN:
        os.chmod(exe, 0o755)
    log("STEAMCMD INSTALLED. FIRST START (SELF-UPDATE) ...")
    run_steamcmd(exe, ["+quit"], log)
    return exe


def install_workshop_item(src, data, title, wid):
    """Раскладывает скачанный элемент по папкам данных Mindustry.
    -> (тип, [пути относительно data])"""
    base = safe_file(title) or f"item_{wid}"
    found = [(dp, fn) for dp, _, fns in os.walk(src) for fn in fns]
    mod_dp = next((dp for dp, fn in found if fn.lower() in MOD_MARKERS), None)
    out, kinds = [], []
    if mod_dp is not None:
        shutil.copytree(mod_dp, os.path.join(data, "mods", base), dirs_exist_ok=True)
        out.append(os.path.join("mods", base))
        kinds.append("mod")
    else:
        files = [(dp, fn) for dp, fn in found if fn.lower().endswith((".msav", ".msch", ".zip", ".jar"))]
        for i, (dp, fn) in enumerate(files):
            ext = os.path.splitext(fn)[1].lower()
            sub, kind = {".msav": ("maps", "map"), ".msch": ("schematics", "schematic")}.get(ext, ("mods", "mod"))
            name = base + ext if len(files) == 1 else f"{base}_{i + 1}{ext}"
            os.makedirs(os.path.join(data, sub), exist_ok=True)
            shutil.copy2(os.path.join(dp, fn), os.path.join(data, sub, name))
            out.append(os.path.join(sub, name))
            kinds.append(kind)
    if not out:
        raise RuntimeError("UNKNOWN CONTENT: " + ", ".join(fn for _, fn in found[:5]))
    return "+".join(sorted(set(kinds))), out


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def import_local_item(path, data):
    """Кладёт скачанный вручную файл (.msav/.msch/.zip/.jar) или папку мода в папки данных
    Mindustry. -> (название, тип, [пути относительно data])"""
    path = path.rstrip("\\/")
    name = os.path.basename(path)
    if os.path.isdir(path):
        mod_dp = next((dp for dp, _, fns in os.walk(path)
                       if any(fn.lower() in MOD_MARKERS for fn in fns)), None)
        if mod_dp is None:
            raise RuntimeError("FOLDER HAS NO mod.json / mod.hjson")
        dst = unique_path(os.path.join(data, "mods", safe_file(name) or "mod"))
        shutil.copytree(mod_dp, dst)
        return name, "mod", [os.path.relpath(dst, data)]
    ext = os.path.splitext(name)[1].lower()
    sub, kind = {".msav": ("maps", "map"), ".msch": ("schematics", "schematic"),
                 ".zip": ("mods", "mod"), ".jar": ("mods", "mod")}.get(ext, (None, None))
    if not sub:
        raise RuntimeError(f"UNSUPPORTED FILE TYPE '{ext}' (USE .msav .msch .zip .jar)")
    if ext == ".zip" and not zipfile.is_zipfile(path):
        raise RuntimeError("NOT A VALID ZIP FILE")
    os.makedirs(os.path.join(data, sub), exist_ok=True)
    dst = unique_path(os.path.join(data, sub, (safe_file(os.path.splitext(name)[0]) or "item") + ext))
    shutil.copy2(path, dst)
    return name, kind, [os.path.relpath(dst, data)]


def is_inside(child, parent):
    try:
        c, p = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([c, p]) == p and c != p
    except ValueError:
        return False


# ====================================================================== виджеты
class TermButton(tk.Label):
    def __init__(self, parent, app, text, command, accent=False, color=None):
        t = app.t
        self._col = color or (t["acc"] if accent else t["fg"])
        self._bg, self._dim = t["bg"], t["dim"]
        self._cmd, self._on, self._act = command, True, False
        super().__init__(parent, text=text, font=app.font_b, bg=self._bg, fg=self._col,
                         padx=8, pady=2, bd=0, highlightthickness=1,
                         highlightbackground=self._col, cursor="hand2")
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<ButtonRelease-1>", self._click)

    def _paint(self, hover=False):
        if not self._on:
            self.config(bg=self._bg, fg=self._dim, highlightbackground=self._dim, cursor="arrow")
            return
        inv = hover or self._act
        self.config(bg=self._col if inv else self._bg, fg=self._bg if inv else self._col,
                    highlightbackground=self._col, cursor="hand2")

    def _click(self, e):
        if self._on and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self._cmd()

    def enable(self, v=True):
        self._on = v
        self._paint()

    def activate(self, v=True):
        self._act = v
        self._paint()

    def set_text(self, s):
        self.config(text=s)


class TermToggle(tk.Label):
    def __init__(self, parent, app, text, value, command):
        self._text, self._val, self._cmd = text, bool(value), command
        super().__init__(parent, font=app.font_b, bg=app.t["bg"], fg=app.t["fg"],
                         cursor="hand2", anchor="w")
        self._render()
        self.bind("<Button-1>", self._click)

    def _render(self):
        self.config(text=("[X] " if self._val else "[ ] ") + self._text)

    def _click(self, e):
        self._val = not self._val
        self._render()
        self._cmd(self._val)


class TermScroll(tk.Canvas):
    def __init__(self, parent, app, command):
        super().__init__(parent, width=int(10 * app.sc), bg=app.t["panel"], highlightthickness=0, bd=0)
        self._cmd, self._first, self._last, self._col = command, 0.0, 1.0, app.t["dim"]
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._draw()

    def _draw(self):
        self.delete("all")
        h = self.winfo_height()
        if self._last - self._first >= 0.999:
            return
        y0, y1 = self._first * h, self._last * h
        self.create_rectangle(2, y0, self.winfo_width() - 2, max(y1, y0 + 14), fill=self._col, outline="")

    def _drag(self, e):
        h = max(self.winfo_height(), 1)
        span = self._last - self._first
        frac = max(0.0, min(1.0 - span, e.y / h - span / 2))
        self._cmd("moveto", frac)


class ChevronBar(tk.Canvas):
    """Лента шевронов: в простое пульсирует от центра, при загрузке заполняется."""

    def __init__(self, parent, app):
        self.app = app
        self.H = int(28 * app.sc)
        super().__init__(parent, height=self.H, bg=app.t["bg"], highlightthickness=1,
                         highlightbackground=app.t["dim"], bd=0)
        self._tick = 0
        self.bind("<Configure>", lambda e: self.draw())
        self.after(120, self._loop)

    def _loop(self):
        try:
            if not self.winfo_exists():
                return
            self._tick += 1
            self.draw()
            self.after(120, self._loop)
        except tk.TclError:
            pass

    def draw(self):
        t = self.app.t
        self.delete("all")
        w = self.winfo_width()
        if w < 40:
            return
        sc = self.app.sc
        cw, cw_in, th = int(24 * sc), int(20 * sc), int(8 * sc)
        n = max(3, (w - 8) // cw)
        x0 = (w - n * cw) // 2
        y0, y1 = int(4 * sc), self.H - int(4 * sc)
        ym = (y0 + y1) / 2
        prog = self.app.progress
        c = (n - 1) / 2
        head = self._tick % (int(c) + 5)
        for i in range(n):
            if prog is not None:
                a, d = (1.0 if (i + 1) / n <= prog + 1e-9 else 0.13), 1
            else:
                dist = abs(i - c)
                k = head - dist
                a = 1.0 if 0 <= k < 1 else .55 if 1 <= k < 2 else .3 if 2 <= k < 3 else .13
                d = -1 if i < c else 1
            x = x0 + i * cw
            pts = [(0, y0), (th, y0), (cw_in, ym), (th, y1), (0, y1), (cw_in - th, ym)]
            if d < 0:
                pts = [(cw_in - px, py) for px, py in pts]
            flat = [v for px, py in pts for v in (x + px, py)]
            self.create_polygon(flat, fill=mix(t["bg"], t["fg"], a), outline="")


# ====================================================================== приложение
class App(tk.Tk):
    def __init__(self):
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        super().__init__()
        self.sc = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.title("MINDUSTRY // LAUNCHER")
        self.geometry(f"{int(1020 * self.sc)}x{int(720 * self.sc)}")
        self.minsize(int(980 * self.sc), int(680 * self.sc))

        self.S = Settings()
        self.t = THEMES[self.S.get("theme")]
        fam = "Courier"
        have = set(tkfont.families(self))
        for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
            if f in have:
                fam = f
                break
        self.font = (fam, 10)
        self.font_b = (fam, 10, "bold")
        self.font_s = (fam, 9)
        self.font_big = (fam, 15, "bold")

        self.q = queue.Queue()
        self.log_lines = []
        self.releases = load_json(CACHE_FILE, [])
        self.busy = False
        self.progress = None
        self.running = {}
        self.v_ws_cont = tk.StringVar()
        self.v_ws_items = tk.StringVar()
        self.v_ws_query = tk.StringVar()
        self.v_ws_sort = tk.StringVar(value="TRENDING")
        self.ws_ids = []
        self.ws_items = []
        self.ws_mode = "browse"
        self.ws_page = 1
        self.ws_loaded = False
        self.ws_previews = {}
        self._ws_tok = 0
        self._ws_img = None
        self.ws_type = "all"
        self.java_info = "..."
        self.page = "containers"
        self.containers = []
        self.v_tags = []
        self.c_names = []

        self.bind_all("<Control-KeyPress>", self._ctrl_keys)
        self.sessions = {}  # имя контейнера -> время последней записи наигранного
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.make_dirs()
        self.build_all()
        self.after(40, self._poll)
        self.after(30000, self._tick_play)
        self.log("SYSTEM ONLINE. ROOT = " + self.root)
        self.refresh_releases()
        self.detect_java()

    # ----------------------------------------------------------- буфер обмена
    # Tk привязывает Ctrl+C/V/X к буквам латиницы, поэтому с русской раскладкой
    # они не работают. Ловим их по коду клавиши.
    CTRL_CODES = ({86: "paste", 67: "copy", 88: "cut", 65: "all"} if IS_WIN
                  else {55: "paste", 54: "copy", 53: "cut", 38: "all"})

    def _ctrl_keys(self, e):
        if e.keysym.lower() in ("v", "c", "x"):
            return None  # латиница: стандартные привязки Tk справятся сами
        # Ctrl+A обрабатываем всегда: в Tk он по умолчанию не выделяет всё
        act = self.CTRL_CODES.get(e.keycode)
        w = e.widget
        if not act or not isinstance(w, (tk.Entry, tk.Text)):
            return None
        if act == "all":
            if isinstance(w, tk.Entry):
                w.select_range(0, "end")
                w.icursor("end")
            else:
                w.tag_add("sel", "1.0", "end")
        else:
            w.event_generate({"paste": "<<Paste>>", "copy": "<<Copy>>", "cut": "<<Cut>>"}[act])
        return "break"

    def _entry_menu(self, e):
        w, t = e.widget, self.t
        w.focus_set()
        menu = tk.Menu(self, tearoff=0, font=self.font, bg=t["panel"], fg=t["fg"],
                       activebackground=t["fg"], activeforeground=t["bg"], bd=0)
        for label, ev in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>")):
            menu.add_command(label=label, command=lambda ev=ev: w.event_generate(ev))
        menu.add_separator()
        menu.add_command(label="Select all", command=lambda: w.select_range(0, "end"))
        menu.tk_popup(e.x_root, e.y_root)

    # ----------------------------------------------------------- базовое
    @property
    def root(self):
        return self.S.get("install_dir")

    def make_dirs(self):
        try:
            os.makedirs(versions_dir(self.root), exist_ok=True)
            os.makedirs(containers_dir(self.root), exist_ok=True)
        except OSError as e:
            self.after(500, lambda: self.alert("ERROR", f"CANNOT CREATE FOLDERS IN\n{self.root}\n\n{e}"))

    def ui(self, fn, *a):
        self.q.put((fn, a))

    def _poll(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(40, self._poll)

    def log(self, msg, level="info"):
        line = f"{time.strftime('%H:%M:%S')} > {msg}"
        self.log_lines.append((line, level))
        if len(self.log_lines) > 800:
            del self.log_lines[:100]
        self._log_write(line, level)

    def _log_write(self, line, level):
        try:
            w = self.log_w
            w.config(state="normal")
            w.insert("end", line + "\n", level)
            w.see("end")
            w.config(state="disabled")
        except (AttributeError, tk.TclError):
            pass

    # ----------------------------------------------------------- фабрики виджетов
    def btn(self, parent, text, cmd, accent=False, color=None):
        return TermButton(parent, self, text, cmd, accent, color)

    def entry(self, parent, var, width=30, readonly=False):
        t = self.t
        e = tk.Entry(parent, textvariable=var, width=width, font=self.font, bg=t["panel"], fg=t["fg"],
                     insertbackground=t["fg"], relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=t["dim"], highlightcolor=t["fg"],
                     selectbackground=t["fg"], selectforeground=t["bg"],
                     readonlybackground=t["panel"], disabledforeground=t["dim"])
        if readonly:
            e.config(state="readonly")
        e.bind("<Button-3>", self._entry_menu)
        return e

    def label(self, parent, text, dim=False, bold=False, accent=False, **kw):
        t = self.t
        col = t["acc"] if accent else (t["dim"] if dim else t["fg"])
        return tk.Label(parent, text=text, font=self.font_b if bold else self.font,
                        bg=t["bg"], fg=col, **kw)

    def panel(self, parent, title):
        t = self.t
        outer = tk.Frame(parent, bg=t["bg"], highlightthickness=1, highlightbackground=t["fg"])
        head = tk.Frame(outer, bg=t["bg"])
        head.pack(fill="x")
        tk.Label(head, text=f" {title} ", font=self.font_b, bg=t["fg"], fg=t["bg"]).pack(side="left")
        tk.Frame(head, bg=t["dim"], height=1).pack(side="left", fill="x", expand=True, padx=(6, 0))
        body = tk.Frame(outer, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=8)
        return outer, body

    def listbox(self, parent):
        t = self.t
        wrap = tk.Frame(parent, bg=t["panel"], highlightthickness=1, highlightbackground=t["dim"])
        lb = tk.Listbox(wrap, font=self.font, bg=t["panel"], fg=t["fg"], selectbackground=t["fg"],
                        selectforeground=t["bg"], activestyle="none", bd=0, highlightthickness=0,
                        exportselection=False, relief="flat")
        sc = TermScroll(wrap, self, lb.yview)
        lb.config(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=2)
        return wrap, lb

    def optmenu(self, parent, var, values, command=None):
        t = self.t
        om = tk.OptionMenu(parent, var, *values, command=command)
        om.config(font=self.font, bg=t["panel"], fg=t["fg"], activebackground=t["fg"],
                  activeforeground=t["bg"], highlightthickness=1, highlightbackground=t["dim"],
                  bd=0, relief="flat", anchor="w")
        om["menu"].config(font=self.font, bg=t["panel"], fg=t["fg"], activebackground=t["fg"],
                          activeforeground=t["bg"], bd=0, relief="flat")
        return om

    # ----------------------------------------------------------- диалоги
    def make_dialog(self, title):
        d = tk.Toplevel(self)
        d.title(title)
        d.configure(bg=self.t["fg"], padx=1, pady=1)
        d.body = tk.Frame(d, bg=self.t["bg"], padx=18, pady=14)
        d.body.pack(fill="both", expand=True)
        d.transient(self)
        d.resizable(False, False)
        d.bind("<Escape>", lambda e: d.destroy())
        return d

    def show_dialog(self, d):
        d.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - d.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - d.winfo_reqheight()) // 3
        d.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            d.wait_visibility()
            d.grab_set()
        except tk.TclError:
            pass
        d.focus_set()
        self.wait_window(d)

    def ask(self, title, text, buttons):
        """buttons: [(надпись, значение, accent)] -> выбранное значение или None."""
        d = self.make_dialog(title)
        res = {"v": None}
        self.label(d.body, text, justify="left", wraplength=int(480 * self.sc), anchor="w").pack(anchor="w")
        row = tk.Frame(d.body, bg=self.t["bg"])
        row.pack(anchor="e", pady=(16, 0))
        for label, val, acc in buttons:
            def pick(v=val):
                res["v"] = v
                d.destroy()
            self.btn(row, label, pick, accent=acc).pack(side="left", padx=(8, 0))
        self.show_dialog(d)
        return res["v"]

    def alert(self, title, text):
        self.ask(title, text, [("OK", True, False)])

    def confirm(self, title, text, yes="YES", danger=False):
        return bool(self.ask(title, text, [("CANCEL", False, False), (yes, True, danger)]))

    # ----------------------------------------------------------- построение окна
    def build_all(self):
        for w in self.winfo_children():
            w.destroy()
        t = self.t
        self.configure(bg=t["bg"])

        head = tk.Frame(self, bg=t["bg"])
        head.pack(side="top", fill="x", padx=10, pady=(10, 0))
        tk.Label(head, text="MINDUSTRY // LAUNCHER", font=self.font_big, bg=t["bg"], fg=t["fg"]).pack(side="left")
        self.lbl_state = tk.Label(head, text="", font=self.font_b, bg=t["bg"], fg=t["acc"])
        self.lbl_state.pack(side="right")
        tk.Label(head, text=f"SYSTEM // ALPHA {APP_VER}", font=self.font_s, bg=t["bg"],
                 fg=t["dim"]).pack(side="right", padx=16)
        self.bar = ChevronBar(self, self)
        self.bar.pack(side="top", fill="x", padx=10, pady=8)

        foot, fbody = self.panel(self, "CONSOLE.LOG")
        foot.pack(side="bottom", fill="x", padx=10, pady=(8, 10))
        self.log_w = tk.Text(fbody, height=6, font=self.font_s, bg=t["bg"], fg=t["fg"], bd=0,
                             highlightthickness=0, wrap="word", state="disabled",
                             selectbackground=t["fg"], selectforeground=t["bg"], cursor="arrow")
        self.log_w.tag_config("info", foreground=t["fg"])
        self.log_w.tag_config("err", foreground=t["acc"])
        self.log_w.tag_config("game", foreground=t["dim"])
        self.log_w.pack(fill="x")
        self.log_w.bind("<Button-1>", lambda e: self.log_w.focus_set())  # чтобы работало Ctrl+C
        for line, lvl in self.log_lines[-300:]:
            self._log_write(line, lvl)

        mid = tk.Frame(self, bg=t["bg"])
        mid.pack(side="top", fill="both", expand=True, padx=10)
        left = tk.Frame(mid, bg=t["bg"], width=int(215 * self.sc))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.content = tk.Frame(mid, bg=t["bg"])
        self.content.pack(side="left", fill="both", expand=True, padx=(10, 0))

        nav_o, nav_b = self.panel(left, "SYS.INDEX")
        nav_o.pack(fill="x")
        self.nav = {}
        for key, label in NAV:
            l = tk.Label(nav_b, font=self.font_b, anchor="w", padx=6, pady=3, cursor="hand2")
            l.pack(fill="x")
            l.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            self.nav[key] = (l, label)
        info_o, info_b = self.panel(left, "STATUS")
        info_o.pack(fill="both", expand=True, pady=(10, 0))
        self.info_w = tk.Label(info_b, font=self.font_s, bg=t["bg"], fg=t["dim"], justify="left",
                               anchor="nw", wraplength=int(180 * self.sc))
        self.info_w.pack(fill="both", expand=True)

        self.show_page(self.page)
        self.update_state()

    def show_page(self, key):
        self.page = key
        for w in self.content.winfo_children():
            w.destroy()
        getattr(self, "page_" + key)()
        t = self.t
        for k, (l, label) in self.nav.items():
            if k == key:
                l.config(text=f"► {label}", bg=t["fg"], fg=t["bg"])
            else:
                l.config(text=f"  {label}", bg=t["bg"], fg=t["fg"])
        self.refresh_info()

    def update_state(self):
        n = len(self.running)
        if self.busy:
            pct = f" {int(self.progress * 100)}%" if self.progress is not None else ""
            s = f"[BUSY{pct}]"
        elif n:
            s = f"[RUNNING:{n}]"
        else:
            s = "[READY]"
        try:
            self.lbl_state.config(text=s)
        except tk.TclError:
            pass

    def refresh_info(self):
        root = self.root
        short = root if len(root) <= 22 else "..." + root[-19:]
        inst = len(installed_tags(root, self.releases))
        txt = (f"JAVA\n{self.java_info}\n\nROOT\n{short}\n\n"
               f"CONTAINERS  {len(load_containers(root))}\nINSTALLED   {inst}\nRUNNING     {len(self.running)}"
               f"\n\nPLAYED TOTAL\n{fmt_dur(self.play_total())}")
        try:
            self.info_w.config(text=txt)
        except tk.TclError:
            pass

    # ----------------------------------------------------------- страница: контейнеры
    @staticmethod
    def c_row(a, b, c, p, d):
        return f"  {a:<15.15} {b:<10.10} {c:<11.11} {p:<8.8} {d}"

    def page_containers(self):
        t = self.t
        outer, body = self.panel(self.content, "CONTAINERS.RSU")
        outer.pack(fill="both", expand=True)
        tk.Label(body, text=self.c_row("NAME", "VERSION", "RAM", "PLAYED", "STATE"), font=self.font,
                 bg=t["bg"], fg=t["dim"], anchor="w").pack(fill="x")
        mid = tk.Frame(body, bg=t["bg"])
        mid.pack(fill="both", expand=True, pady=(2, 8))
        card = tk.Frame(mid, bg=t["bg"], width=int(230 * self.sc), highlightthickness=1,
                        highlightbackground=t["dim"])
        card.pack(side="right", fill="y", padx=(10, 0))
        card.pack_propagate(False)
        self.ic_size = int(96 * self.sc)
        ic_box = tk.Frame(card, bg=t["panel"], width=self.ic_size, height=self.ic_size,
                          highlightthickness=1, highlightbackground=t["fg"])
        ic_box.pack(pady=(14, 8))
        ic_box.pack_propagate(False)
        self.ic_lbl = tk.Label(ic_box, bg=t["panel"], fg=t["fg"], bd=0, font=(self.font[0], 28, "bold"))
        self.ic_lbl.pack(expand=True)
        self.card_txt = tk.Label(card, bg=t["bg"], fg=t["fg"], font=self.font_s, justify="left",
                                 anchor="nw", wraplength=int(205 * self.sc))
        self.card_txt.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._icon_img = None
        wrap, self.c_lb = self.listbox(mid)
        wrap.pack(side="left", fill="both", expand=True)
        self.c_lb.bind("<Double-Button-1>", lambda e: self.c_run())
        self.c_lb.bind("<<ListboxSelect>>", lambda e: self.c_buttons())
        row = tk.Frame(body, bg=t["bg"])
        row.pack(fill="x")
        self.cb_run = self.btn(row, "► RUN", self.c_run, accent=True)
        self.cb_new = self.btn(row, "+ NEW", self.c_new)
        self.cb_edit = self.btn(row, "EDIT", self.c_edit)
        self.cb_clone = self.btn(row, "CLONE", self.c_clone)
        self.cb_open = self.btn(row, "OPEN DIR", self.c_open)
        self.cb_del = self.btn(row, "DELETE", self.c_delete)
        for b in (self.cb_run, self.cb_new, self.cb_edit, self.cb_clone, self.cb_open, self.cb_del):
            b.pack(side="left", padx=(0, 8))
        self.refresh_containers()

    def c_state(self, c):
        if c["name"] in self.running:
            return "RUNNING", self.t["acc"]
        if not is_installed(self.root, c["version"]):
            return "NO VERSION", self.t["dim"]
        return "READY", self.t["fg"]

    def refresh_containers(self, select=None):
        if self.page != "containers":
            return
        lb = self.c_lb
        select = select or self.c_selected()
        self.containers = load_containers(self.root)
        self.c_names = [c["name"] for c in self.containers]
        lb.delete(0, "end")
        if not self.containers:
            lb.insert("end", "  NO CONTAINERS. PRESS [+ NEW].")
            lb.itemconfig(0, foreground=self.t["dim"])
        for i, c in enumerate(self.containers):
            mn = parse_ram(c["ram_min"] or self.S.get("ram_min")) or 256
            mx = parse_ram(c["ram_max"] or self.S.get("ram_max")) or 2048
            st, col = self.c_state(c)
            lb.insert("end", self.c_row(c["name"], c["version"], f"{fmt_ram(mn)}-{fmt_ram(mx)}",
                                        fmt_dur(self.play_secs(c["name"])), st))
            lb.itemconfig(i, foreground=col)
        if self.c_names:
            idx = self.c_names.index(select) if select in self.c_names else 0
            lb.selection_set(idx)
            lb.see(idx)
        self.c_buttons()
        self.refresh_info()

    def c_selected(self):
        try:
            s = self.c_lb.curselection()
            return self.c_names[s[0]] if s and s[0] < len(self.c_names) else None
        except (AttributeError, tk.TclError):
            return None

    def c_get(self, name=None):
        name = name or self.c_selected()
        return next((c for c in self.containers if c["name"] == name), None)

    def c_buttons(self):
        c = self.c_get()
        run = bool(c) and c["name"] in self.running
        self.cb_run.set_text("STOP" if run else "► RUN")
        self.cb_run.enable(bool(c))
        self.cb_edit.enable(bool(c) and not run)
        self.cb_clone.enable(bool(c) and not run and not self.busy)
        self.cb_open.enable(bool(c))
        self.cb_del.enable(bool(c) and not run)
        self.update_card(c)

    def update_card(self, c):
        """Правая карточка: иконка и параметры выбранного контейнера."""
        img = None
        if c and c.get("icon"):
            try:
                im = tk.PhotoImage(file=os.path.join(container_dir(self.root, c["name"]), c["icon"]))
                big = max(im.width(), im.height(), 1)
                box = self.ic_size - 4
                if big > box:
                    im = im.subsample(-(-big // box))
                elif box // big >= 2:
                    im = im.zoom(box // big)
                img = im
            except tk.TclError:
                img = None
        self._icon_img = img
        if not c:
            self.ic_lbl.config(image="", text="--")
            self.card_txt.config(text="")
            return
        words = [w for w in re.split(r"[\s_\-.]+", c["name"]) if w]
        initials = ("".join(w[0] for w in words)[:2] or c["name"][:2]).upper()
        self.ic_lbl.config(image=img if img else "", text="" if img else initials)
        mn = parse_ram(c["ram_min"] or self.S.get("ram_min")) or 256
        mx = parse_ram(c["ram_max"] or self.S.get("ram_max")) or 2048
        lines = [c["name"], "", f"VERSION  {c['version']}", f"RAM      {fmt_ram(mn)}-{fmt_ram(mx)}",
                 f"PLAYED   {fmt_dur(self.play_secs(c['name']))}",
                 f"LAUNCHES {container_stats(self.root, c['name'])[1]}"]
        if c["jvm_args"]:
            lines.append(f"JVM      {c['jvm_args']}")
        if c["game_args"]:
            lines.append(f"ARGS     {c['game_args']}")
        self.card_txt.config(text="\n".join(lines))

    def name_error(self, name, exclude=None):
        if not name or re.search(r'[\\/:*?"<>|]', name) or name.endswith("."):
            return "! INVALID NAME"
        if any(c["name"].lower() == name.lower() and c["name"] != exclude
               for c in load_containers(self.root)):
            return "! NAME ALREADY EXISTS"
        return None

    def save_with_icon(self, r):
        """Сохраняет container.ini и копирует выбранную иконку в папку контейнера."""
        src = r.pop("icon_src", None)
        d = container_dir(self.root, r["name"])
        os.makedirs(d, exist_ok=True)
        if src:
            fn = "icon" + (os.path.splitext(src)[1].lower() or ".png")
            shutil.copyfile(src, os.path.join(d, fn))
            r["icon"] = fn
        for f in os.listdir(d):
            if f.startswith("icon.") and f != r.get("icon"):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        save_container(self.root, r)

    def c_clone(self):
        c = self.c_get()
        if not c or self.busy:
            return
        if c["name"] in self.running:
            self.alert("RUNNING", "STOP THE CONTAINER BEFORE CLONING IT.")
            return
        r = self.dlg_clone(c)
        if not r:
            return
        name, with_data = r
        src, dst = container_dir(self.root, c["name"]), container_dir(self.root, name)
        self.busy = True
        self.update_state()
        self.c_buttons()
        self.log(f"CLONING '{c['name']}' -> '{name}' ...")

        def work():
            try:
                if with_data:
                    shutil.copytree(src, dst)
                else:
                    os.makedirs(os.path.join(dst, "data"))
                    for f in os.listdir(src):
                        p = os.path.join(src, f)
                        if os.path.isfile(p):
                            shutil.copy2(p, dst)
                try:  # у клона своя статистика с нуля
                    os.remove(os.path.join(dst, "stats.json"))
                except OSError:
                    pass
                self.ui(self._clone_done, name, None)
            except Exception as e:
                shutil.rmtree(dst, ignore_errors=True)
                self.ui(self._clone_done, name, e)

        threading.Thread(target=work, daemon=True).start()

    def _clone_done(self, name, err):
        self.busy = False
        self.update_state()
        if err:
            self.log(f"CLONE FAILED: {err}", "err")
        else:
            self.log(f"CONTAINER '{name}' CREATED (CLONE)")
        self.refresh_containers(None if err else name)

    def dlg_clone(self, c):
        t = self.t
        d = self.make_dialog("CLONE CONTAINER")
        b = d.body
        v_name = tk.StringVar(value=c["name"] + "-copy")
        v_data = {"v": True}
        self.label(b, f"CLONE '{c['name']}'", bold=True, anchor="w").pack(fill="x", pady=(0, 10))
        g = tk.Frame(b, bg=t["bg"])
        g.pack(fill="x")
        self.label(g, "new name", dim=True).grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.entry(g, v_name, 34).grid(row=0, column=1, sticky="ew", ipady=2)
        TermToggle(b, self, "COPY SAVES AND MODS (data folder)", True,
                   lambda v: v_data.__setitem__("v", v)).pack(anchor="w", pady=(10, 0))
        err = self.label(b, "", accent=True, anchor="w")
        err.pack(fill="x", pady=(8, 0))
        res = {"v": None}

        def ok(*_):
            name = v_name.get().strip()
            e = self.name_error(name)
            if e:
                err.config(text=e)
                return
            res["v"] = (name, v_data["v"])
            d.destroy()

        row_b = tk.Frame(b, bg=t["bg"])
        row_b.pack(anchor="e", pady=(12, 0))
        self.btn(row_b, "CANCEL", d.destroy).pack(side="left", padx=(0, 8))
        self.btn(row_b, "CLONE", ok, accent=True).pack(side="left")
        d.bind("<Return>", ok)
        self.show_dialog(d)
        return res["v"]

    def c_run(self):
        c = self.c_get()
        if not c:
            return
        if c["name"] in self.running:
            try:
                self.running[c["name"]].terminate()
                self.log(f"[{c['name']}] TERMINATE SENT")
            except Exception as e:
                self.log(f"STOP FAILED: {e}", "err")
            return
        self.launch(c)

    def c_open(self):
        c = self.c_get()
        if c:
            try:
                open_path(container_dir(self.root, c["name"]))
            except Exception as e:
                self.log(f"OPEN FAILED: {e}", "err")

    def c_new(self):
        if not installed_tags(self.root, self.releases):
            self.alert("NO VERSIONS", "INSTALL AT LEAST ONE VERSION FIRST.")
            self.show_page("versions")
            return
        r = self.dlg_container(None)
        if r:
            try:
                self.save_with_icon(r)
                self.log(f"CONTAINER '{r['name']}' CREATED")
            except OSError as e:
                self.log(f"CREATE FAILED: {e}", "err")
            self.refresh_containers(r["name"])

    def c_edit(self):
        c = self.c_get()
        if not c or c["name"] in self.running:
            return
        r = self.dlg_container(c)
        if not r:
            return
        try:
            if r["name"] != c["name"]:
                os.rename(container_dir(self.root, c["name"]), container_dir(self.root, r["name"]))
            self.save_with_icon(r)
            self.log(f"CONTAINER '{r['name']}' SAVED")
        except OSError as e:
            self.log(f"SAVE FAILED: {e}", "err")
        self.refresh_containers(r["name"])

    def c_delete(self):
        c = self.c_get()
        if not c or c["name"] in self.running:
            return
        if self.confirm("DELETE CONTAINER",
                        f"DELETE '{c['name']}'?\n\nALL ITS SAVES, MODS AND SETTINGS WILL BE LOST.",
                        "DELETE", danger=True):
            shutil.rmtree(container_dir(self.root, c["name"]), ignore_errors=True)
            self.log(f"CONTAINER '{c['name']}' DELETED")
            self.refresh_containers()

    def free_name(self):
        used = {c["name"].lower() for c in load_containers(self.root)}
        i = 1
        while f"container{i}" in used:
            i += 1
        return f"container{i}"

    def dlg_container(self, old):
        t = self.t
        tags = installed_tags(self.root, self.releases)
        if old and old["version"] not in tags:
            tags.insert(0, old["version"])
        d = self.make_dialog("EDIT CONTAINER" if old else "NEW CONTAINER")
        b = d.body
        v_name = tk.StringVar(value=old["name"] if old else self.free_name())
        v_ver = tk.StringVar(value=old["version"] if old else tags[0])
        v_min = tk.StringVar(value=old["ram_min"] if old else "")
        v_max = tk.StringVar(value=old["ram_max"] if old else "")
        v_jvm = tk.StringVar(value=old["jvm_args"] if old else "")
        v_game = tk.StringVar(value=old["game_args"] if old else "")
        err = self.label(b, "", accent=True, anchor="w")
        icon = {"src": None, "keep": (old or {}).get("icon", "")}
        v_icon = tk.StringVar(value=icon["keep"] or "(none)")

        def pick_icon():
            p = filedialog.askopenfilename(parent=d, title="ICON (PNG / GIF)",
                                           filetypes=[("images", "*.png *.gif *.ppm *.pgm"), ("all", "*.*")])
            if not p:
                return
            try:
                tk.PhotoImage(file=p)
            except tk.TclError:
                err.config(text="! UNSUPPORTED IMAGE (USE PNG OR GIF)")
                return
            icon["src"] = p
            v_icon.set(os.path.basename(p))
            err.config(text="")

        def clear_icon():
            icon["src"], icon["keep"] = None, ""
            v_icon.set("(none)")

        g = tk.Frame(b, bg=t["bg"])
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)

        def row(i, label, widget, hint=""):
            self.label(g, label, dim=True, anchor="w").grid(row=i, column=0, sticky="w", padx=(0, 14), pady=4)
            widget.grid(row=i, column=1, sticky="ew", pady=4, ipady=2)
            if hint:
                self.label(g, hint, dim=True).grid(row=i, column=2, sticky="w", padx=(8, 0))

        row(0, "name", self.entry(g, v_name, 34))
        om = tk.OptionMenu(g, v_ver, *tags)
        om.config(font=self.font, bg=t["panel"], fg=t["fg"], activebackground=t["fg"],
                  activeforeground=t["bg"], highlightthickness=1, highlightbackground=t["dim"],
                  bd=0, relief="flat", anchor="w")
        om["menu"].config(font=self.font, bg=t["panel"], fg=t["fg"], activebackground=t["fg"],
                          activeforeground=t["bg"], bd=0, relief="flat")
        row(1, "version", om)
        row(2, "ram_min", self.entry(g, v_min, 12), f"empty = global ({self.S.get('ram_min')})")
        row(3, "ram_max", self.entry(g, v_max, 12), f"empty = global ({self.S.get('ram_max')})")
        row(4, "jvm_args", self.entry(g, v_jvm, 34), "added after global")
        row(5, "game_args", self.entry(g, v_game, 34), "added after global")
        fi = tk.Frame(g, bg=t["bg"])
        self.entry(fi, v_icon, 20, readonly=True).pack(side="left", fill="x", expand=True, ipady=2)
        self.btn(fi, "BROWSE", pick_icon).pack(side="left", padx=(6, 0))
        self.btn(fi, "CLEAR", clear_icon).pack(side="left", padx=(6, 0))
        row(6, "icon", fi, "png / gif")
        err.pack(fill="x", pady=(8, 0))
        res = {"v": None}

        def ok(*_):
            name = v_name.get().strip()
            e = self.name_error(name, old["name"] if old else None)
            if e:
                err.config(text=e)
                return
            mn, mx = v_min.get().strip(), v_max.get().strip()
            pmn = parse_ram(mn) if mn else None
            pmx = parse_ram(mx) if mx else None
            if (mn and (pmn is None or pmn < 64)) or (mx and (pmx is None or pmx < 64)):
                err.config(text="! RAM: USE 512M OR 2G (MIN 64M)")
                return
            emn = pmn or parse_ram(self.S.get("ram_min")) or 256
            emx = pmx or parse_ram(self.S.get("ram_max")) or 2048
            if emn > emx:
                err.config(text="! RAM_MIN IS GREATER THAN RAM_MAX")
                return
            res["v"] = dict(name=name, version=v_ver.get(), ram_min=mn.upper(), ram_max=mx.upper(),
                            jvm_args=v_jvm.get().strip(), game_args=v_game.get().strip(),
                            icon=icon["keep"], icon_src=icon["src"])
            d.destroy()

        row_b = tk.Frame(b, bg=t["bg"])
        row_b.pack(anchor="e", pady=(12, 0))
        self.btn(row_b, "CANCEL", d.destroy).pack(side="left", padx=(0, 8))
        self.btn(row_b, "SAVE", ok, accent=True).pack(side="left")
        d.bind("<Return>", ok)
        self.show_dialog(d)
        return res["v"]

    # ----------------------------------------------------------- время игры
    def play_secs(self, name):
        saved, _ = container_stats(self.root, name)
        last = self.sessions.get(name)
        return saved + (time.time() - last if last else 0)

    def play_total(self):
        total = float(load_json(GLOBAL_STATS, {}).get("seconds", 0) or 0)
        return total + sum(time.time() - last for last in self.sessions.values())

    def _tick_play(self):
        """Раз в 30 с сохраняем наигранное, чтобы время не терялось при закрытии лаунчера."""
        now = time.time()
        for name, last in list(self.sessions.items()):
            add_playtime(self.root, name, now - last)
            self.sessions[name] = now
        if self.sessions:
            self.refresh_info()
            if self.page == "containers":
                self.refresh_containers()
        self.after(30000, self._tick_play)

    def _on_close(self):
        now = time.time()
        for name, last in list(self.sessions.items()):
            add_playtime(self.root, name, now - last)
        self.destroy()

    # ----------------------------------------------------------- запуск игры
    def launch(self, c):
        name = c["name"]
        root = self.root
        if not is_installed(root, c["version"]):
            self.alert("NO VERSION", f"VERSION {c['version']} IS NOT INSTALLED.\n"
                                     "INSTALL IT IN VERSIONS.DAT OR EDIT THE CONTAINER.")
            return
        java = find_java(self.S.get("java_path"))
        if not java:
            self.alert("JAVA NOT FOUND", "MINDUSTRY NEEDS JAVA 17 OR NEWER.\n"
                                         "INSTALL IT (E.G. ECLIPSE TEMURIN, adoptium.net)\n"
                                         "OR SET java_path IN SETTINGS.INI.")
            return
        mn = parse_ram(c["ram_min"] or self.S.get("ram_min")) or 256
        mx = parse_ram(c["ram_max"] or self.S.get("ram_max")) or 2048
        if mn > mx:
            mn = mx
        data = os.path.join(container_dir(root, name), "data")
        os.makedirs(data, exist_ok=True)
        cmd = [java, f"-Xms{mn}m", f"-Xmx{mx}m"]
        cmd += split_args(self.S.get("jvm_args")) + split_args(c["jvm_args"])
        cmd += ["-jar", jar_path(root, c["version"])]
        cmd += split_args(self.S.get("game_args")) + split_args(c["game_args"])
        env = os.environ.copy()
        env["MINDUSTRY_DATA_DIR"] = data
        kw = dict(cwd=data, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT)
        if IS_WIN:
            kw["creationflags"] = NOWIN | 0x200
        else:
            kw["start_new_session"] = True
        try:
            p = subprocess.Popen(cmd, **kw)
        except Exception as e:
            self.log(f"LAUNCH FAILED: {e}", "err")
            return
        self.running[name] = p
        self.sessions[name] = time.time()
        add_playtime(root, name, 0, launches=1)
        self.log(f"[{name}] STARTED  {c['version']}  Xms{mn}m Xmx{mx}m")
        threading.Thread(target=self._watch, args=(name, p), daemon=True).start()
        self.update_state()
        self.refresh_containers(name)

    def _watch(self, name, p):
        try:
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.ui(self.log, f"[{name}] {line}", "game")
        except Exception:
            pass
        code = p.wait()
        self.ui(self._exited, name, code)

    def _exited(self, name, code):
        last = self.sessions.pop(name, None)
        if last:
            add_playtime(self.root, name, time.time() - last)
        self.running.pop(name, None)
        self.log(f"[{name}] EXITED (code {code})", "err" if code not in (0, None) and code > 0 else "info")
        self.update_state()
        self.refresh_containers()

    # ----------------------------------------------------------- страница: версии
    @staticmethod
    def v_row(mark, tag, date, size, state):
        return f"  {mark} {tag:<16.16} {date:<11} {size:<9} {state}"

    def page_versions(self):
        t = self.t
        outer, body = self.panel(self.content, "VERSIONS.DAT")
        outer.pack(fill="both", expand=True)
        top = tk.Frame(body, bg=t["bg"])
        top.pack(fill="x", pady=(0, 6))
        TermToggle(top, self, "SHOW PRE-RELEASES", self.S.get("show_pre") == "1", self.v_toggle_pre).pack(side="left")
        self.btn(top, "REFRESH", self.refresh_releases).pack(side="right")
        tk.Label(body, text=self.v_row("   ", "TAG", "DATE", "SIZE", "STATE"), font=self.font,
                 bg=t["bg"], fg=t["dim"], anchor="w").pack(fill="x")
        wrap, self.v_lb = self.listbox(body)
        wrap.pack(fill="both", expand=True, pady=(2, 8))
        self.v_lb.bind("<<ListboxSelect>>", lambda e: self.v_buttons())
        self.v_lb.bind("<Double-Button-1>", lambda e: self.v_install())
        row = tk.Frame(body, bg=t["bg"])
        row.pack(fill="x")
        self.vb_inst = self.btn(row, "INSTALL", self.v_install, accent=True)
        self.vb_del = self.btn(row, "DELETE", self.v_delete)
        self.vb_open = self.btn(row, "OPEN DIR", self.v_open)
        for b in (self.vb_inst, self.vb_del, self.vb_open):
            b.pack(side="left", padx=(0, 8))
        self.refresh_versions()

    def v_toggle_pre(self, val):
        self.S.set("show_pre", "1" if val else "0")
        self.S.save()
        self.refresh_versions()

    def refresh_versions(self, select=None):
        if self.page != "versions":
            return
        lb = self.v_lb
        select = select or self.v_selected()
        lb.delete(0, "end")
        self.v_tags = []
        show_pre = self.S.get("show_pre") == "1"
        for r in self.releases:
            inst = is_installed(self.root, r["tag"])
            if r["prerelease"] and not show_pre and not inst:
                continue
            name = r["tag"] + ("*" if r["prerelease"] else "")
            lb.insert("end", self.v_row("[x]" if inst else "[ ]", name, r["date"],
                                        fmt_size(r["size"]), "INSTALLED" if inst else "-"))
            lb.itemconfig(lb.size() - 1, foreground=self.t["fg"] if inst else self.t["dim"])
            self.v_tags.append(r["tag"])
        if not self.v_tags:
            lb.insert("end", "  NO DATA. PRESS [REFRESH].")
            lb.itemconfig(0, foreground=self.t["dim"])
        else:
            idx = self.v_tags.index(select) if select in self.v_tags else 0
            lb.selection_set(idx)
            lb.see(idx)
        self.v_buttons()
        self.refresh_info()

    def v_selected(self):
        try:
            s = self.v_lb.curselection()
            return self.v_tags[s[0]] if s and s[0] < len(self.v_tags) else None
        except (AttributeError, tk.TclError):
            return None

    def v_buttons(self):
        tag = self.v_selected()
        inst = bool(tag) and is_installed(self.root, tag)
        self.vb_inst.enable(bool(tag) and not inst and not self.busy)
        self.vb_del.enable(inst and not self.busy)
        self.vb_open.enable(True)

    def v_open(self):
        tag = self.v_selected()
        try:
            if tag and is_installed(self.root, tag):
                open_path(version_dir(self.root, tag))
            else:
                open_path(versions_dir(self.root))
        except Exception as e:
            self.log(f"OPEN FAILED: {e}", "err")

    def v_delete(self):
        tag = self.v_selected()
        if not tag or not is_installed(self.root, tag):
            return
        users = [c["name"] for c in load_containers(self.root) if c["version"] == tag]
        if any(n in self.running for n in users):
            self.alert("IN USE", "A RUNNING CONTAINER USES THIS VERSION.\nSTOP IT FIRST.")
            return
        text = f"DELETE VERSION {tag}?"
        if users:
            text += "\n\nUSED BY: " + ", ".join(users) + "\n(THEIR SAVES ARE NOT TOUCHED)"
        if self.confirm("DELETE VERSION", text, "DELETE", danger=True):
            shutil.rmtree(version_dir(self.root, tag), ignore_errors=True)
            self.log(f"VERSION {tag} DELETED")
            self.refresh_versions()

    def refresh_releases(self):
        self.log("FETCHING RELEASE LIST ...")

        def work():
            try:
                rels = fetch_releases()
                if not rels:
                    raise RuntimeError("EMPTY LIST")
                save_json(CACHE_FILE, rels)
                self.ui(self._set_rels, rels)
            except Exception as e:
                self.ui(self.log, f"RELEASE LIST FAILED: {e}", "err")

        threading.Thread(target=work, daemon=True).start()

    def _set_rels(self, rels):
        self.releases = rels
        self.log(f"RELEASE LIST OK: {len(rels)} VERSIONS")
        self.refresh_versions()

    def set_progress(self, p):
        self.progress = p
        self.update_state()

    def v_install(self):
        tag = self.v_selected()
        rel = next((r for r in self.releases if r["tag"] == tag), None)
        if not rel or self.busy or is_installed(self.root, tag):
            return
        self.busy = True
        self.progress = 0.0
        self.update_state()
        self.v_buttons()
        root = self.root
        self.log(f"DOWNLOADING {tag} ({fmt_size(rel['size'])}) ...")

        def work():
            os.makedirs(version_dir(root, tag), exist_ok=True)
            dest = jar_path(root, tag)
            part = dest + ".part"
            try:
                req = urllib.request.Request(rel["url"], headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
                    total = int(r.headers.get("Content-Length") or rel["size"] or 0)
                    done, last = 0, 0.0
                    while True:
                        chunk = r.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if total and now - last > 0.1:
                            last = now
                            self.ui(self.set_progress, done / total)
                if total and os.path.getsize(part) != total:
                    raise IOError("INCOMPLETE DOWNLOAD")
                os.replace(part, dest)
                self.ui(self._dl_done, tag, None)
            except Exception as e:
                try:
                    os.remove(part)
                except OSError:
                    pass
                self.ui(self._dl_done, tag, e)

        threading.Thread(target=work, daemon=True).start()

    def _dl_done(self, tag, err):
        self.busy = False
        self.progress = None
        self.update_state()
        if err:
            self.log(f"DOWNLOAD FAILED: {err}", "err")
        else:
            self.log(f"VERSION {tag} INSTALLED")
            if not load_containers(self.root):
                try:
                    save_container(self.root, dict(name="default", version=tag, ram_min="", ram_max="",
                                                   jvm_args="", game_args=""))
                    self.log("CONTAINER 'default' CREATED")
                except OSError:
                    pass
        self.refresh_versions()

    # ----------------------------------------------------------- страница: мастерская Steam
    def ws_dir(self):
        return steam_dir(self.root)

    def ws_manifest(self, cname):
        return os.path.join(container_dir(self.root, cname), "workshop.json")

    def page_workshop(self):
        t = self.t
        outer, body = self.panel(self.content, "WORKSHOP.SYS")
        outer.pack(fill="both", expand=True)
        names = [c["name"] for c in load_containers(self.root)]
        if not names:
            self.label(body, "NO CONTAINERS. CREATE ONE IN CONTAINERS.RSU FIRST.", dim=True).pack(anchor="w")
            return
        if self.v_ws_cont.get() not in names:
            self.v_ws_cont.set(names[0])
        top = tk.Frame(body, bg=t["bg"])
        top.pack(fill="x")
        self.label(top, "container", dim=True).pack(side="left", padx=(0, 10))
        self.optmenu(top, self.v_ws_cont, names, command=lambda v: self.ws_update()).pack(side="left")
        self.btn(top, "IMPORT FILES", self.ws_import_files).pack(side="left", padx=(14, 0))
        self.btn(top, "IMPORT DIR", self.ws_import_folder).pack(side="left", padx=(6, 0))
        self.ws_tabs = {}
        for key, text in (("installed", "INSTALLED"), ("browse", "BROWSE")):
            b = self.btn(top, text, lambda k=key: self.ws_set_mode(k))
            b.pack(side="right", padx=(6, 0))
            self.ws_tabs[key] = b
        self.ws_msg = self.label(body, "", dim=True, anchor="w")
        self.ws_msg.pack(fill="x", pady=(6, 0))
        self.ws_area = tk.Frame(body, bg=t["bg"])
        self.ws_area.pack(fill="both", expand=True)
        self.ws_set_mode(self.ws_mode)

    def ws_import_files(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="IMPORT MAPS / SCHEMATICS / MODS",
            filetypes=[("Mindustry content", "*.msav *.msch *.zip *.jar"), ("all", "*.*")])
        if paths:
            self._ws_import(list(paths))

    def ws_import_folder(self):
        p = filedialog.askdirectory(parent=self, title="IMPORT UNPACKED MOD FOLDER")
        if p:
            self._ws_import([p])

    def _ws_import(self, paths):
        cname = self.v_ws_cont.get()
        data = os.path.join(container_dir(self.root, cname), "data")
        mpath = self.ws_manifest(cname)
        man = load_json(mpath, {})
        ok = 0
        for p in paths:
            try:
                title, kind, files = import_local_item(p, data)
                wid, n = f"L{int(time.time())}", 0
                while f"{wid}{n}" in man:
                    n += 1
                man[f"{wid}{n}"] = {"title": title, "kind": kind, "files": files}
                self.log(f"IMPORTED {kind.upper()} '{title}' -> '{cname}': {', '.join(files)}")
                ok += 1
            except Exception as e:
                bn = os.path.basename(p.rstrip("/\\"))
                self.log(f"IMPORT FAILED '{bn}': {e}", "err")
        if ok:
            save_json(mpath, man)
        self.ws_say(f"IMPORTED {ok}/{len(paths)}" + ("" if ok == len(paths) else " (SEE CONSOLE)"),
                    ok != len(paths))
        self.ws_update()

    def ws_set_mode(self, mode):
        self.ws_mode = mode
        self.ws_say("")
        for k, b in self.ws_tabs.items():
            b.activate(k == mode)
        for w in self.ws_area.winfo_children():
            w.destroy()
        (self.ws_build_browse if mode == "browse" else self.ws_build_installed)()

    def ws_say(self, text, err=False):
        try:
            self.ws_msg.config(text=text, fg=self.t["acc"] if err else self.t["dim"])
        except (AttributeError, tk.TclError):
            pass

    def ws_update(self):
        if self.page != "workshop":
            return
        if self.ws_mode == "browse":
            self.ws_fill()
        else:
            self.ws_refresh()

    # ---- режим INSTALLED: что уже скачано в контейнер
    def ws_build_installed(self):
        t, a = self.t, self.ws_area
        tk.Label(a, text=f"  {'ID':<12} {'TYPE':<10} TITLE", font=self.font, bg=t["bg"],
                 fg=t["dim"], anchor="w").pack(fill="x", pady=(8, 0))
        wrap, self.ws_lb = self.listbox(a)
        wrap.pack(fill="both", expand=True, pady=(2, 8))
        row = tk.Frame(a, bg=t["bg"])
        row.pack(fill="x")
        self.btn(row, "REMOVE", self.ws_remove).pack(side="left", padx=(0, 8))
        self.btn(row, "OPEN DIR", lambda: self.safe_open(
            os.path.join(container_dir(self.root, self.v_ws_cont.get()), "data"))).pack(side="left")
        self.ws_refresh()

    def ws_refresh(self):
        if self.page != "workshop":
            return
        try:
            lb = self.ws_lb
            lb.delete(0, "end")
        except (AttributeError, tk.TclError):
            return
        self.ws_ids = []
        man = load_json(self.ws_manifest(self.v_ws_cont.get()), {})
        for wid, it in man.items():
            lb.insert("end", f"  {wid:<12} {str(it.get('kind', '?')):<10.10} {it.get('title', '')}")
            self.ws_ids.append(wid)
        if not self.ws_ids:
            lb.insert("end", "  NOTHING DOWNLOADED YET.")
            lb.itemconfig(0, foreground=self.t["dim"])

    def ws_selected(self):
        try:
            s = self.ws_lb.curselection()
            return self.ws_ids[s[0]] if s and s[0] < len(self.ws_ids) else None
        except (AttributeError, tk.TclError):
            return None

    # ---- режим BROWSE: каталог мастерской с превью
    def ws_build_browse(self):
        t, a = self.t, self.ws_area
        bar = tk.Frame(a, bg=t["bg"])
        bar.pack(fill="x", pady=(8, 6))
        e = self.entry(bar, self.v_ws_query, 28)
        e.pack(side="left", ipady=2)
        e.bind("<Return>", lambda ev: self.ws_search(1))
        self.optmenu(bar, self.v_ws_sort, list(BROWSE_SORTS), command=lambda v: self.ws_search(1)).pack(
            side="left", padx=(8, 0))
        self.btn(bar, "SEARCH", lambda: self.ws_search(1), accent=True).pack(side="left", padx=(8, 0))

        paste = tk.Frame(a, bg=t["bg"])
        paste.pack(side="bottom", fill="x", pady=(8, 0))
        self.label(paste, "link / id", dim=True).pack(side="left", padx=(0, 10))
        e2 = self.entry(paste, self.v_ws_items, 40)
        e2.pack(side="left", fill="x", expand=True, ipady=2)
        e2.bind("<Return>", lambda ev: self.ws_download())
        self.btn(paste, "DOWNLOAD", self.ws_download).pack(side="left", padx=(8, 0))
        pager = tk.Frame(a, bg=t["bg"])
        pager.pack(side="bottom", fill="x", pady=(6, 0))
        self.btn(pager, "< PREV", lambda: self.ws_search(max(1, self.ws_page - 1))).pack(side="left")
        self.ws_pg = self.label(pager, f"PAGE {self.ws_page}", dim=True)
        self.ws_pg.pack(side="left", padx=10)
        self.btn(pager, "NEXT >", lambda: self.ws_search(self.ws_page + 1)).pack(side="left")

        mid = tk.Frame(a, bg=t["bg"])
        mid.pack(fill="both", expand=True)
        card = tk.Frame(mid, bg=t["bg"], width=int(290 * self.sc), highlightthickness=1,
                        highlightbackground=t["dim"])
        card.pack(side="right", fill="y", padx=(10, 0))
        card.pack_propagate(False)
        brow = tk.Frame(card, bg=t["bg"])  # кнопки пакуем первыми, чтобы им всегда хватало места
        brow.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.ws_bdl = self.btn(brow, "DOWNLOAD", self.ws_dl_selected, accent=True)
        self.ws_bdl.pack(side="left")
        self.ws_bst = self.btn(brow, "STEAM PAGE", self.ws_open_steam)
        self.ws_bst.pack(side="left", padx=(8, 0))
        self.ws_psize = int(150 * self.sc)
        box = tk.Frame(card, bg=t["panel"], width=self.ws_psize, height=self.ws_psize,
                       highlightthickness=1, highlightbackground=t["fg"])
        box.pack(pady=(10, 6))
        box.pack_propagate(False)
        self.ws_prev = tk.Label(box, bg=t["panel"], fg=t["dim"], bd=0, font=self.font_s, text="")
        self.ws_prev.pack(expand=True)
        self.ws_title = tk.Label(card, bg=t["bg"], fg=t["fg"], font=self.font_b, anchor="w",
                                 justify="left", wraplength=int(265 * self.sc))
        self.ws_title.pack(fill="x", padx=10)
        self.ws_meta = tk.Label(card, bg=t["bg"], fg=t["dim"], font=self.font_s, anchor="w", justify="left")
        self.ws_meta.pack(fill="x", padx=10, pady=(2, 0))
        self.ws_desc = tk.Text(card, bg=t["bg"], fg=t["dim"], font=self.font_s, bd=0, wrap="word",
                               highlightthickness=0, state="disabled", height=4, cursor="arrow",
                               selectbackground=t["fg"], selectforeground=t["bg"])
        self.ws_desc.pack(fill="both", expand=True, padx=10, pady=6)

        wrap, self.ws_rl = self.listbox(mid)
        wrap.pack(side="left", fill="both", expand=True)
        self.ws_rl.bind("<<ListboxSelect>>", lambda ev: self.ws_show())
        self.ws_rl.bind("<Double-Button-1>", lambda ev: self.ws_dl_selected())
        self.ws_fill()
        if not self.ws_loaded:
            self.ws_search(1)

    def ws_search(self, page=1):
        self._ws_tok += 1
        token = self._ws_tok
        self.ws_page = page
        q, sort = self.v_ws_query.get().strip(), self.v_ws_sort.get()
        self.ws_say("LOADING WORKSHOP ...")
        try:
            self.ws_pg.config(text=f"PAGE {page}")
        except (AttributeError, tk.TclError):
            pass

        def work():
            try:
                ids = workshop_search(q, sort, page)
                det = workshop_details_many(ids) if ids else {}
                items = [dict(det[i], id=i) for i in ids if i in det and det[i]["app"] == APP_ID]
                self.ui(self._ws_results, token, items, None)
            except Exception as e:
                self.ui(self._ws_results, token, [], e)

        threading.Thread(target=work, daemon=True).start()

    def _ws_results(self, token, items, err):
        if token != self._ws_tok:
            return
        self.ws_loaded = True
        self.ws_items = items
        if err:
            self.ws_say(f"! LOAD FAILED: {err}", True)
            self.log(f"WORKSHOP LOAD FAILED: {err}", "err")
        else:
            self.ws_say("" if items else "NO RESULTS")
        if self.page == "workshop" and self.ws_mode == "browse":
            self.ws_fill()
            try:
                if items:
                    self.ws_rl.selection_set(0)
                self.ws_show()
            except (AttributeError, tk.TclError):
                pass

    def ws_fill(self):
        try:
            lb = self.ws_rl
            lb.delete(0, "end")
        except (AttributeError, tk.TclError):
            return
        man = load_json(self.ws_manifest(self.v_ws_cont.get()), {})
        for it in self.ws_items:
            have = it["id"] in man
            lb.insert("end", f"  {'[x]' if have else '[ ]'} {it['title']:<30.30} "
                             f"{fmt_num(it['subs']):>6}  {'/'.join(it['tags'])[:14]}")
            lb.itemconfig(lb.size() - 1, foreground=self.t["acc"] if have else self.t["fg"])
        if not self.ws_items:
            lb.insert("end", "  NOTHING TO SHOW.")
            lb.itemconfig(0, foreground=self.t["dim"])

    def ws_cur(self):
        try:
            s = self.ws_rl.curselection()
            return self.ws_items[s[0]] if s and s[0] < len(self.ws_items) else None
        except (AttributeError, tk.TclError):
            return None

    def ws_show(self):
        it = self.ws_cur()
        try:
            self.ws_desc.config(state="normal")
            self.ws_desc.delete("1.0", "end")
            if not it:
                self.ws_title.config(text="")
                self.ws_meta.config(text="")
                self.ws_prev.config(image="", text="")
                self._ws_img = None
                self.ws_desc.config(state="disabled")
                self.ws_bdl.enable(False)
                self.ws_bst.enable(False)
                return
            upd = time.strftime("%Y-%m-%d", time.localtime(it["updated"])) if it["updated"] else "-"
            self.ws_title.config(text=it["title"])
            self.ws_meta.config(text=f"TAGS  {', '.join(it['tags']) or '-'}\n"
                                     f"SUBS  {fmt_num(it['subs'])}   SIZE {fmt_size(it['size'])}\n"
                                     f"UPD   {upd}")
            self.ws_desc.insert("1.0", it["desc"] or "(no description)")
            self.ws_desc.config(state="disabled")
            self.ws_bdl.enable(True)
            self.ws_bst.enable(True)
            self.ws_prev.config(image="", text="LOADING ...")
            self._ws_img = None
        except (AttributeError, tk.TclError):
            return
        wid = it["id"]
        if wid in self.ws_previews:
            self._ws_apply_preview(wid)
            return

        def work():
            try:
                data = fetch_preview(it["preview"])
            except Exception:
                data = b""
            self.ui(self._ws_got_preview, wid, data)

        threading.Thread(target=work, daemon=True).start()

    def _ws_got_preview(self, wid, data):
        if len(self.ws_previews) > 80:
            self.ws_previews.clear()
        self.ws_previews[wid] = data
        cur = self.ws_cur()
        if cur and cur["id"] == wid:
            self._ws_apply_preview(wid)

    def make_image(self, data, size):
        if HAVE_PIL:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((size, size))
            return ImageTk.PhotoImage(im)
        im = tk.PhotoImage(data=base64.b64encode(data))  # без Pillow только PNG/GIF
        big = max(im.width(), im.height(), 1)
        if big > size:
            im = im.subsample(-(-big // size))
        return im

    def _ws_apply_preview(self, wid):
        data = self.ws_previews.get(wid)
        img = None
        if data:
            try:
                img = self.make_image(data, self.ws_psize - 4)
            except Exception:
                img = None
        self._ws_img = img
        try:
            self.ws_prev.config(image=img if img else "", text="" if img else "NO PREVIEW")
        except tk.TclError:
            pass

    def ws_dl_selected(self):
        it = self.ws_cur()
        if it:
            self.ws_download([it["id"]])

    def ws_open_steam(self):
        it = self.ws_cur()
        if it:
            webbrowser.open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={it['id']}")

    # ---- общее

    def ws_download(self, ids=None):
        if self.busy:
            self.ws_say("! WAIT, ANOTHER OPERATION IS RUNNING", True)
            return
        if ids is None:
            ids = parse_workshop_ids(self.v_ws_items.get())
        cname = self.v_ws_cont.get()
        if not ids:
            self.ws_say("! NO ITEM LINK OR ID FOUND", True)
            return
        self.ws_say("DOWNLOADING ... (DETAILS IN CONSOLE)")
        root = self.root
        data = os.path.join(container_dir(root, cname), "data")
        sdir = self.ws_dir()
        mpath = self.ws_manifest(cname)
        login = self.S.get("steam_user").strip() or "anonymous"
        self.busy = True
        self.update_state()
        self.log(f"WORKSHOP: {len(ids)} ITEM(S) -> '{cname}'")

        def log(m, lvl="info"):
            self.ui(self.log, m, lvl)

        def work():
            ok = 0
            try:
                exe = ensure_steamcmd(sdir, log)
            except Exception as e:
                log(f"STEAMCMD SETUP FAILED: {e}", "err")
                self.ui(self._ws_done, 0, len(ids))
                return
            dl = os.path.join(sdir, "dl")
            for wid in ids:
                try:
                    det = None
                    try:
                        det = workshop_details(wid)
                    except Exception:
                        pass
                    if det and det["app"] != APP_ID:
                        raise RuntimeError("NOT A MINDUSTRY ITEM")
                    if det and det.get("banned"):
                        raise RuntimeError("ITEM IS BANNED OR REMOVED")
                    if det and det.get("visibility") in (1, 2):
                        raise RuntimeError("ITEM IS PRIVATE OR FRIENDS-ONLY, STEAM WILL NOT GIVE IT TO ANYONE ELSE")
                    title = (det or {}).get("title") or f"item_{wid}"
                    log(f"[{wid}] {title}")
                    content = os.path.join(dl, "steamapps", "workshop", "content", str(APP_ID), wid)
                    errors = []
                    for _attempt in (1, 2):
                        shutil.rmtree(content, ignore_errors=True)
                        errors = run_steamcmd(exe, ["+force_install_dir", dl, "+login", login,
                                                    "+workshop_download_item", str(APP_ID), wid, "+quit"], log)
                        if os.path.isdir(content) and os.listdir(content):
                            break
                    if not (os.path.isdir(content) and os.listdir(content)):
                        msg = errors[-1] if errors else "NOTHING DOWNLOADED (PRIVATE ITEM OR NO CONNECTION)"
                        low = " ".join(errors).lower()
                        if login == "anonymous" and errors:
                            msg += (" | STEAM REFUSED THE ANONYMOUS DOWNLOAD. THE ITEM MAY BE RESTRICTED "
                                    "OR NEED AN ACCOUNT THAT OWNS MINDUSTRY (OPTIONAL steam_user IN SETTINGS.INI)")
                        elif any(w in low for w in ("password", "login", "credential", "guard")):
                            msg += " | PRESS LOGIN IN SETTINGS.INI TO SIGN IN AGAIN"
                        raise RuntimeError(msg)
                    kind, files = install_workshop_item(content, data, title, wid)
                    man = load_json(mpath, {})
                    man[wid] = {"title": title, "kind": kind, "files": files}
                    save_json(mpath, man)
                    log(f"[{wid}] INSTALLED AS {kind.upper()}: {', '.join(files)}")
                    ok += 1
                except Exception as e:
                    log(f"[{wid}] FAILED: {e}", "err")
            self.ui(self._ws_done, ok, len(ids))

        threading.Thread(target=work, daemon=True).start()

    def _ws_done(self, ok, total):
        self.busy = False
        self.update_state()
        self.log(f"WORKSHOP DONE: {ok}/{total}", "info" if ok == total else "err")
        if ok:
            self.v_ws_items.set("")
        self.ws_say(f"DONE: {ok}/{total}" if ok == total else f"DONE: {ok}/{total} (SEE CONSOLE)", ok != total)
        self.ws_update()
        self.refresh_info()

    def ws_remove(self):
        wid = self.ws_selected()
        cname = self.v_ws_cont.get()
        if not wid:
            return
        mp = self.ws_manifest(cname)
        man = load_json(mp, {})
        it = man.get(wid)
        if not it:
            return
        if not self.confirm("REMOVE ITEM", f"REMOVE '{it.get('title', wid)}' FROM '{cname}'?",
                            "REMOVE", danger=True):
            return
        data = os.path.join(container_dir(self.root, cname), "data")
        for rel in it.get("files", []):
            p = os.path.normpath(os.path.join(data, rel))
            if not is_inside(p, data):
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        man.pop(wid, None)
        save_json(mp, man)
        self.log(f"WORKSHOP ITEM {wid} REMOVED FROM '{cname}'")
        self.ws_refresh()

    # ----------------------------------------------------------- страница: настройки
    def page_settings(self):
        t = self.t
        outer, body = self.panel(self.content, "SETTINGS.INI")
        outer.pack(fill="both", expand=True)
        g = tk.Frame(body, bg=t["bg"])
        g.pack(fill="x", anchor="n")
        g.columnconfigure(1, weight=1)
        self.v_dir = tk.StringVar(value=self.root)
        self.v_java = tk.StringVar(value=self.S.get("java_path"))
        self.v_rmin = tk.StringVar(value=self.S.get("ram_min"))
        self.v_rmax = tk.StringVar(value=self.S.get("ram_max"))
        self.v_jvm = tk.StringVar(value=self.S.get("jvm_args"))
        self.v_game = tk.StringVar(value=self.S.get("game_args"))
        r = [0]

        def section(name):
            self.label(g, f"[{name}]", bold=True, accent=True).grid(
                row=r[0], column=0, columnspan=3, sticky="w", pady=(0 if r[0] == 0 else 12, 2))
            r[0] += 1

        def row(label, widget, extra=None, hint=None):
            self.label(g, label, dim=True).grid(row=r[0], column=0, sticky="w", padx=(0, 14), pady=3)
            widget.grid(row=r[0], column=1, sticky="ew", pady=3, ipady=2 if isinstance(widget, tk.Entry) else 0)
            if extra is not None:
                extra.grid(row=r[0], column=2, sticky="w", padx=(8, 0))
            elif hint:
                self.label(g, hint, dim=True).grid(row=r[0], column=2, sticky="w", padx=(8, 0))
            r[0] += 1

        def btns(*pairs):
            f = tk.Frame(g, bg=t["bg"])
            for text, cmd in pairs:
                self.btn(f, text, cmd).pack(side="left", padx=(0, 6))
            return f

        section("launcher")
        row("install_dir", self.entry(g, self.v_dir, 24, readonly=True),
            btns(("BROWSE", self.change_root), ("OPEN", lambda: self.safe_open(self.root))))
        tf = tk.Frame(g, bg=t["bg"])
        for name, th in THEMES.items():
            b = self.btn(tf, name, lambda n=name: self.set_theme(n), color=th["fg"])
            b.pack(side="left", padx=(0, 6))
            if name == self.S.get("theme"):
                b.activate()
        row("theme", tf)
        section("java")
        row("java_path", self.entry(g, self.v_java, 24),
            btns(("BROWSE", self.pick_java), ("AUTO", lambda: self.v_java.set(""))))
        self.lbl_java = self.label(g, self.java_info, dim=True, anchor="w")
        row("java_found", self.lbl_java)
        row("ram_min", self.entry(g, self.v_rmin, 12), hint="e.g. 256M, 1G")
        row("ram_max", self.entry(g, self.v_rmax, 12), hint="e.g. 2G, 4096M")
        row("jvm_args", self.entry(g, self.v_jvm, 24), hint="")
        row("game_args", self.entry(g, self.v_game, 24), hint="")
        self.label(g, "global args apply to every container; container args go after them.",
                   dim=True, wraplength=int(600 * self.sc), justify="left").grid(row=r[0], column=0, columnspan=3, sticky="w", pady=(4, 0))
        r[0] += 1
        section("steam")
        self.v_suser = tk.StringVar(value=self.S.get("steam_user"))
        row("steam_user", self.entry(g, self.v_suser, 24), btns(("LOGIN", self.steam_login)))
        self.label(g, "optional: your own Steam account that owns Mindustry (password goes only into the steamcmd window)",
                   dim=True, wraplength=int(600 * self.sc), justify="left").grid(row=r[0], column=0, columnspan=3, sticky="w", pady=(2, 0))
        r[0] += 1
        bar = tk.Frame(body, bg=t["bg"])
        bar.pack(fill="x", pady=(16, 0), anchor="n")
        self.btn(bar, "SAVE", self.settings_save, accent=True).pack(side="left", padx=(0, 8))
        self.btn(bar, "RESET", lambda: self.show_page("settings")).pack(side="left", padx=(0, 12))
        self.set_msg = self.label(bar, "", accent=True)
        self.set_msg.pack(side="left")

    def steam_login(self):
        """Открывает окно steamcmd, где пользователь сам вводит пароль и код Steam Guard.
        Лаунчер пароль не видит; steamcmd запоминает вход и дальше качает от имени аккаунта."""
        user = self.v_suser.get().strip()
        if not user or " " in user:
            self.set_msg.config(text="! ENTER YOUR STEAM LOGIN NAME FIRST")
            return
        self.S.set("steam_user", user)
        self.S.save()
        sdir = self.ws_dir()
        self.log("PREPARING STEAMCMD LOGIN ...")

        def log(m, lvl="info"):
            self.ui(self.log, m, lvl)

        def work():
            try:
                exe = ensure_steamcmd(sdir, log)
                if IS_WIN:
                    subprocess.Popen([exe, "+login", user], cwd=os.path.dirname(exe),
                                     creationflags=0x10)  # CREATE_NEW_CONSOLE
                    log("STEAMCMD WINDOW OPENED. ENTER PASSWORD AND STEAM GUARD CODE THERE, "
                        "WAIT FOR 'Waiting for user info...OK', THEN TYPE quit")
                else:
                    log(f"RUN IN A TERMINAL: {exe} +login {user}   (THEN TYPE quit)")
            except Exception as e:
                log(f"STEAM LOGIN FAILED: {e}", "err")

        threading.Thread(target=work, daemon=True).start()

    def safe_open(self, path):
        try:
            open_path(path)
        except Exception as e:
            self.log(f"OPEN FAILED: {e}", "err")

    def set_theme(self, name):
        if name == self.S.get("theme"):
            return
        self.S.set("theme", name)
        self.S.save()
        self.t = THEMES[name]
        self.build_all()
        self.log(f"THEME = {name}")

    def pick_java(self):
        ft = [("java", "java.exe" if IS_WIN else "java"), ("all", "*")]
        p = filedialog.askopenfilename(parent=self, title="JAVA EXECUTABLE", filetypes=ft)
        if p:
            self.v_java.set(os.path.normpath(p))

    def settings_save(self):
        mn, mx = parse_ram(self.v_rmin.get()), parse_ram(self.v_rmax.get())
        jp = self.v_java.get().strip()
        if mn is None or mx is None or mn < 64 or mx < 64:
            self.set_msg.config(text="! RAM: USE 512M OR 2G (MIN 64M)")
            return
        if mn > mx:
            self.set_msg.config(text="! RAM_MIN IS GREATER THAN RAM_MAX")
            return
        if jp and not os.path.isfile(jp):
            self.set_msg.config(text="! JAVA FILE NOT FOUND")
            return
        self.S.set("ram_min", fmt_ram(mn))
        self.S.set("ram_max", fmt_ram(mx))
        self.S.set("java_path", jp)
        self.S.set("jvm_args", self.v_jvm.get().strip())
        self.S.set("game_args", self.v_game.get().strip())
        self.S.set("steam_user", self.v_suser.get().strip())
        self.S.save()
        self.v_rmin.set(fmt_ram(mn))
        self.v_rmax.set(fmt_ram(mx))
        self.set_msg.config(text="")
        self.log("SETTINGS.INI SAVED")
        self.detect_java()

    def detect_java(self):
        custom = self.S.get("java_path")

        def work():
            p = find_java(custom)
            info = f"{java_version(p)}" if p else "NOT FOUND (install Java 17+)"
            self.ui(self._java_done, info, bool(p))

        threading.Thread(target=work, daemon=True).start()

    def _java_done(self, info, found):
        self.java_info = info
        if not found:
            self.log("JAVA NOT FOUND. INSTALL JAVA 17+ OR SET java_path.", "err")
        self.refresh_info()
        try:
            self.lbl_java.config(text=info, fg=self.t["dim"] if found else self.t["acc"])
        except (AttributeError, tk.TclError):
            pass

    # ----------------------------------------------------------- смена папки установки
    def change_root(self):
        if self.busy or self.running:
            self.alert("BUSY", "WAIT FOR DOWNLOADS AND CLOSE RUNNING GAMES FIRST.")
            return
        sel = filedialog.askdirectory(parent=self, initialdir=self.root, title="INSTALL FOLDER")
        if not sel:
            return
        new = os.path.normpath(sel)
        try:
            if os.listdir(new) and not any(os.path.isdir(os.path.join(new, x)) for x in ("versions", "containers")):
                new = os.path.join(new, "MindustryLauncher")
        except OSError:
            pass
        old = os.path.normpath(self.root)
        if os.path.normcase(new) == os.path.normcase(old):
            return
        if is_inside(new, old):
            self.alert("INVALID FOLDER", "THE NEW FOLDER CANNOT BE INSIDE THE CURRENT ONE.")
            return
        choice = self.ask("INSTALL FOLDER",
                          f"NEW PATH:\n{new}\n\nMOVE EXISTING VERSIONS AND CONTAINERS THERE?",
                          [("CANCEL", None, False), ("KEEP IN PLACE", False, False), ("MOVE", True, True)])
        if choice is None:
            return
        if not choice:
            self.apply_root(new, "INSTALL FOLDER CHANGED (OLD FILES LEFT IN PLACE)")
            return
        self.busy = True
        self.update_state()
        self.log("MOVING FILES ...")

        def work():
            try:
                subs = [s for s in ("versions", "containers") if os.path.isdir(os.path.join(old, s))]
                for s in subs:
                    d = os.path.join(new, s)
                    if os.path.isdir(d) and os.listdir(d):
                        raise RuntimeError(f"{d} IS NOT EMPTY")
                os.makedirs(new, exist_ok=True)
                for s in subs:
                    d = os.path.join(new, s)
                    if os.path.isdir(d):
                        os.rmdir(d)
                    shutil.move(os.path.join(old, s), d)
                self.ui(self._move_done, new, None)
            except Exception as e:
                self.ui(self._move_done, new, e)

        threading.Thread(target=work, daemon=True).start()

    def _move_done(self, new, err):
        self.busy = False
        if err:
            self.update_state()
            self.log(f"MOVE FAILED: {err}", "err")
            return
        self.apply_root(new, "FILES MOVED. INSTALL FOLDER CHANGED")

    def apply_root(self, new, msg):
        self.S.set("install_dir", new)
        self.S.save()
        self.make_dirs()
        self.update_state()
        self.log(f"{msg}: {new}")
        self.show_page(self.page)


if __name__ == "__main__":
    App().mainloop()
