"""Проверка главного цикла запуска, без настоящего браузера.

Запускается под Wine на машине разработки, не попадает в сборку.

The one thing that cannot be checked by reading the code is the handover:
launcher starts the server, launcher starts the browser, and when the browser
goes away the server has to go away with it.  A browser is not needed to prove
that, only a process that exits, so this stands a sleeping Python in its place.
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import launch  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  СБОЙ ") + label
          + ("  " + str(detail) if detail and not cond else ""))
    if not cond:
        ok = False


FAKE_LIFETIME = 8
fake = {}


def fake_chrome(cfg):
    """Вместо браузера: процесс, который просто живёт восемь секунд."""
    proc = subprocess.Popen(
        [str(launch.PYDIR / "python.exe"), "-c",
         "import time; time.sleep(%d)" % FAKE_LIFETIME],
        creationflags=launch.NO_WINDOW)
    fake["proc"] = proc
    launch.log("подставной браузер, номер %d" % proc.pid)
    return proc.pid


launch.start_chrome = fake_chrome
cfg = launch.settings()

print("запуск")
launch.stop_all(cfg)
check("порт свободен перед началом", not launch.port_answers(cfg["port"], 0.3))

began = time.time()
code = launch.start(cfg)
took = time.time() - began

check("start вернул успех", code == 0, code)
check("цикл держался, пока жил браузер", took >= FAKE_LIFETIME - 1, round(took, 1))
check("сервер остановлен вместе с браузером",
      not launch.port_answers(cfg["port"], 0.3))
check("состояние очищено", not launch.read_state().get("server"),
      launch.read_state())

print("\nповторный запуск при уже работающей кассе")
server_pid = launch.start_server(cfg)
check("сервер поднялся", bool(server_pid))
fake_pid = fake_chrome(cfg)
launch.write_state(server_pid, fake_pid)
began = time.time()
code = launch.start(cfg)
check("вторая копия сразу вышла", time.time() - began < 2, round(time.time() - began, 1))
check("и не тронула работающий сервер", launch.port_answers(cfg["port"], 0.3))
launch.stop_all(cfg)
check("stop_all всё выключил", not launch.port_answers(cfg["port"], 0.3))

print("\nбраузер закрыли, сервер остался")
server_pid = launch.start_server(cfg)
launch.write_state(server_pid, 999999)      # такого процесса нет
began = time.time()
launch.start_chrome = lambda cfg: fake_chrome(cfg)
code = launch.start(cfg)
check("касса не перезапускала сервер, только браузер",
      time.time() - began >= FAKE_LIFETIME - 1)
check("после закрытия браузера сервера нет",
      not launch.port_answers(cfg["port"], 0.3))

launch.stop_all(cfg)
print("\nВСЁ ХОРОШО" if ok else "\nЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
