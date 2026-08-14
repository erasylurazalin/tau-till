"""Сборка кассы Tau Till для кассового компьютера.

Запускается на машине разработки (Linux), собирает папку и zip, которые
кладутся на флешку и разворачиваются на кассе одним двойным щелчком.

    python3 pack/build.py

What comes out is a self-contained folder: the shop's Python, the shop's data
and the shop's program, so nothing has to be installed on a Windows 7 machine
that has no Python and must not be experimented on.

Three things here are not cosmetic and will break the till if changed:

  * .bat files are written in CP866 with CRLF.  The Windows console reads a
    batch file in the OEM codepage, and a UTF-8 one turns every Russian word
    into garbage in the middle of an installation.
  * shortcuts.vbs is written in UTF-16.  Windows Script Host reads a script
    file as ANSI unless it starts with a byte order mark, and the shortcut
    names are Russian.
  * python38._pth gains the app folder.  The embeddable Python ignores
    everything outside that file, so without the line the server cannot import
    a single one of its own modules.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path

import peimports

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WIN = HERE / "win"
CACHE = HERE / "cache"
OUT = HERE / "dist"

# Последняя сборка Python, работающая на Windows 7.  Это 3.8, а не 3.9:
# python39.dll требует api-ms-win-core-path-l1-1-0.dll, набора, которого в
# Windows 7 нет, и касса в магазине отказалась запускаться именно из-за этого.
# У 3.8 таких зависимостей нет ни одной, и проверяет это check_win7() ниже.
# Контрольная сумма взята со страницы выпуска на python.org и сверяется при
# каждой сборке: подменённый или недокачанный архив не должен уехать в магазин.
PY_VERSION = "3.8.10"
PY_TAG = "".join(PY_VERSION.split(".")[:2])      # 3.8.10 -> 38
PY_ZIP = f"python-{PY_VERSION}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_ZIP}"
PY_MD5 = "3acb1d7d9bde5a79f840167b166bb633"

# Набор Universal C Runtime из установщика самого Python той же версии.
# Лежит на python.org рядом с установщиком, отдельным msi.
UCRT_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/amd64/ucrt.msi"
UCRT_SHA = "40ebc49d8861b3292ea0e1c0ea289db1e4777d5559aa12db96b7ee375ef2adfe"

# Запасной путь, если библиотеки рядом с программой почему-то не помогут:
# то самое обновление Windows, но уже с правами администратора и перезагрузкой.
KB_FILE = "Windows6.1-KB2999226-x64.msu"

# Что из проекта уезжает на кассу.  Списком, а не «всё подряд»: в папке
# проекта лежит и рабочая база, и выгрузки, и мусор от старых импортов.
APP_FILES = ["server.py", "db.py", "printer.py", "winprint.py", "xlsx.py",
             "index.html", "version.py"]

# Настройки, которые уезжают на кассу.  Установщик никогда не перезаписывает
# тот settings.json, что уже лежит на кассе, так что подстройки на месте
# переживают любое дополнение.
#
# Настройки конкретного магазина в репозитории не лежат: репозиторий открытый,
# а здесь имя хозяйки и её пин от кассы.  Реальные значения живут рядом со
# сборщиком в pack/shop.json, который в git не попадает.  Без этого файла
# сборка получается обезличенной и годится кому угодно.
SHOP_DEFAULTS = {
    "shop": "МАГАЗИН",
    "printer": "XP-58",
    "port": 8000,
    "boot_delay": 12,
    "owner": "ХОЗЯИН",
    "owner_pin": "0000",
}

README = """КАССА TAU TILL

КАК УСТАНОВИТЬ

  1. Включите чековый принтер и убедитесь, что он подключён кабелем.
  2. Откройте эту папку на кассовом компьютере.
  3. Дважды щёлкните «УСТАНОВИТЬ КАССУ.bat».
  4. Подождите. Ничего нажимать не нужно, установка идёт сама.
  5. В конце откроется отчёт проверки. Посмотрите в нём две строки:
     «Печатать будем на» и «Chrome». Если там написано, что чего-то
     нет, покажите этот отчёт тому, кто настраивает кассу.

ЧТО ПОЯВИТСЯ НА РАБОЧЕМ СТОЛЕ

  КАССА                 запускает кассу
  ПЕРЕЗАПУСТИТЬ КАССУ   закрывает и снова открывает, если что-то подвисло
  ОБНОВИТЬ КАССУ        скачивает и ставит новую версию программы
  ОСТАНОВИТЬ КАССУ      закрывает кассу совсем

АВТОМАТИЧЕСКИЙ ЗАПУСК

  После установки касса сама при включении компьютера НЕ поднимается:
  её открывает значок КАССА. Так задумано, пока магазин работает на
  старой программе и Tau Till стоит рядом для проверки.

  Когда решите работать на ней по-настоящему, откройте папку кассы
  C:\\TauTill и запустите «АВТОЗАПУСК ВКЛЮЧИТЬ.bat». Вернуть всё назад
  можно там же файлом «АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat».

КАК ВЫКЛЮЧИТЬ КАССУ С САМОГО ЭКРАНА

  АДМИН, пролистать вниз, «ВЫКЛЮЧИТЬ КАССУ».
  Открытый операционный день при этом не пропадает.

ГДЕ ЧТО ЛЕЖИТ ПОСЛЕ УСТАНОВКИ

  C:\\TauTill\\data\\store.db      вся база магазина, один файл
  C:\\TauTill\\data\\backup\\      копии базы, по одной за каждый закрытый день
  C:\\TauTill\\logs\\             журналы запуска, если что-то пошло не так
  C:\\TauTill\\settings.json      имя принтера и название магазина для чека

  Если C:\\TauTill нет, значит установщику не хватило прав на диск C
  и касса встала в папку пользователя. Точный путь написан в отчёте.

ЕСЛИ УСТАНОВКА СКАЗАЛА, ЧТО PYTHON НЕ ЗАПУСКАЕТСЯ

  Запустите «ЕСЛИ КАССА НЕ СТАВИТСЯ.bat» из этой же папки,
  перезагрузите компьютер и повторите установку кассы.
  Это редкий случай: обычно нужные библиотеки уже лежат
  внутри сборки и ставить в Windows ничего не надо.

ЕСЛИ ЧТО-ТО НЕ РАБОТАЕТ

  Дважды щёлкните «ПРОВЕРКА КАССЫ.bat» в папке кассы. Он соберёт
  отчёт и откроет его в Блокноте. Этот отчёт и нужен для разбора.
"""


PATCH_README = """ДОПОЛНЕНИЕ КАССЫ TAU TILL

Это НЕ полная установка. Python сюда не входит: он уже лежит
на кассе с прошлой попытки. Поэтому файл маленький и проходит
даже через капризную флешку.

ЧТО ДЕЛАТЬ

  1. Скопируйте эту папку на рабочий стол кассы.
  2. Запустите из неё «ДОПОЛНИТЬ КАССУ.bat».
  3. Дождитесь отчёта в Блокноте.

Если программа скажет, что касса на компьютере не найдена,
значит нужна полная установка из полного архива.

Автоматический запуск дополнение не трогает: был включён, останется
включённым. Переключатели лежат в папке кассы и называются
«АВТОЗАПУСК ВКЛЮЧИТЬ.bat» и «АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat».
"""


def say(msg):
    print("  " + msg)


# --- откуда касса будет брать обновления ----------------------------------
def github_repo():
    """Владелец и название репозитория из настроек git, если они уже есть.

    Пока origin не заведён, то же самое можно сказать прямо:
        TAU_REPO=имя/tau-till python3 pack/build.py
    """
    forced = os.environ.get("TAU_REPO", "").strip()
    if forced and "/" in forced:
        owner, name = forced.split("/", 1)
        return owner, name.rstrip(".git")
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", out)
    return (m.group(1), m.group(2)) if m else None


def raw_base():
    """Адрес, по которому касса читает файлы прямо из репозитория."""
    repo = github_repo()
    return (f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/main/"
            if repo else "")


def settings_json():
    base = raw_base()
    if not base:
        say("репозиторий ещё не привязан, обновления в настройках выключены")
    cfg = dict(SHOP_DEFAULTS)
    local = HERE / "shop.json"
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8")))
        say("настройки магазина взяты из pack/shop.json")
    else:
        say("pack/shop.json нет, сборка выйдет обезличенной:"
            " имя хозяина ХОЗЯИН, пин 0000")
    cfg["update_url"] = base + "update/version.json" if base else ""
    return json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"


def certificates(dest):
    """Свой список корневых сертификатов для проверки обновлений.

    Windows 7, которую годами не обновляли, могла не получить новые корневые
    сертификаты, и тогда совершенно исправный GitHub покажется кассе
    подозрительным.  Со своим списком обновления не зависят от состояния
    системы вообще.
    """
    src = Path("/etc/ssl/certs/ca-certificates.crt")
    if not src.exists():
        say("список сертификатов не найден, обновления будут полагаться"
            " на системный")
        return
    shutil.copy(src, dest / "cacert.pem")
    count = src.read_text(errors="replace").count("BEGIN CERTIFICATE")
    say(f"сертификаты: {count} корневых, {src.stat().st_size // 1024} КБ")


# --- Python ---------------------------------------------------------------
def python_zip():
    """Скачать встраиваемый Python (один раз) и проверить контрольную сумму."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / PY_ZIP
    if not path.exists():
        say(f"скачиваю {PY_ZIP} ...")
        with urllib.request.urlopen(PY_URL, timeout=120) as r:
            path.write_bytes(r.read())
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    if digest != PY_MD5:
        path.unlink()
        raise SystemExit(f"контрольная сумма {PY_ZIP} не совпала: {digest}")
    say(f"{PY_ZIP}: контрольная сумма совпала")
    return path


def ucrt_files():
    """Библиотеки Universal C Runtime, которые кладутся рядом с python.exe.

    Windows 7, не получавшая обновление KB2999226, не умеет запускать ничего
    собранного новыми компиляторами, и Python из такой системы просто не
    стартует.  Кассовый компьютер оказался как раз таким.

    Ставить обновление в чужую Windows по телефону, через права
    администратора и перезагрузку, плохая затея, а Microsoft разрешает другой
    путь: положить эти библиотеки в папку самой программы.  Так делает и
    установщик самого Python, откуда набор и взят (ucrt.msi с python.org,
    ровно для версии 3.9.13).  Имена внутри msi записаны через подчёркивания,
    а загрузчик Windows ищет их через дефис, поэтому файлы переименовываются.
    """
    out = CACHE / f"ucrt-{PY_VERSION}"
    dlls = sorted(out.glob("*.dll"))
    if len(dlls) >= 40:
        return dlls

    out.mkdir(parents=True, exist_ok=True)
    msi = CACHE / f"ucrt-{PY_VERSION}.msi"
    if not msi.exists():
        say("скачиваю ucrt.msi ...")
        with urllib.request.urlopen(UCRT_URL, timeout=120) as r:
            msi.write_bytes(r.read())
    digest = hashlib.sha256(msi.read_bytes()).hexdigest()
    if digest != UCRT_SHA:
        msi.unlink()
        raise SystemExit(f"контрольная сумма ucrt.msi не совпала: {digest}")

    tmp = CACHE / "ucrt-tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    if shutil.which("7z") is None:
        raise SystemExit("для распаковки ucrt.msi нужен 7z (pacman -S p7zip)")
    subprocess.run(["7z", "x", "-y", "-o" + str(tmp), str(msi)],
                   check=True, stdout=subprocess.DEVNULL)
    for f in tmp.glob("InstallDirectory_*"):
        name = f.name[len("InstallDirectory_"):].replace("_", "-")
        if name.startswith("ucrtbase"):
            name = "ucrtbase.dll"
        shutil.copy(f, out / name)
    shutil.rmtree(tmp)
    return sorted(out.glob("*.dll"))


def unpack_python(dest):
    with zipfile.ZipFile(python_zip()) as z:
        z.extractall(dest)
    # Встраиваемый Python берёт пути только отсюда.  Без ..\\app сервер не
    # найдёт ни db.py, ни printer.py и не запустится вообще.
    (dest / f"python{PY_TAG}._pth").write_bytes(
        f"python{PY_TAG}.zip\r\n.\r\n..\\app\r\n".encode())
    # Рядом с python.exe, а не куда-нибудь ещё: именно свою папку загрузчик
    # Windows просматривает первой.
    dlls = ucrt_files()
    for f in dlls:
        shutil.copy(f, dest / f.name)
    # Ни один файл не должен требовать того, чего в Windows 7 нет.  Проверка
    # стоит здесь, а не в голове: именно эта ошибка уже съездила в магазин.
    bad = peimports.too_new_for_win7(dest)
    if bad:
        raise SystemExit(
            "Python требует библиотек, которых нет в Windows 7:\n"
            + "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items()))
    say(f"python {PY_VERSION}: {len(list(dest.iterdir()))} файлов, из них"
        f" {len(dlls)} библиотек Universal C Runtime")
    say("проверка на Windows 7: ни одной лишней зависимости")


# --- значки ---------------------------------------------------------------
def icons(dest):
    """Три значка: запуск, перезапуск, остановка.

    Разного цвета и с разными знаками, чтобы их нельзя было перепутать
    мельком: зелёный тенге, синяя стрелка по кругу, красный квадрат.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("Pillow не установлен, значки пропущены")
        return

    def base(color):
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 252, 252], radius=44, fill=color)
        return img, d

    white = (255, 255, 255, 255)

    # КАССА: знак тенге, две черты и ножка.
    img, d = base((31, 157, 85, 255))
    d.rectangle([62, 66, 194, 88], fill=white)
    d.rectangle([62, 104, 194, 126], fill=white)
    d.rectangle([117, 104, 139, 200], fill=white)
    img.save(dest / "kassa.ico", sizes=[(16, 16), (32, 32), (48, 48),
                                        (64, 64), (128, 128), (256, 256)])

    # ПЕРЕЗАПУСТИТЬ: стрелка по кругу.
    img, d = base((47, 158, 224, 255))
    d.arc([58, 58, 198, 198], start=25, end=320, fill=white, width=26)
    d.polygon([(196, 96), (152, 96), (176, 44)], fill=white)
    img.save(dest / "restart.ico", sizes=[(16, 16), (32, 32), (48, 48),
                                          (64, 64), (128, 128), (256, 256)])

    # ОБНОВИТЬ: стрелка вниз, как у всякой загрузки.
    img, d = base((0, 166, 196, 255))
    d.rectangle([115, 52, 141, 150], fill=white)
    d.polygon([(78, 130), (178, 130), (128, 196)], fill=white)
    d.rounded_rectangle([64, 210, 192, 226], radius=8, fill=white)
    img.save(dest / "update.ico", sizes=[(16, 16), (32, 32), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])

    # ОСТАНОВИТЬ: квадрат, как на кнопке остановки.
    img, d = base((226, 88, 108, 255))
    d.rounded_rectangle([80, 80, 176, 176], radius=10, fill=white)
    img.save(dest / "stop.ico", sizes=[(16, 16), (32, 32), (48, 48),
                                       (64, 64), (128, 128), (256, 256)])
    say("значки: kassa.ico, restart.ico, update.ico, stop.ico")


# --- перекодировка --------------------------------------------------------
def write_bat(src, dest):
    """UTF-8 из проекта в CP866 с CRLF для консоли Windows."""
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    try:
        data = text.replace("\n", "\r\n").encode("cp866")
    except UnicodeEncodeError:
        # В CP866 нет ни ёлочек, ни длинного тире, ни многоточия одним знаком.
        # Сказать об этом прямо дешевле, чем разбирать трассировку кодека.
        bad = sorted({c for c in text if c.encode("cp866", "ignore") == b""})
        raise SystemExit(
            f"{src.name}: в кодировке консоли Windows нет знаков "
            + " ".join(f"«{c}»" for c in bad)
            + ". Замените их обычными кавычками или дефисами.")
    dest.write_bytes(data)


def write_vbs(src, dest):
    """UTF-8 из проекта в UTF-16 с меткой порядка байтов для WSH."""
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    dest.write_bytes(text.replace("\n", "\r\n").encode("utf-16"))


def write_txt(src_text, dest):
    """Текст для Блокнота: UTF-8 с меткой, иначе Windows 7 покажет мусор."""
    dest.write_bytes(b"\xef\xbb\xbf"
                     + src_text.replace("\n", "\r\n").encode("utf-8"))


# --- данные ---------------------------------------------------------------
def clean_data(dbfile):
    """Убрать из копии базы всё, что наработано при разработке.

    Товары, штрихкоды и кассиры едут в магазин, а пробные продажи и незакрытый
    операционный день не едут: касса должна открыться в первый рабочий день с
    чистого листа, а не унаследовать чей-то чужой день с чеком на 87 тенге.

    The ledger goes with them.  products.stock is a cache of stock_moves, so
    wiping the moves under a counted balance would leave the two disagreeing
    forever; where a balance exists it is rewritten as a single opening entry
    that explains it.
    """
    import sqlite3
    con = sqlite3.connect(dbfile)
    con.row_factory = sqlite3.Row
    counted = con.execute(
        "SELECT id, stock FROM products WHERE stock IS NOT NULL").fetchall()
    removed = {}
    for table in ("receipt_items", "receipts", "shifts", "stock_moves"):
        removed[table] = con.execute(
            f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        con.execute(f"DELETE FROM {table}")
    now = datetime.now().isoformat(timespec="seconds")
    for p in counted:
        con.execute(
            "INSERT INTO stock_moves (ts, product_id, delta, kind, note)"
            " VALUES (?,?,?,'adjust','начальный остаток')",
            (now, p["id"], p["stock"]))
    con.commit()
    con.execute("VACUUM")
    con.close()
    gone = ", ".join(f"{k}: {v}" for k, v in removed.items() if v)
    say("база очищена от пробных данных" + (f" ({gone})" if gone else ""))
    if counted:
        say(f"остатки сохранены как начальные: {len(counted)} товаров")


# --- сборка ---------------------------------------------------------------
def build():
    name = "TauTill-" + date.today().strftime("%Y-%m-%d")
    dest = OUT / name
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "app").mkdir(parents=True)
    (dest / "icons").mkdir()
    (dest / "data").mkdir()

    say(f"собираю {dest}")

    for f in APP_FILES:
        shutil.copy(PROJECT / f, dest / "app" / f)
    shutil.copy(WIN / "launch.py", dest / "app" / "launch.py")
    say(f"программа: {len(APP_FILES) + 1} файлов")

    unpack_python(dest / "python")
    icons(dest / "icons")
    certificates(dest)

    write_bat(WIN / "install.bat", dest / "УСТАНОВИТЬ КАССУ.bat")
    write_bat(WIN / "check.bat", dest / "ПРОВЕРКА КАССЫ.bat")
    write_bat(WIN / "kbfix.bat", dest / "ЕСЛИ КАССА НЕ СТАВИТСЯ.bat")
    write_bat(WIN / "autostart-on.bat", dest / "АВТОЗАПУСК ВКЛЮЧИТЬ.bat")
    write_bat(WIN / "autostart-off.bat", dest / "АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat")
    shutil.copy(CACHE / KB_FILE, dest / KB_FILE)
    write_vbs(WIN / "shortcuts.vbs", dest / "shortcuts.vbs")
    write_txt(README, dest / "ЧИТАТЬ ПЕРВЫМ.txt")
    (dest / "settings.json").write_text(settings_json(), encoding="utf-8")

    # Магазин начинает работу со своим товаром, а не с пустой базой.  Копия,
    # а не оригинал: касса на кассовом компьютере живёт своей жизнью.
    live = PROJECT / "store.db"
    if live.exists():
        shutil.copy(live, dest / "data" / "store.db")
        clean_data(dest / "data" / "store.db")
        size = (dest / "data" / "store.db").stat().st_size // 1024
        say(f"база товаров: {size} КБ")
    else:
        say("рабочей базы нет, касса создаст пустую при первом запуске")

    archive = shutil.make_archive(str(OUT / name), "zip", root_dir=OUT,
                                  base_dir=name)
    # Контрольная сумма рядом с архивом.  Флешка, вынутая до того, как Linux
    # успел дописать данные, отдаёт битую копию молча, и проверить это надо
    # до поездки в магазин, а не на месте:
    #     cd <флешка> && sha256sum -c TauTill-....zip.sha256
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    sums = Path(archive + ".sha256")
    sums.write_text(f"{digest}  {Path(archive).name}\n")

    size = Path(archive).stat().st_size / 1024 / 1024
    say(f"готово: {archive} ({size:.1f} МБ)")
    say(f"контрольная сумма: {sums.name}")
    return dest, Path(archive)


def build_patch():
    """Маленькая сборка для кассы, где Python уже стоит.

    Флешка в магазине оказалась ненадёжной, и одиннадцать мегабайт с неё
    прочитать не удалось.  Питон уже лежит на кассе с прошлой попытки, а всё
    остальное вместе весит около двух мегабайт, и это единственное, что
    отличает нерабочую установку от рабочей.
    """
    name = "TauTill-FIX-" + date.today().strftime("%Y-%m-%d")
    dest = OUT / name
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "app").mkdir(parents=True)
    (dest / "python").mkdir()
    (dest / "icons").mkdir()

    for f in APP_FILES:
        shutil.copy(PROJECT / f, dest / "app" / f)
    shutil.copy(WIN / "launch.py", dest / "app" / "launch.py")
    for f in ucrt_files():
        shutil.copy(f, dest / "python" / f.name)
    icons(dest / "icons")
    certificates(dest)
    write_bat(WIN / "patch.bat", dest / "ДОПОЛНИТЬ КАССУ.bat")
    write_bat(WIN / "check.bat", dest / "ПРОВЕРКА КАССЫ.bat")
    write_bat(WIN / "autostart-on.bat", dest / "АВТОЗАПУСК ВКЛЮЧИТЬ.bat")
    write_bat(WIN / "autostart-off.bat", dest / "АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat")
    write_vbs(WIN / "shortcuts.vbs", dest / "shortcuts.vbs")
    write_txt(PATCH_README, dest / "ЧИТАТЬ ПЕРВЫМ.txt")
    (dest / "settings.json").write_text(settings_json(), encoding="utf-8")

    archive = shutil.make_archive(str(OUT / name), "zip", root_dir=OUT,
                                  base_dir=name)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(f"{digest}  {Path(archive).name}\n")
    size = Path(archive).stat().st_size / 1024 / 1024
    say(f"дополнение: {archive} ({size:.1f} МБ)")
    return dest, Path(archive)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    build()
    build_patch()
