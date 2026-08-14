@echo off
rem Установщик кассы Tau Till.  Собирается build.py: этот файл лежит в проекте в
rem UTF-8, а в готовую сборку попадает в CP866 с переводами строк CRLF, иначе
rem консоль Windows покажет русский текст крокозябрами.
rem
rem Нигде не ждём нажатия клавиши: на кассовом компьютере нет клавиатуры.
chcp 866 >nul
setlocal EnableExtensions
title Установка кассы Tau Till
cls

echo.
echo    ==================================================
echo       УСТАНОВКА КАССЫ TAU TILL
echo    ==================================================
echo.
echo    Программа поставит кассу на этот компьютер
echo    и сделает значки на рабочем столе. Сама она
echo    при включении компьютера пока запускаться
echo    не будет.
echo.
echo    Ничего нажимать не нужно, просто подождите.
echo.

rem --- куда ставим -------------------------------------------------------
rem Сначала пробуем C:\TauTill: туда проще заглянуть, когда что-то не так.
rem Если создавать папки в корне диска не разрешено, уходим в профиль
rem пользователя, где права есть всегда.
set "DEST=C:\TauTill"
md "%DEST%" 2>nul
if exist "%DEST%\" goto :havedest
set "DEST=%LOCALAPPDATA%\TauTill"
md "%DEST%" 2>nul
if exist "%DEST%\" goto :havedest
echo    НЕ УДАЛОСЬ создать папку для кассы.
echo    Попробуйте запустить установку от имени администратора.
goto :fail

:havedest
echo    Папка кассы: %DEST%
echo.

rem --- остановить кассу, если она уже работает ---------------------------
if not exist "%DEST%\python\python.exe" goto :copy
echo    [1/6] Останавливаю работающую кассу...
"%DEST%\python\python.exe" "%DEST%\app\launch.py" --stop >nul 2>&1

:copy
echo    [2/6] Копирую программу...
xcopy "%~dp0app\*" "%DEST%\app\" /E /I /Y /Q >nul
if errorlevel 1 goto :copyfail
copy /Y "%~dp0icons\*.ico" "%DEST%\" >nul
rem Список корневых сертификатов для проверки обновлений.
copy /Y "%~dp0cacert.pem" "%DEST%\" >nul
rem Кладём и сам создатель ярлыков: им пользуются переключатели автозапуска.
copy /Y "%~dp0shortcuts.vbs" "%DEST%\" >nul
copy /Y "%~dp0АВТОЗАПУСК*.bat" "%DEST%\" >nul
copy /Y "%~dp0ПРОВЕРКА КАССЫ.bat" "%DEST%\" >nul

echo    [3/6] Копирую Python, это самая долгая часть...
rem Сносим старую папку целиком, а не копируем поверх.  На кассе успела
rem побывать другая версия Python, и две версии в одной папке дают
rem неотлаживаемые чудеса: файлы у них называются одинаково, а внутри разные.
rem Своих данных в этой папке нет, терять нечего.
if exist "%DEST%\python\" rd /s /q "%DEST%\python"
xcopy "%~dp0python\*" "%DEST%\python\" /E /I /Y /Q >nul
if errorlevel 1 goto :copyfail

rem --- данные ------------------------------------------------------------
rem Самое важное место во всём установщике: рабочая база магазина никогда
rem не перезаписывается.  Обновление программы не должно стирать товары,
rem остатки и историю продаж.
md "%DEST%\data" 2>nul
md "%DEST%\logs" 2>nul
echo    [4/6] Проверяю базу товаров...
if exist "%DEST%\data\store.db" goto :havedb
if not exist "%~dp0data\store.db" goto :nodb
copy "%~dp0data\store.db" "%DEST%\data\store.db" >nul
echo          База товаров установлена.
goto :dbdone
:havedb
echo          Уже есть база магазина, оставляю её как есть.
goto :dbdone
:nodb
echo          Базы нет, касса создаст пустую при первом запуске.
:dbdone
if not exist "%DEST%\settings.json" copy "%~dp0settings.json" "%DEST%\settings.json" >nul

rem --- проверка, что Python вообще запускается ---------------------------
echo    [5/6] Проверяю Python...
"%DEST%\python\python.exe" -c "import sqlite3, ctypes" >nul 2>&1
if errorlevel 1 goto :ucrt

rem --- ярлыки ------------------------------------------------------------
echo    [6/6] Делаю значки на рабочем столе...
set "TAU_DEST=%DEST%"
rem Первая установка: автозапуск выключен, пока магазин работает на старой
rem программе.  Дополнения (ДОПОЛНИТЬ КАССУ.bat) этой переменной не трогают
rem и потому не сбрасывают то, что здесь уже включили.
set "TAU_AUTOSTART=0"
cscript //nologo "%~dp0shortcuts.vbs"
if errorlevel 1 goto :linkfail

rem --- проверка оборудования --------------------------------------------
echo.
echo    Проверяю принтер и браузер...
"%DEST%\python\python.exe" "%DEST%\app\launch.py" --check >nul 2>&1

echo.
echo    ==================================================
echo       ГОТОВО
echo    ==================================================
echo.
echo    На рабочем столе появились четыре значка.
echo.
echo       КАССА                 запускает кассу
echo       ПЕРЕЗАПУСТИТЬ КАССУ   если касса подвисла
echo       ОБНОВИТЬ КАССУ        ставит новую версию
echo       ОСТАНОВИТЬ КАССУ      закрывает кассу совсем
echo.
echo    Автозапуск сейчас ВЫКЛЮЧЕН: касса поднимается
echo    только по значку. Когда решите доверить ей
echo    открытие магазина, запустите в папке кассы
echo    АВТОЗАПУСК ВКЛЮЧИТЬ.bat
echo.
echo    Сейчас откроется отчёт проверки. Посмотрите в нём
echo    строку про принтер и строку про Chrome.
echo.
echo    Это окно закроется само.
start notepad "%DEST%\logs\tau-check.txt"
timeout /t 25 /nobreak >nul
exit /b 0

:copyfail
echo.
echo    ОШИБКА при копировании файлов.
echo    Проверьте, что флешка не вынута и на диске есть место.
goto :fail

:linkfail
echo.
echo    Программа установлена, но значки создать не удалось.
echo    Кассу можно запустить отсюда:
echo    %DEST%\python\pythonw.exe %DEST%\app\launch.py
goto :fail

:ucrt
echo.
echo    Python не запускается на этом компьютере.
echo.
echo    В сборке уже лежат библиотеки, которые обычно
echo    решают эту беду без всяких обновлений Windows.
echo    Раз не помогло, остаётся запасной путь.
echo.
echo    Запустите в этой же папке файл
echo       ЕСЛИ КАССА НЕ СТАВИТСЯ.bat
echo    Он поставит обновление Windows KB2999226.
echo    Потом перезагрузите компьютер и повторите
echo    установку кассы.
goto :fail

:fail
echo.
echo    Это окно закроется через минуту.
timeout /t 60 /nobreak >nul
exit /b 1
