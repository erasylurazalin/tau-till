"""Выпуск новой версии Tau Till.

    python3 pack/release.py 0.5.1 -m "чинит печать копии чека"
    python3 pack/release.py                 # без номера: только пересобрать список

Что делает: проставляет номер версии, пересчитывает контрольные суммы всех
файлов программы, кладёт их в update/version.json и делает коммит с меткой.
После этого остаётся один `git push --follow-tags`, и касса в магазине увидит
новую версию по кнопке ОБНОВИТЬ.

Почему именно так, а не через выпуски GitHub с приложенными архивами: касса
скачивает семь обычных файлов по прямым ссылкам на репозиторий.  Ни токенов,
ни отдельного шага публикации, ни лишних инструментов на этой машине.  Обычный
git push и есть выпуск.

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
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
UPDATE = PROJECT / "update"

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


def say(msg):
    print("  " + msg)


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(PROJECT)] + list(args),
                          capture_output=True, text=True, check=check)


def repo_base():
    """Адрес, с которого касса читает файлы этого репозитория.

    Пока origin не заведён, то же самое можно сказать прямо:
        TAU_REPO=имя/tau-till python3 pack/release.py
    """
    forced = os.environ.get("TAU_REPO", "").strip()
    if forced and "/" in forced:
        owner, name = forced.split("/", 1)
        return f"https://raw.githubusercontent.com/{owner}/{name.rstrip('.git')}/main/"
    try:
        url = git("remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    if not m:
        return None
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/main/"


def set_version(number):
    """Проставить номер в version.py, единственное место, где он живёт."""
    path = PROJECT / "version.py"
    text = path.read_text(encoding="utf-8")
    new = re.sub(r'VERSION = "[^"]+"', f'VERSION = "{number}"', text, count=1)
    if new == text and f'VERSION = "{number}"' not in text:
        raise SystemExit("не нашёл строку VERSION в version.py")
    path.write_text(new, encoding="utf-8")
    return number


def current_version():
    text = (PROJECT / "version.py").read_text(encoding="utf-8")
    return re.search(r'VERSION = "([^"]+)"', text).group(1)


def manifest(number, notes):
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
    return {
        "version": number,
        "released": date.today().isoformat(),
        "notes": notes or "",
        "base": base or "",
        "files": files,
    }


def main():
    ap = argparse.ArgumentParser(description="выпуск новой версии Tau Till")
    ap.add_argument("version", nargs="?", help="например 0.5.1")
    ap.add_argument("-m", "--notes", default="", help="что изменилось")
    ap.add_argument("--no-commit", action="store_true",
                    help="только обновить файлы, без коммита и метки")
    args = ap.parse_args()

    number = set_version(args.version) if args.version else current_version()
    say(f"версия: {number}")

    UPDATE.mkdir(exist_ok=True)
    man = manifest(number, args.notes)
    (UPDATE / "version.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(f"список обновления: {len(man['files'])} файлов, "
        f"{sum(f['size'] for f in man['files']) // 1024} КБ")
    if man["base"]:
        say("касса будет брать их отсюда: " + man["base"])

    if args.no_commit:
        return 0

    if not (PROJECT / ".git").exists():
        say("git в проекте ещё не заведён, коммит пропущен")
        return 0

    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        say("менять нечего, всё уже закоммичено")
        return 0
    message = f"Версия {number}" + (f": {args.notes}" if args.notes else "")
    git("commit", "-m", message)
    tag = "v" + number
    existing = git("tag", "-l", tag).stdout.strip()
    if existing:
        say(f"метка {tag} уже есть, оставляю как есть")
    else:
        git("tag", "-a", tag, "-m", message)
        say(f"метка: {tag}")
    say("коммит готов")
    print()
    print("  Осталось отправить это на GitHub:")
    print("      git push --follow-tags")
    print()
    print("  И сказать родителям нажать ОБНОВИТЬ КАССУ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
