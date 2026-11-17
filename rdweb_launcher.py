#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RD Web Launcher — лаунчер публикаций RemoteApp / рабочих столов RD Web Access.

Порядок работы (согласно требованиям):
  1. Получает текущего пользователя Windows (getpass/os.environ) — пароль НЕ
     запрашивается и нигде не хранится.
  2. Подключается к RD Web от имени этого пользователя с Windows-аутентификацией
     (Negotiate/Kerberos) через pyspnego, передавая текущий Windows-токен.
  3. Загружает список доступных ресурсов (Atom feed /feedlocal.rdp, fallback —
     парсинг страницы usergroup_ldap.xml.asp) и строит из него дерево папок.
  4. При выборе ресурса получает .rdp-файл по прямой ссылке; если ссылки нет —
     формирует .rdp по согласованным с администратором правилам (config.ini).
  5. Запускает файл через стандартный клиент Windows — mstsc.exe
     (mstsc.exe /v:<host> /admin ... либо ассоциация файла .rdp).

Сборка .exe (на машине с Windows):
  pip install -r requirements.txt pyinstaller
  pyinstaller --onefile --windowed --name RDWebLauncher rdweb_launcher.py
  Готовый exe: dist/RDWebLauncher.exe
"""

import configparser
import getpass
import html
import os
import re
import subprocess
import sys
import tempfile

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    HAVE_TK = True
except ImportError:  # GUI недоступен (например, в Linux-окружении сборки)
    tk = None
    messagebox = None
    ttk = None
    HAVE_TK = False

from urllib.parse import quote, urljoin, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import spnego  # pyspnego — Negotiate/Kerberos поверх текущего Windows-токена
    HAVE_SPNEGO = True
except ImportError:
    HAVE_SPNEGO = False

APP_TITLE = "RD Web Launcher"
CONFIG_FILE_NAME = "config.ini"

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_TEXT = """\
[rdweb]
; Полный URL RD Web Access (Windows Server 2016+ или RDS Gateway web)
url = https://rdweb.contoso.com/RDWeb/Pages/en-US/Default.aspx
; Проверять TLS-сертификат (true/false)
verify_ssl = true
; Опционально: явно указать домен (если не берётся из user@domain формата)
domain =

[rdp]
; Правила формирования .rdp, согласованные с администратором.
; Применяются, когда у публикации нет прямой ссылки на .rdp-файл,
; а также как базовые параметры для скачанных файлов (дополняются).
screenmodeid:i = 1
session bpp:i = 32
prompt for credentials on client:i = 1
redirectinputmode:i = 1
audiomode:i = 0
; Разрешить сохранение пароля в mstsc (обычно 0 — пароль не сохраняется)
disable password saving:i = 0

[launcher]
; Путь к клиенту; по умолчанию системный mstsc.exe
mstsc = C:\\Windows\\System32\\mstsc.exe
; Каталог для временных .rdp файлов (пусто = %%TEMP%%)
rdp_dir =
"""


def config_path() -> str:
    """Файл конфигурации лежит рядом с .exe (или со скриптом в dev-режиме)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILE_NAME)


def load_config() -> configparser.ConfigParser:
    # delimiters="=" — чтобы ключи .rdp вида "screenmodeid:i" не разбирались
    # по двоеточию; interpolation=None — "%TEMP%" и пути не искажаются.
    cfg = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cfg.optionxform = str  # сохраняем регистр ключей (важно для секции [rdp])
    cfg.read_string(DEFAULT_CONFIG_TEXT)
    path = config_path()
    if not os.path.exists(path):
        # Создаём шаблон, чтобы администратор мог отредактировать.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_CONFIG_TEXT)
    else:
        cfg.read(path, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Шаг 1. Текущий пользователь Windows (без пароля)
# ---------------------------------------------------------------------------

def current_user() -> str:
    """Логин текущего пользователя Windows. Пароль не запрашивается."""
    login = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or getpass.getuser()
    )
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{login}" if domain else login


def user_components(cfg: configparser.ConfigParser):
    login = os.environ.get("USERNAME") or getpass.getuser()
    domain = (
        os.environ.get("USERDOMAIN")
        or cfg.get("rdweb", "domain", fallback="")
        or ""
    )
    return login, domain


# ---------------------------------------------------------------------------
# Шаг 2. Сессия HTTP с Windows-аутентификацией (Negotiate/Kerberos)
# ---------------------------------------------------------------------------

class NtlmKerberosAuth(requests.auth.AuthBase if requests else object):
    """Аутентификация «от имени текущего пользователя» без передачи пароля.

    Используется pyspnego: клиент берёт текущий Windows-логон-токен
    (SSPI / Kerberos / NTLM), то есть фактические учётные данные никогда
    не покидают машину и не запрашиваются у пользователя.
    """

    def __init__(self, service: str):
        self.service = service  # http://host — SPN сервиса

    def __call__(self, r):
        # username/password=None — pyspnego использует текущий Windows-токен
        # (SSPI): Kerberos, если доступен, иначе NTLM. Пароль не запрашивается.
        token = b""
        auth_value = None
        with spnego.client(
            username=None,
            password=None,
            service=self.service,
            protocol="kerberos,ntlm",
            negotiate_target="http",
        ) as ctx:
            while True:
                resp = ctx.step(token)
                if resp is None or resp.authenticator is None:
                    break
                token = resp.token or b""
                auth_value = resp.authenticator
                if resp.complete:
                    break
        if auth_value is not None:
            r.headers["Authorization"] = auth_value
        return r


def build_session(cfg: configparser.ConfigParser):
    url = cfg.get("rdweb", "url").strip()
    verify = cfg.getboolean("rdweb", "verify_ssl", fallback=True)
    s = requests.Session()
    s.headers.update({"User-Agent": f"{APP_TITLE}/1.0"})
    host = urlparse(url).hostname or ""
    if HAVE_SPNEGO and os.name == "nt":
        s.auth = NtlmKerberosAuth(f"http://{host}")
    return s, url, verify


# ---------------------------------------------------------------------------
# Шаг 3. Загрузка ресурсов и построение дерева папок
# ---------------------------------------------------------------------------

class Resource:
    """Одна публикация RD Web."""

    def __init__(self, alias: str, name: str, rdp_url: str = "",
                 type_: str = "Application"):
        self.alias = alias          # например "RemoteDesktop/CONTOSO-WS"
        self.name = name            # отображаемое имя
        self.rdp_url = rdp_url      # прямая ссылка на .rdp (если есть)
        self.type = type_

    @property
    def folder(self):
        """Папка публикации: 'RemoteApplications', 'RemoteDesktop' и т.п."""
        return self.alias.split("/")[0] if "/" in self.alias else ""

    @property
    def display(self):
        return self.name or self.alias


def parse_atom_feed(text: str):
    """Разбирает Atom-ленту RD Web (/RDWeb/Feed/<user>) — формат Feeds/RDWeb."""
    items = []
    for entry in re.findall(r"<entry.*?</entry>", text, re.S):
        # Имя публикации из <d:Name>…</d:Name> (или <m:name>)
        name_m = re.search(
            r"<(?:[A-Za-z0-9_.]*:)?(?:name|alias)>(.*?)</", entry, re.I | re.S)
        name = html.unescape(name_m.group(1)).strip() if name_m else ""
        # Alias: либо <d:Alias>, либо из ссылки Downloads/Desktops.aspx?Alias=…
        alias_m = re.search(
            r"<(?:[A-Za-z0-9_.]*:)?alias>(.*?)</", entry, re.I | re.S)
        alias = html.unescape(alias_m.group(1)).strip() if alias_m else ""
        link_m = re.search(r'<link[^>]*href="([^"]+)"', entry, re.I)
        href = html.unescape(link_m.group(1)) if link_m else ""
        if not alias:
            a = re.search(r"[?&]Alias=([^&\"<>]+)", href, re.I)
            if a:
                alias = html.unescape(a.group(1)).replace("%2f", "/")
        if not name:
            n = re.search(r"[?&]Name=([^&\"<>]*)", href, re.I)
            name = html.unescape(n.group(1)).replace("+", " ") if n else ""
        if not name:
            t = re.search(r"<title[^>]*>(.*?)</title>", entry, re.S)
            name = html.unescape(t.group(1)).strip() if t else alias
        typ_m = re.search(r"[?&]Type=([^&\"<>]*)", href, re.I)
        typ = html.unescape(typ_m.group(1)) if typ_m else "Application"
        # Прямая ссылка на .rdp (встречается в Feeds-сборке)
        rdp_m = re.search(r'<link[^>]*href="([^"]+\.rdp[^"]*)"', entry, re.I)
        rdp_url = html.unescape(rdp_m.group(1)) if rdp_m else ""
        if alias or name:
            items.append(Resource(alias, name, rdp_url, typ))
    return items


def parse_web_page(html_text: str, base_url: str):
    """Fallback: разбор Default.aspx / usergroup_ldap.xml.asp (формат RDWeb)."""
    items = []
    # Публикации вида: <a href="/RDWeb/Pages/en-US/...Default.aspx?Alias=RemoteApplications%2fMyApp&Name=...&Type=Application...">
    # NB: все группы параметров обёрнуты в группу href (...) — иначе regex
    # не компилируется (несбалансированные скобки внутри именованной группы).
    pattern = re.compile(
        r'<a[^>]+href="(?P<href>(?:[^"]*?Alias=(?P<alias>[^&"<>]*)'
        r'(?:&(?:amp;)?Name=(?P<name>[^&"<>]*))?'
        r'(?:&(?:amp;)?Type=(?P<type>[^&"<>]*))?[^"]*))"'
        r'[^>]*>(?P<label>.*?)</a>',
        re.I | re.S,
    )
    seen = set()
    for m in pattern.finditer(html_text):
        alias = html.unescape(m.group("alias") or "").replace("%2f", "/")
        name = html.unescape(m.group("name") or "")
        typ = html.unescape(m.group("type") or "Application")
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group("label"))).strip()
        key = (alias, label)
        if not alias or key in seen:
            continue
        seen.add(key)
        href = urljoin(base_url, html.unescape(m.group("href")))
        # Прямой ссылки на .rdp на этой странице нет — получим при запуске
        # (RD Web отдаёт file.aspx?...&Type=... либо формируем по правилам).
        items.append(Resource(alias, name or label, "", typ))
    return items


def fetch_resources(session, cfg, base_url, verify):
    """Пробует Atom-фид, затем HTML-страницу. Возвращает список Resource."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [
        f"{origin}/RDWeb/Feed/{quote(os.environ.get('USERNAME') or getpass.getuser())}",
        f"{origin}/RDWeb/Feed/",
    ]
    errors = []
    for url in candidates:
        try:
            r = session.get(url, verify=verify, timeout=30)
            if r.status_code == 200 and "<entry" in r.text.lower():
                items = parse_atom_feed(r.text)
                if items:
                    return items, url
                errors.append(f"{url}: лента пуста")
            else:
                errors.append(f"{url}: HTTP {r.status_code}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    # Fallback — парсинг самой страницы RD Web
    try:
        r = session.get(base_url, verify=verify, timeout=30)
        r.raise_for_status()
        items = parse_web_page(r.text, base_url)
        if items:
            return items, base_url
        errors.append(f"{base_url}: публикации не найдены на странице")
    except Exception as exc:
        errors.append(f"{base_url}: {exc}")
    raise RuntimeError(
        "Не удалось получить список ресурсов RD Web.\n\n"
        + "\n".join(errors)
        + "\n\nУбедитесь, что машина в домене, Kerberos/Negotiate настроен, "
          "а URL в config.ini верен."
    )


def build_tree(resources):
    """dict: {папка: [Resource, ...]} — дерево папок."""
    tree = {}
    for res in resources:
        tree.setdefault(res.folder or "Прочее", []).append(res)
    for folder in tree:
        tree[folder].sort(key=lambda x: x.display.lower())
    return dict(sorted(tree.items()))


# ---------------------------------------------------------------------------
# Шаг 4. Получение / формирование .rdp
# ---------------------------------------------------------------------------

def rdp_rules(cfg: configparser.ConfigParser) -> str:
    """Правила формирования .rdp из секции [rdp].

    Запись в config.ini:  имя_параметра = тип значение
    (например, `screenmodeid:i = 1` или `screenmodeid = i 1`) превращается
    в строку формата .rdp:  "screenmodeid:i=1".
    Если тип не указан — используется :s (строка).
    """
    lines = []
    for key, value in cfg.items("rdp"):
        key = key.strip()
        value = value.strip()
        if ":" in key:                     # "screenmodeid:i" = "1"
            lines.append(f"{key}={value}")
        else:
            parts = value.split(None, 1)   # "i 1" -> тип i, значение 1
            if len(parts) == 2 and parts[0] in ("s", "i", "b"):
                lines.append(f"{key}:{parts[0]}={parts[1]}")
            else:
                lines.append(f"{key}:s={value}")
    return "\r\n".join(lines) + "\r\n"


def acquire_rdp(session, cfg, base_url, verify, res: Resource):
    """Возвращает текст .rdp файла: скачанный или сформированный по правилам."""
    body = None
    if res.rdp_url:
        url = urljoin(base_url, res.rdp_url)
        r = session.get(url, verify=verify, timeout=30)
        r.raise_for_status()
        body = r.content.decode("utf-8-sig", errors="replace")
    else:
        # Попытка скачать через file.aspx RD Web (2012+ путь)
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        qs = (f"?Alias={quote(res.alias)}"
              f"&Name={quote(res.display)}"
              f"&Type={quote(res.type)}")
        for path in ("/RDWeb/Pages/en-US/file.aspx", "/RDWeb/pages/en-us/file.aspx"):
            try:
                r = session.get(origin + path + qs, verify=verify, timeout=30)
                if r.ok and ("dvremoteapp" in r.text.lower()
                             or ".rdp" in r.url.lower()
                             or "full address" in r.text.lower()):
                    body = r.text
                    break
            except Exception:
                continue
    if body is None:
        # Формируем .rdp по согласованным с администратором правилам.
        host = res.alias.split("/", 1)[-1] if "/" in res.alias else res.alias
        generated = (
            f"full address:s:{host}\r\n"
            f"remoteapplicationname:s:{res.display}\r\n"
            f"remoteapplicationmode:i:0\r\n"
        )
        generated += rdp_rules(cfg)
        return generated, "generated"
    # Дополняем скачанный файл правилами администратора (не перетирая его ключи)
    existing_keys = {ln.split(":", 1)[0].lower()
                     for ln in body.splitlines() if ":" in ln}
    extra = [ln for ln in rdp_rules(cfg).splitlines()
             if ln.split(":", 1)[0].lower() not in existing_keys]
    merged = body.rstrip("\r\n") + ("\r\n" + "\r\n".join(extra) if extra else "")
    return merged + "\r\n", "downloaded"


def save_rdp(cfg, res: Resource, content: str) -> str:
    rd = cfg.get("launcher", "rdp_dir", fallback="").strip()
    target_dir = rd or os.path.join(tempfile.gettempdir(), "RDWebLauncher")
    os.makedirs(target_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", res.display)[:80] or "resource"
    path = os.path.join(target_dir, f"{safe}.rdp")
    with open(path, "w", encoding="utf-16-le") as fh:  # mstsc ожидает UTF-16
        fh.write("\ufeff" + content)
    return path


# ---------------------------------------------------------------------------
# Шаг 5. Запуск через mstsc.exe
# ---------------------------------------------------------------------------

def launch_mstsc(cfg, rdp_path: str):
    mstsc = cfg.get("launcher", "mstsc",
                    fallback=r"C:\Windows\System32\mstsc.exe").strip()
    if not os.path.isfile(mstsc):
        mstsc = "mstsc.exe"  # полагаемся на PATH
    try:
        # Стандартный способ — открыть .rdp через mstsc.exe
        subprocess.Popen([mstsc, rdp_path])
    except OSError:
        # Последний вариант — стандартная ассоциация .rdp в Windows
        os.startfile(rdp_path)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# GUI (tkinter): дерево папок + выбор публикации
# ---------------------------------------------------------------------------

if HAVE_TK:

    class LauncherGUI(tk.Tk):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg
            self.title(f"{APP_TITLE} — {current_user()}")
            self.geometry("700x480")
            self.tree_map = {}          # iid -> Resource
            self._build()
            self.after(200, self.refresh)

        def _build(self):
            top = ttk.Frame(self, padding=6)
            top.pack(fill="x")
            ttk.Label(top, text=f"Пользователь: {current_user()}").pack(side="left")
            ttk.Button(top, text="Обновить", command=self.refresh).pack(side="right")
            self.status = tk.StringVar(value="Подключение к RD Web…")
            ttk.Label(self, textvariable=self.status, padding=(8, 2)).pack(fill="x")

            wrap = ttk.Frame(self)
            wrap.pack(fill="both", expand=True, padx=6, pady=4)
            self.tree = ttk.Treeview(wrap, show="tree", selectmode="browse")
            scroll = ttk.Scrollbar(wrap, command=self.tree.yview)
            self.tree.configure(yscrollcommand=scroll.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.tree.bind("<Double-1>", lambda e: self.run_selected())
            self.tree.bind("<Return>", lambda e: self.run_selected())

            bottom = ttk.Frame(self, padding=6)
            bottom.pack(fill="x")
            ttk.Button(bottom, text="Запустить выбранное",
                       command=self.run_selected).pack(side="right")

        def refresh(self):
            self.tree.delete(*self.tree.get_children())
            self.tree_map.clear()
            self.status.set("Загрузка списка ресурсов…")
            self.update_idletasks()

            def work():
                try:
                    session, url, verify = build_session(self.cfg)
                    resources, src = fetch_resources(session, self.cfg, url, verify)
                    self._populate(build_tree(resources), src)
                except Exception as exc:
                    self.after(0, lambda: self.status.set(f"Ошибка: {exc}"))
                    self.after(0, lambda: messagebox.showerror(APP_TITLE, str(exc)))
            import threading
            threading.Thread(target=work, daemon=True).start()

        def _populate(self, tree_dict, source):
            def done():
                for folder, items in tree_dict.items():
                    fid = self.tree.insert("", "end", text=folder, open=True)
                    for res in items:
                        iid = self.tree.insert(fid, "end",
                                               text=f"{res.display}  [{res.type}]")
                        self.tree_map[iid] = res
                self.status.set(
                    f"Найдено публикаций: {sum(len(v) for v in tree_dict.values())}"
                    f"  (источник: {source})")
            self.after(0, done)

        def run_selected(self):
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo(APP_TITLE, "Выберите публикацию в дереве.")
                return
            iid = sel[0]
            res = self.tree_map.get(iid)
            if res is None:
                return
            self.status.set(f"Получение .rdp для «{res.display}»…")
            self.update_idletasks()
            try:
                session, url, verify = build_session(self.cfg)
                content, how = acquire_rdp(session, self.cfg, url, verify, res)
                path = save_rdp(self.cfg, res, content)
                self.status.set(f"Запуск {os.path.basename(path)} ({how})")
                launch_mstsc(self.cfg, path)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, f"Не удалось запустить:\n{exc}")
                self.status.set("Ошибка запуска.")


def main():
    cfg = load_config()
    problems = []
    if requests is None:
        problems.append("не установлен модуль 'requests'")
    if not HAVE_TK:
        problems.append("нет tkinter (GUI)")
    if problems:
        sys.stderr.write(APP_TITLE + ": " + "; ".join(problems) + "\n")
        sys.exit(1)
    app = LauncherGUI(cfg)
    app.mainloop()


if __name__ == "__main__":
    main()
