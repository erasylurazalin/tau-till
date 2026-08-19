"""Выпуск новой версии Tau Till.

    python3 pack/release.py 0.5.1 -m "чинит печать копии чека"
    python3 pack/release.py                 # без номера: только пересобрать список

Что делает: проставляет номер версии, пересчитывает контрольные суммы всех
файлов программы и кладёт их в update/version.json.  Дальше печатает точные
команды git и на этом останавливается.  Коммит, метку и push я делаю сам,
прочитав git status глазами.

Коммит выпуска должен быть один и содержать ровно файлы программы вместе с
пересчитанным списком.  Если список уедет на GitHub отдельно от файлов, для
которых он посчитан, касса скачает их, не сойдётся контрольная сумма, и
обновление откажется ставиться.  Поэтому в команде git add перечислены именно
эти файлы и никакие другие, а если в дереве есть посторонние правки, скрипт
ничего не трогает и говорит разобраться с ними сначала.

Файлы программы едут на кассу архивом, приложенным к выпуску GitHub, и это
стоит отдельного шага публикации.  Раньше касса скачивала каждый файл по
прямой ссылке на репозиторий, и обычный git push был выпуском целиком.  Так
проще, но с raw.githubusercontent.com это девять запросов на одно обновление,
GitHub считает запросы с одного интернет-адреса, и в день с несколькими
выпусками магазин упёрся в 429 и перестал обновляться вовсе.  С архивом
запросов два: список и архив.  Раздача файлов из выпусков ровно для этого и
сделана, а raw для неё не предназначен.

Сам список остаётся там же, на raw, и это не случайность: адрес, по которому
касса его спрашивает, записан в settings.json рядом с папкой app, а обновление
подменяет только саму папку.  Значит адрес списка сменить по сети нельзя, а
всё, на что список показывает, можно.

Каждый файл в списке идёт со своей контрольной суммой, поэтому недокачанный
или подменённый файл касса не поставит.  А раз список полный, обновление
подменяет папку программы целиком, и файлы, которых в новой версии нет,
исчезают, вместо того чтобы тихо остаться от прошлой.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
UPDATE = PROJECT / "update"

# Имя архива с файлами программы.  Номер версии в имени не для красоты:
# ссылка на файл выпуска включает и метку, и имя, так что одно имя на все
# выпуски означало бы одну ссылку на разное содержимое.
ARCHIVE_NAME = "tautill-update-%s.zip"

# Что именно составляет программу на кассе.  Слева путь в репозитории, справа
# имя, под которым файл ложится в папку app.
FILES = [
    ("server.py", None),
    ("db.py", None),
    ("printer.py", None),
    ("winprint.py", None),
    ("xlsx.py", None),
    ("version.py", None),
    ("index.html", None),
    ("pack/win/launch.py", "launch.py"),
]

# Всё, что должно попасть в коммит выпуска: файлы программы, пересчитанный
# для них список и README, в котором номер тоже проставлен.  Ровно этот
# перечень уходит в git add, чтобы вместе с версией случайно не уехало то,
# что я в тот день ещё дописывал.
RELEASE_PATHS = ([path for path, _ in FILES]
                 + ["update/version.json", "README.md"])


def say(msg):
    print("  " + msg)


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(PROJECT)] + list(args),
                          capture_output=True, text=True, check=check)


def repo_slug():
    """Владелец и имя репозитория на GitHub.

    Пока origin не заведён, то же самое можно сказать прямо:
        TAU_REPO=имя/tau-till python3 pack/release.py
    """
    forced = os.environ.get("TAU_REPO", "").strip()
    if forced and "/" in forced:
        owner, name = forced.split("/", 1)
    else:
        try:
            url = git("remote", "get-url", "origin").stdout.strip()
        except subprocess.CalledProcessError:
            return None
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
        if not m:
            return None
        owner, name = m.group(1), m.group(2)
    return owner, re.sub(r"\.git$", "", name.strip("/"))


def repo_base():
    """Адрес, по которому лежит сам список обновления и файлы поштучно."""
    slug = repo_slug()
    if not slug:
        return None
    return f"https://raw.githubusercontent.com/{slug[0]}/{slug[1]}/main/"


def release_base(number):
    """Адрес выпуска, к которому приложен архив с программой.

    Ссылка привязана к метке версии, поэтому она неизменна: один номер это
    навсегда одно содержимое, и кассе не нужно обходить кэш при скачивании.
    """
    slug = repo_slug()
    if not slug:
        return None
    return (f"https://github.com/{slug[0]}/{slug[1]}"
            f"/releases/download/v{number}/")


def build_archive(number):
    """Собрать архив с файлами программы, который приложим к выпуску.

    Внутри архива всё лежит плоско, под теми именами, под которыми файлы
    ложатся в папку app: касса читает их оттуда по именам из списка, а не
    распаковывает архив целиком.
    """
    dist = PROJECT / "pack" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / (ARCHIVE_NAME % number)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, alias in FILES:
            z.write(PROJECT / src, alias or src.rsplit("/", 1)[-1])
    return path


def set_version(number):
    """Проставить номер в version.py, единственное место, где он живёт.

    Заодно подтягивается пример в подсказке к label().  Сам ярлык на экране
    собирается из VERSION и не устаревает никогда, но пример рядом с ним
    списан руками, и прошлый номер в нём сбивает с толку того, кто открыл файл
    посмотреть, как эта надпись выглядит.
    """
    path = PROJECT / "version.py"
    text = path.read_text(encoding="utf-8")
    new = re.sub(r'VERSION = "[^"]+"', f'VERSION = "{number}"', text, count=1)
    if new == text and f'VERSION = "{number}"' not in text:
        raise SystemExit("не нашёл строку VERSION в version.py")

    name = re.search(r'^NAME = "([^"]+)"', new, re.M)
    stage = re.search(r'^STAGE = "([^"]*)"', new, re.M)
    if name:
        # Ровно то, что вернёт label(): имя, номер и, если она есть, приставка.
        sample = name.group(1) + " " + number
        if stage and stage.group(1):
            sample += " " + stage.group(1)
        new = re.sub("«%s[^»]*»" % re.escape(name.group(1)),
                     "«%s»" % sample, new)
    path.write_text(new, encoding="utf-8")
    return number


def sync_readme(number):
    """Подтянуть номер в строке Status: README.md.

    README не едет на кассу, но лежит на GitHub, и версия в нём расходится с
    меткой выпуска ровно до тех пор, пока кто-нибудь не заметит.  Трогается
    только сам номер: слово рядом с ним написано по-английски и меняться не
    должно.
    """
    path = PROJECT / "README.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"(?m)^(\*\*Status:\*\*\s*v)\d+(?:\.\d+)*",
                 r"\g<1>" + number, text, count=1)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def current_version():
    text = (PROJECT / "version.py").read_text(encoding="utf-8")
    return re.search(r'VERSION = "([^"]+)"', text).group(1)


def manifest(number, notes, archive=None):
    base = repo_base()
    if not base:
        say("репозиторий пока не привязан к GitHub, адрес в списке будет пустым")
    files = []
    for path, alias in FILES:
        data = (PROJECT / path).read_bytes()
        entry = {"path": path,
                 "sha256": hashlib.sha256(data).hexdigest(),
                 "size": len(data)}
        if alias:
            entry["as"] = alias
        files.append(entry)
    man = {
        "version": number,
        "released": date.today().isoformat(),
        "notes": notes or "",
        "base": base or "",
        "files": files,
    }
    # Список files остаётся и при архиве.  По нему касса сверяет каждый файл
    # после распаковки, и по нему же скачивает поштучно та касса, которая ещё
    # не обновилась до версии, умеющей брать архив.
    if archive:
        man["archive"] = archive
    return man


def stray_changes():
    """Что в дереве изменено помимо самого выпуска.

    Возвращает два списка: правки в файлах, за которыми git следит, и файлы,
    про которые он ещё не знает.  Первые выпуску мешают, вторые нет: в git add
    они всё равно не попадут, но сказать о них стоит.

    Читается с -z, иначе git берёт в кавычки и экранирует всё, что не ASCII,
    а в этом проекте так выглядит половина имён.
    """
    changed, untracked = [], []
    items = git("status", "--porcelain", "-z").stdout.split("\0")
    i = 0
    while i < len(items):
        record = items[i]
        i += 1
        if not record:
            continue
        code, path = record[:2], record[3:]
        if code[0] in "RC":
            i += 1   # за переименованием отдельным полем идёт прежнее имя
        if code == "??":
            untracked.append(path)
        elif path not in RELEASE_PATHS:
            changed.append(path)
    return sorted(changed), sorted(untracked)


def main():
    ap = argparse.ArgumentParser(description="выпуск новой версии Tau Till")
    ap.add_argument("version", nargs="?", help="например 0.5.1")
    ap.add_argument("-m", "--notes", default="", help="что изменилось")
    args = ap.parse_args()

    have_git = (PROJECT / ".git").exists()
    changed, untracked = stray_changes() if have_git else ([], [])
    if changed:
        say("в дереве есть правки, к выпуску не относящиеся:")
        for path in changed:
            say("    " + path)
        say("их нужно сначала закоммитить отдельно или отложить через")
        say("git stash, иначе номер версии уедет непонятно с чем")
        return 1

    # Номер проставляется даже когда он не менялся: заодно подтягиваются
    # места, где он написан словами, а не собирается из VERSION.
    number = set_version(args.version or current_version())
    say(f"версия: {number}")
    if sync_readme(number):
        say("номер в README.md подтянут")

    archive_path = build_archive(number)
    archive_url = release_base(number)
    archive = None
    if archive_url:
        blob = archive_path.read_bytes()
        archive = {"url": archive_url + archive_path.name,
                   "sha256": hashlib.sha256(blob).hexdigest(),
                   "size": len(blob)}

    UPDATE.mkdir(exist_ok=True)
    man = manifest(number, args.notes, archive)
    (UPDATE / "version.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(f"список обновления: {len(man['files'])} файлов, "
        f"{sum(f['size'] for f in man['files']) // 1024} КБ")
    if archive:
        say(f"архив для выпуска: {archive_path.name}, "
            f"{archive['size'] // 1024} КБ")
    if man["base"]:
        say("поштучно файлы лежат здесь: " + man["base"])

    if not have_git:
        say("git в проекте ещё не заведён, команды выпуска печатать не из чего")
        return 0
    if untracked:
        say("git пока не знает про эти файлы, в выпуск они не пойдут:")
        for path in untracked:
            say("    " + path)

    message = f"Версия {number}" + (f": {args.notes}" if args.notes else "")
    tag = "v" + number
    print()
    print("  Дальше руками, прочитав сначала git status:")
    print()
    print("      git status -sb")
    print("      git add " + " ".join(RELEASE_PATHS))
    print("      git commit -m " + shlex.quote(message))
    if git("tag", "-l", tag).stdout.strip():
        print(f"      # метка {tag} уже стоит, второй раз не нужно")
    else:
        print(f"      git tag -a {tag} -m " + shlex.quote(message))
    print("      git push --follow-tags")
    if archive:
        rel = archive_path.relative_to(PROJECT)
        print()
        print("  Потом приложить архив к выпуску, уже после push:")
        print()
        print(f"      gh release create {tag} {shlex.quote(str(rel))} "
              "-t " + shlex.quote(tag) + " -n " + shlex.quote(message))
        print()
        print("  Если забыть этот шаг, обновление всё равно поставится: касса")
        print("  не найдёт архив и скачает файлы по одному, как раньше.")
    print()
    print("  И сказать родителям нажать ОБНОВИТЬ КАССУ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
