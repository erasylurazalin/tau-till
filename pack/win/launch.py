"""Запуск и обновление кассы Tau Till на кассовом компьютере.

Runs on Windows under the embedded Python that ships next to it.  Nothing here
is imported by the till itself: this file starts things, watches them, updates
them and stops them again.

    pythonw.exe launch.py             запустить кассу
    pythonw.exe launch.py --boot      то же, но с паузой (автозапуск)
    pythonw.exe launch.py --restart   перезапустить
    pythonw.exe launch.py --stop      остановить
    pythonw.exe launch.py --update    обновить и запустить
    python.exe  launch.py --check     проверить компьютер и написать отчёт

Три вещи должны быть верны одновременно, чтобы магазин работал: сервер поднят,
браузер показывает его на весь экран, и ни то ни другое не является второй
копией уже работающего.  В этом вся работа файла.

Клавиатуры на кассе нет, поэтому здесь ничего никогда не ждёт нажатия клавиши,
а каждая беда заканчивается окном с кнопкой ОК, достаточно большой для пальца.
"""
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
PYDIR = ROOT / "python"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
STATE = DATA / "running.json"
SETTINGS = ROOT / "settings.json"
UPDATE_FLAG = DATA / "update-requested"
MINIMIZE_FLAG = DATA / "minimize-requested"
# Свой список корневых сертификатов.  Лежит рядом с программой, а не внутри
# папки app: обновление подменяет app целиком, и остаться без сертификатов
# посреди обновления значит потерять возможность обновляться дальше.
CA_FILE = ROOT / "cacert.pem"

NAME = "Tau Till"

# Создание процесса без окна консоли.  Без этого каждый вспомогательный
# процесс мигал бы чёрным окном поперёк экрана магазина.
NO_WINDOW = 0x08000000
STILL_ACTIVE = 259
LOG_KEEP = 30            # около месяца ежедневных файлов

DEFAULTS = {
    "printer": "XP-58",   # имя принтера в «Устройства и принтеры»
    "port": 8000,
    "shop": "МАГАЗИН",    # печатается в шапке каждого чека
    "boot_delay": 12,     # секунд перед стартом при автозапуске
    "update_url": "",     # адрес version.json, пустой значит обновлений нет
    "owner": "ХОЗЯИН",    # заводится только при первом запуске на пустой базе
    "owner_pin": "0000",
}

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


# --- настройки ------------------------------------------------------------
def settings():
    """Читает settings.json, но никогда не падает из-за него.

    A shop must still open if someone edits that file and forgets a comma, so a
    broken file is logged and the defaults are used instead.
    """
    cfg = dict(DEFAULTS)
    try:
        if SETTINGS.exists():
            cfg.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        log(f"settings.json не прочитан ({e}), беру настройки по умолчанию")
    return cfg


# --- журнал ---------------------------------------------------------------
def log_file():
    """Файл журнала за сегодня, уже готовый к дописыванию.

    The byte order mark is written here and nowhere else.  Two writers append
    to this file, the launcher line by line and the server through its
    redirected output, and when both tried to stamp the mark themselves one of
    them landed in the middle of the log.
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    f = LOGS / ("tau-%s.log" % datetime.now().strftime("%Y-%m-%d"))
    if not f.exists():
        try:
            f.write_bytes(b"\xef\xbb\xbf")   # Блокнот Windows 7 иначе не поймёт
        except OSError:
            pass
    return f


def log(msg):
    """Одна строка в журнал сегодняшнего дня."""
    line = "%s  %s\r\n" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        with open(log_file(), "ab") as fh:
            fh.write(line.encode("utf-8"))
    except OSError:
        pass          # сломанный журнал не имеет права останавливать магазин
    if "--check" in sys.argv or "--console" in sys.argv:
        print(line.rstrip())


def trim_logs():
    try:
        for f in sorted(LOGS.glob("tau-*.log"))[:-LOG_KEEP]:
            f.unlink()
    except OSError:
        pass


def tail(path, lines=15):
    try:
        # utf-8-sig, иначе метка в начале файла попадёт в первую строку
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def box(text, title=NAME, icon=0x10):
    """Сообщение на весь экран, которое закрывается пальцем.

    MB_SYSTEMMODAL кладёт его поверх полноэкранного браузера, иначе его никто
    никогда не увидит.
    """
    log("сообщение на экран: " + text.replace("\n", " "))
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, icon | 0x1000)
    except Exception:
        pass


# --- процессы -------------------------------------------------------------
def alive(pid):
    """Жив ли процесс с таким номером."""
    if not pid:
        return False
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFO
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


def kill(pid, what):
    """Снять процесс вместе с детьми.

    Chrome is a family of processes, not one, so nothing short of /T actually
    closes the browser.
    """
    if not alive(pid):
        return
    log(f"останавливаю {what} (номер {pid})")
    subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"],
                    creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(server=None, chrome=None):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        STATE.write_text(json.dumps({"server": server, "chrome": chrome}),
                         encoding="utf-8")
    except OSError:
        pass


# --- сервер ---------------------------------------------------------------
def port_answers(port, timeout=0.4):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_server(cfg):
    """Поднять сервер, а если не вышло, попробовать вторым способом.

    Запасной вариант не блажь.  python.exe без консоли уже один раз отказался
    стартовать на кассе, а pythonw.exe консоль не трогает вовсе, и если
    подобное повторится, магазин откроется сам, без звонка и без поездки.
    """
    pid = start_server(cfg, "python.exe", quiet=True)
    if pid:
        return pid
    log("python.exe не поднял сервер, пробую pythonw.exe")
    return start_server(cfg, "pythonw.exe")


def start_server(cfg, exe_name="python.exe", quiet=False):
    """Запустить кассовый сервер и дождаться, пока он начнёт отвечать."""
    exe = PYDIR / exe_name
    if not exe.exists():
        if not quiet:
            box("Не найден Python в папке:\n%s\n\nПереустановите кассу." % PYDIR)
        return None

    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["POS_DB"] = str(DATA / "store.db")
    env["POS_PORT"] = str(cfg["port"])
    env["POS_PRINTER"] = str(cfg["printer"])
    env["POS_SHOP"] = str(cfg["shop"])
    env["POS_OWNER"] = str(cfg["owner"])
    env["POS_OWNER_PIN"] = str(cfg["owner_pin"])
    # Без этого русский вывод сервера просто нечем записать в журнал на машине
    # с кодовой страницей консоли 866.
    env["PYTHONIOENCODING"] = "utf-8"

    logf = log_file()
    log("запускаю сервер кассы (%s)" % exe_name)
    # Сервер пишет прямо в тот же журнал; у ребёнка своя копия дескриптора,
    # поэтому наш закрывается сразу же.
    with open(logf, "ab") as handle:
        proc = subprocess.Popen(
            [str(exe), "-u", str(APP / "server.py")],
            cwd=str(APP), env=env,
            # stdin обязателен именно так.  Процесс запускается без консоли,
            # и если ввод не перенаправить, он наследует консольный дескриптор
            # родителя, которого уже нет.  Python на старте пытается открыть
            # эту консоль и падает с «can't initialize sys standard streams»
            # ещё до первой строки нашего кода.
            stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW)

    for _ in range(60):                      # до 30 секунд
        if port_answers(cfg["port"]):
            log(f"сервер отвечает на порту {cfg['port']}, номер {proc.pid}")
            return proc.pid
        if proc.poll() is not None:
            if not quiet:
                box("Касса не запустилась.\n\nПоследние строки журнала:\n\n"
                    + tail(logf))
            return None
        time.sleep(0.5)

    kill(proc.pid, "сервер")
    if not quiet:
        box("Касса не ответила за 30 секунд.\n\nПоследние строки журнала:\n\n"
            + tail(logf))
    return None


# --- браузер --------------------------------------------------------------
def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    # Chrome, поставленный в необычное место, всё равно записывается сюда, так
    # что это ответ самой Windows.
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        r"\App Paths\chrome.exe") as k:
                    path = winreg.QueryValue(k, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    except ImportError:
        pass
    return None


def start_chrome(cfg):
    """Открыть кассу на весь экран.

    Every flag here answers something that would otherwise appear on top of the
    till: the restore-tabs bar after a power cut, the translation offer, the
    zoom that a stray two-finger touch produces, the swipe that navigates back
    out of the page.  The profile is separate so none of this touches the
    browser the owners use for anything else.
    """
    chrome = find_chrome()
    if not chrome:
        box("Не найден браузер Google Chrome.\n\n"
            "Он нужен, чтобы показать кассу на весь экран.\n"
            "Установите Chrome и запустите кассу снова.")
        return None

    url = "http://127.0.0.1:%s/" % cfg["port"]
    args = [
        chrome,
        "--user-data-dir=" + str(DATA / "chrome"),
        "--kiosk",
        "--app=" + url,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--disable-pinch",
        "--force-device-scale-factor=1",
        "--overscroll-history-navigation=0",
        "--disable-translate",
        "--disable-features=TranslateUI,Translate",
        "--disable-background-networking",
        "--disable-component-update",
        "--check-for-update-interval=31536000",
        "--password-store=basic",
        "--disable-breakpad",
    ]
    log("открываю браузер: " + chrome)
    # Ни один поток не наследуется: у запускающего процесса консоли нет, и
    # передавать браузеру её несуществующие дескрипторы незачем.
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return proc.pid


def minimize_chrome(pid):
    """Свернуть окно браузера по номеру процесса.

    Chrome is several processes, but only the one launched directly here owns
    a top-level window; the renderer and GPU children EnumWindows never sees.
    Nothing raises if the window is already gone -- the next supervise() tick
    just finds the process dead and moves on.
    """
    user32 = ctypes.windll.user32
    found = []

    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(each)
    user32.EnumWindows(proc, 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE


# --- обновление -----------------------------------------------------------
def local_version():
    """Версия, которая сейчас лежит в папке app.

    Читается как текст, а не импортом: сразу после подмены папки импортировать
    из неё что-либо в уже запущенном процессе значит напрашиваться на чудеса.
    """
    try:
        text = (APP / "version.py").read_text(encoding="utf-8")
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def newer(remote, local):
    """Больше ли remote, чем local, по числам версии."""
    def parts(v):
        return [int(x) for x in re.findall(r"\d+", str(v))] or [0]
    return parts(remote) > parts(local)


def too_many(err):
    """Отказ из-за числа запросов, а не из-за самого файла."""
    return getattr(err, "code", None) == 429


def retry_wait(err, default):
    """Сколько ждать перед следующей попыткой, по подсказке сервера.

    Ждать долго нельзя: к этому моменту касса уже остановлена, и каждая
    секунда это секунда, когда в магазине нечем пробить чек.  Поэтому что бы
    сервер ни просил, дольше пятнадцати секунд не ждём, а честно сдаёмся и
    предлагаем нажать ОБНОВИТЬ КАССУ попозже.
    """
    try:
        asked = int((err.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        asked = 0
    return max(1, min(15, asked or default))


def https_get(url, timeout=60, tries=3, fresh=True):
    """Скачать по HTTPS с проверкой подлинности сайта.

    Список корневых сертификатов везём свой.  На Windows 7, которую годами не
    обновляли, системный список отстал от жизни, и проверка может провалиться
    на совершенно исправном сервере.

    fresh добавляет к адресу метку времени, чтобы обойти кэш раздачи.  Она
    нужна для списка обновления, который лежит по одному и тому же адресу и
    меняется, и вредна для архива выпуска: тот привязан к метке версии и
    никогда не меняется, а метка времени мешала бы его кэшировать.

    GitHub считает запросы с одного интернет-адреса и, когда их набирается
    много, отвечает 429 вместо файла.  Набирается это быстрее, чем кажется:
    одно обновление это девять запросов (список и восемь файлов), а в день с
    несколькими выпусками их выходит несколько десятков.  Пара повторов
    вытаскивает случайный отказ, а если магазину действительно закрыли
    доступ на час, повторы не помогут, и лучше сказать об этом словами.
    """
    ctx = (ssl.create_default_context(cafile=str(CA_FILE))
           if CA_FILE.exists() else ssl.create_default_context())
    # Метка времени в адресе: раздача GitHub кэширует файлы на несколько минут,
    # а обновление обычно ставят сразу после выпуска.
    sep = "&" if "?" in url else "?"
    for attempt in range(tries):
        full = (url + sep + "t=" + str(int(time.time()))) if fresh else url
        req = urllib.request.Request(
            full, headers={"User-Agent": "TauTill/" + local_version()})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if not too_many(e):
                raise
            if attempt == tries - 1:
                raise OSError(
                    "GitHub временно ограничил загрузки с этого интернет-адреса. "
                    "Это проходит само. Нажмите ОБНОВИТЬ КАССУ ещё раз через "
                    "полчаса, касса пока продолжит работать как обычно.")
            wait = retry_wait(e, 3 * (attempt + 1))
            log("GitHub ответил 429, жду %d с и пробую снова" % wait)
            time.sleep(wait)


def remote_manifest(cfg):
    url = (cfg.get("update_url") or "").strip()
    if not url:
        raise OSError("адрес обновлений не настроен в settings.json")
    return json.loads(https_get(url).decode("utf-8"))


def backup_db():
    """Копия базы перед подменой программы.

    Новая версия может поменять устройство базы, и откат программы сам по себе
    базу назад не вернёт.  Копия стоит секунду и лежит там же, где ежедневные.
    """
    src = DATA / "store.db"
    if not src.exists():
        return None
    import sqlite3
    out = DATA / "backup"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / ("store-before-update-%s.db"
                  % datetime.now().strftime("%Y%m%d-%H%M%S"))
    con = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            con.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        con.close()
    log("копия базы перед обновлением: " + dest.name)
    return dest


def target_name(f):
    """Имя, под которым файл ложится в папку app."""
    return f.get("as") or f["path"].rsplit("/", 1)[-1]


def fetch_files(man, cfg, into):
    """Скачать файлы программы по одному, прямо из репозитория.

    Адрес, от которого считаются пути файлов, берётся из самого списка.  Если
    его там нет, остаётся папка, откуда пришёл сам список: version.json лежит
    в update/, а файлы программы на уровень выше, в корне репозитория.
    """
    base = man.get("base") or ""
    if not base and cfg:
        url = (cfg.get("update_url") or "").strip()
        if "/update/" in url:
            base = url.split("/update/")[0] + "/"
    for f in man["files"]:
        data = https_get(base + f["path"])
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise OSError("файл %s скачался повреждённым" % f["path"])
        (into / target_name(f)).write_bytes(data)


def fetch_archive(man, arc, into):
    """Забрать все файлы разом, одним архивом из выпуска на GitHub.

    Два запроса вместо девяти.  GitHub считает запросы с одного интернет-
    адреса, и в день с несколькими выпусками магазин упирался в 429 и не мог
    обновиться вовсе.  Раздача файлов из выпусков для того и сделана, а вот
    raw для неё не предназначен.

    Из архива берём ровно то, что перечислено в списке, по именам, и каждый
    файл сверяем отдельно.  Распаковывать архив целиком нельзя: имена внутри
    него это данные, пришедшие из сети, и такая распаковка позволила бы
    записать файл мимо папки обновления.
    """
    data = https_get(arc["url"], fresh=False)
    if hashlib.sha256(data).hexdigest() != arc.get("sha256"):
        raise OSError("архив обновления скачался повреждённым")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for f in man["files"]:
            name = target_name(f)
            blob = z.read(name)
            if hashlib.sha256(blob).hexdigest() != f["sha256"]:
                raise OSError("файл %s в архиве повреждён" % name)
            (into / name).write_bytes(blob)


def install_root():
    """Где касса стоит на самом деле, а не откуда её сейчас запустили.

    Обычно это одно и то же.  Но ярлыки на рабочем столе могли уехать на
    распакованный архив с флешки: переключатели автозапуска из архива считали
    папкой кассы ту, где лежат сами.  Тогда КАССА открывает копию с флешки, и
    чинить ярлыки по ROOT значит закрепить именно эту ошибку.  Поэтому ищем
    там же, где ищет установщик, и только если не нашли, остаёмся при своём.
    """
    for p in (Path(r"C:\TauTill"),
              Path(os.environ.get("LOCALAPPDATA") or "") / "TauTill"):
        try:
            if (p / "python" / "python.exe").exists():
                return p
        except OSError:
            pass
    return ROOT


def fix_shortcuts():
    """Перерисовать ярлыки на настоящую папку кассы.

    Делается после каждого удачного обновления, и это единственный способ
    починить их по сети: сами ярлыки, shortcuts.vbs и переключатели
    автозапуска лежат в корне папки кассы, а обновление подменяет только app.

    TAU_AUTOSTART намеренно убираем из окружения: в shortcuts.vbs это
    трёхзначная переменная, и её отсутствие означает "автозапуск оставить как
    есть".  Иначе починка значков молча выключала бы автозапуск, который в
    магазине уже включили.
    """
    if os.name != "nt":
        return
    root = install_root()
    if root != ROOT:
        log("касса запущена не из папки установки: %s вместо %s" % (ROOT, root))
    vbs = root / "shortcuts.vbs"
    if not vbs.exists():
        log("ярлыки не трогаю: нет %s" % vbs)
        return
    env = dict(os.environ)
    env["TAU_DEST"] = str(root)
    env.pop("TAU_AUTOSTART", None)
    try:
        r = subprocess.run(["cscript", "//nologo", str(vbs)],
                           env=env, capture_output=True, timeout=60)
        if r.returncode == 0:
            log("ярлыки перерисованы на %s" % root)
        else:
            log("ярлыки перерисовать не вышло, код %s" % r.returncode)
    except Exception as e:
        log("ярлыки перерисовать не вышло: %s" % e)


def guard_launcher(folder):
    """Убедиться, что новый launch.py вообще разбирается как программа.

    Это единственный файл, который проверка после подмены не достаёт.  Кассу
    поднимает и проверяет ещё старый launch.py, а новый впервые запустится
    только в следующий раз, когда касса будет включаться.  Значит сломанный
    launch.py откат не поймает: обновление отчитается об успехе, а наутро
    магазин останется без кассы, и починить это можно будет только приехав.

    compile() читает тот же Python, что стоит на кассе, поэтому заодно ловит
    синтаксис, которого на Windows 7 в 3.8 ещё нет.
    """
    f = folder / "launch.py"
    if not f.exists():
        return
    try:
        compile(f.read_text(encoding="utf-8"), "launch.py", "exec")
    except (SyntaxError, ValueError, UnicodeDecodeError) as e:
        raise OSError("новый launch.py не разбирается (%s), "
                      "обновление отменено" % e)


def download_update(man, cfg=None):
    """Скачать новую версию в отдельную папку и проверить каждый файл."""
    new = ROOT / "app.new"
    if new.exists():
        shutil.rmtree(new)
    new.mkdir(parents=True)

    arc = man.get("archive") or {}
    if arc.get("url"):
        try:
            fetch_archive(man, arc, new)
        except Exception as e:
            # Список уезжает на GitHub обычным push, а архив прикладывается к
            # выпуску отдельной командой.  Между этими двумя моментами архива
            # ещё нет, и попасть в эту щель проще, чем кажется.  Файлы по
            # одному лежат там же, где всегда, так что это не повод отменять
            # обновление.
            log("архив не взялся (%s), качаю файлы по одному" % e)
            fetch_files(man, cfg, new)
    else:
        fetch_files(man, cfg, new)

    guard_launcher(new)
    log("скачано файлов: %d" % len(man["files"]))
    return new


def swap(src, dest, keep):
    """Подменить папку app, отложив прежнюю под именем keep."""
    if keep.exists():
        shutil.rmtree(keep)
    dest.rename(keep)
    src.rename(dest)


def clear_flag():
    try:
        if UPDATE_FLAG.exists():
            UPDATE_FLAG.unlink()
    except OSError:
        pass


def do_update(cfg):
    """Обновить программу целиком, с откатом, если новая версия не заводится.

    Порядок здесь важнее красоты.  Сначала всё скачивается и проверяется, и
    только потом касса останавливается: если интернет отвалится на середине,
    магазин этого даже не заметит.  Старая папка не удаляется, а откладывается,
    и если новая версия не отвечает, возвращается на место.
    """
    clear_flag()
    here = local_version()

    try:
        man = remote_manifest(cfg)
    except Exception as e:
        log("обновление не проверено: %s" % e)
        box("Не удалось проверить обновления.\n\n%s\n\n"
            "Касса продолжит работать на версии %s." % (e, here))
        return False

    there = man.get("version", "0.0.0")
    if not newer(there, here):
        log("обновление не требуется: установлена %s, доступна %s"
            % (here, there))
        box("Обновление не требуется.\n\nУстановлена последняя версия: %s"
            % here, icon=0x40)
        return False

    log("обновление %s -> %s" % (here, there))
    try:
        new = download_update(man, cfg)
    except Exception as e:
        log("скачать обновление не удалось: %s" % e)
        box("Не удалось скачать обновление.\n\n%s\n\n"
            "Касса продолжит работать на версии %s." % (e, here))
        return False

    backup_db()
    stop_all(cfg)
    try:
        swap(new, ROOT / "app", ROOT / "app.prev")
    except OSError as e:
        log("подмена папки не удалась: %s" % e)
        box("Не удалось заменить программу.\n\n%s\n\n"
            "Касса осталась на версии %s." % (e, here))
        return False

    pid = start_server(cfg, "python.exe", quiet=True)
    if not pid:
        pid = start_server(cfg, "pythonw.exe", quiet=True)
    if pid:
        kill(pid, "сервер")
        log("обновление установлено: версия %s" % local_version())
        # Значки и пути в ярлыках могли уехать на копию с флешки.  Момент
        # после обновления самый подходящий: касса всё равно сейчас
        # перезапустится, и ярлык под пальцем окажется уже правильный.
        fix_shortcuts()
        return True

    # Новая версия не поднялась.  Возвращаем прежнюю и говорим об этом вслух.
    log("новая версия не запустилась, откатываюсь")
    try:
        swap(ROOT / "app.prev", ROOT / "app", ROOT / "app.bad")
    except OSError as e:
        box("Обновление не заработало, и вернуть старую версию не вышло.\n\n"
            "%s\n\nПозвоните тому, кто настраивает кассу." % e)
        return False
    box("Новая версия не заработала.\n\nКасса вернулась к версии %s "
        "и сейчас откроется.\nПродавать можно как обычно." % local_version())
    return True


# --- команды --------------------------------------------------------------
def stop_all(cfg):
    state = read_state()
    kill(state.get("chrome"), "браузер")
    kill(state.get("server"), "сервер")
    for _ in range(10):
        if not port_answers(cfg["port"], 0.2):
            break
        time.sleep(0.3)
    write_state()
    log("касса остановлена")


def supervise(server_pid, chrome_pid, cfg):
    """Пока открыт браузер, касса работает.

    Возвращает True, если сервер остановился ради обновления: тогда снаружи
    надо обновиться и подняться заново.

    Closing the browser is how the till is shut down from the screen, so when
    it goes the server goes with it.  The other direction matters too: if the
    server dies on its own the browser is left showing a page that can no
    longer sell anything.
    """
    log("касса работает, версия %s" % local_version())
    while True:
        time.sleep(1.0)
        if MINIMIZE_FLAG.exists():
            try:
                MINIMIZE_FLAG.unlink()
            except OSError:
                pass
            if alive(chrome_pid):
                log("сворачиваю браузер по запросу с экрана")
                minimize_chrome(chrome_pid)
        if not alive(chrome_pid):
            log("браузер закрыт")
            kill(server_pid, "сервер")
            write_state()
            return False
        if not alive(server_pid):
            log("сервер остановился сам")
            kill(chrome_pid, "браузер")
            write_state()
            if UPDATE_FLAG.exists():
                log("запрошено обновление с экрана")
                return True
            return False


def start(cfg):
    """Поднять кассу и следить за ней, пока её не закроют."""
    while True:
        state = read_state()
        server_pid = state.get("server")
        running = port_answers(cfg["port"])

        if running and alive(state.get("chrome")):
            log("касса уже работает, ничего не делаю")
            return 0

        if not running:
            server_pid = launch_server(cfg)
            if not server_pid:
                return 1
        else:
            # Сервер пережил случайно закрытый браузер.  Не трогаем его:
            # открытый день, корзина и нумерация чеков продолжаются с места.
            log("сервер уже работает, открываю только браузер")

        chrome_pid = start_chrome(cfg)
        if not chrome_pid:
            return 1

        write_state(server_pid, chrome_pid)
        if not supervise(server_pid, chrome_pid, cfg):
            return 0
        do_update(cfg)      # и по кругу: поднимаем кассу заново


def check(cfg):
    """Проверка компьютера, чтобы было что показать при разборе неполадок."""
    out = []

    def say(line=""):
        out.append(line)
        print(line)

    say("ПРОВЕРКА КАССЫ " + NAME.upper())
    say(datetime.now().strftime("%d.%m.%Y %H:%M"))
    say()
    say("Версия кассы: %s" % local_version())
    say("Python: %s" % sys.version.split()[0])
    # Если этих библиотек не окажется рядом с python.exe, Python на старой
    # Windows 7 не запустится вовсе, и разбираться придётся именно с них.
    ucrt = len(list(PYDIR.glob("api-ms-win-*.dll")))
    base = (PYDIR / "ucrtbase.dll").exists()
    say("Universal C Runtime рядом с Python: %s"
        % (f"{ucrt + base} файлов" if ucrt and base else "НЕТ, а должны быть"))
    say("Папка кассы: %s" % ROOT)
    say("База данных: %s (%s)" % (
        DATA / "store.db",
        "есть" if (DATA / "store.db").exists() else "НЕТ, будет создана пустая"))
    shown = dict(cfg)
    shown["owner_pin"] = "скрыт"
    say("Настройки: %s" % json.dumps(shown, ensure_ascii=False))
    say()

    chrome = find_chrome()
    say("Chrome: %s" % (chrome or "НЕ НАЙДЕН"))

    say()
    try:
        sys.path.insert(0, str(APP))
        import winprint
        names = winprint.printers()
        say("Принтеры в системе: %s" % (", ".join(names) or "ни одного"))
        try:
            say("Печатать будем на: %s" % winprint.resolve(cfg["printer"]))
        except OSError as e:
            say("ВНИМАНИЕ: %s" % e)
    except Exception as e:
        say("Не удалось получить список принтеров: %s" % e)

    say()
    say("Сертификаты для обновлений: %s"
        % ("свои, %d КБ" % (CA_FILE.stat().st_size // 1024)
           if CA_FILE.exists() else "НЕТ, будут взяты системные"))
    if not (cfg.get("update_url") or "").strip():
        say("Обновления: адрес не настроен")
    else:
        try:
            man = remote_manifest(cfg)
            say("Обновления: доступна версия %s" % man.get("version"))
        except Exception as e:
            say("Обновления: проверить не вышло, %s" % e)

    say()
    if port_answers(cfg["port"], 0.3):
        say("Порт %s занят: касса уже запущена." % cfg["port"])
    else:
        say("Порт %s свободен, пробую запустить сервер..." % cfg["port"])
        pid = launch_server(cfg)
        if pid:
            say("Сервер запустился и ответил. Останавливаю обратно.")
            kill(pid, "сервер")
        else:
            say("СЕРВЕР НЕ ЗАПУСТИЛСЯ, смотрите журнал ниже.")

    say()
    say("--- последние строки журнала ---")
    say(tail(log_file(), 25))

    # Имя файла латиницей: его открывает .bat, а он живёт в кодировке 866,
    # и путь с русскими буквами там лишний повод для беды.
    report = LOGS / "tau-check.txt"
    try:
        report.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(out).encode("utf-8"))
        print("\nОтчёт: %s" % report)
    except OSError as e:
        print("не удалось записать отчёт: %s" % e)
    return 0


def main():
    args = sys.argv[1:]
    cfg = settings()
    trim_logs()

    if "--stop" in args:
        stop_all(cfg)
        return 0
    if "--check" in args:
        return check(cfg)
    if "--update" in args:
        log("обновление по значку с рабочего стола")
        stop_all(cfg)
        do_update(cfg)
        return start(cfg)
    if "--restart" in args:
        log("перезапуск по команде с рабочего стола")
        stop_all(cfg)
        time.sleep(1.0)
        return start(cfg)
    if "--boot" in args:
        # При входе в систему Windows ещё занята собственным запуском, и Chrome,
        # стартовавший в эту суету, поднимается медленно или не поднимается.
        delay = int(cfg["boot_delay"])
        log(f"автозапуск, жду {delay} секунд")
        time.sleep(delay)
    return start(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                      # noqa: BLE001
        # Трассировка в скрытую консоль это трассировка, которую никто никогда
        # не увидит.
        import traceback
        log("СБОЙ ЗАПУСКА:\n" + traceback.format_exc())
        box("Не удалось запустить кассу.\n\n%s\n\nЖурнал: %s" % (e, log_file()))
        sys.exit(1)
