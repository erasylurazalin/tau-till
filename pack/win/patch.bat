@echo off
rem Дополнение к уже установленной кассе: библиотеки Universal C Runtime,
rem свежая программа и значки.  Python сюда не входит, он уже лежит на кассе
rem с прошлой установки, и именно поэтому файл в пять раз меньше полного.
rem
rem Нужен, когда флешка не тянет большой архив или когда полную установку
rem гонять незачем.
chcp 866 >nul
setlocal EnableExtensions
title Дополнение кассы Tau Till
cls

echo.
echo    ==================================================
echo       ДОПОЛНЕНИЕ КАССЫ TAU TILL
echo    ==================================================
echo.

rem --- где стоит касса ---------------------------------------------------
set "DEST=C:\TauTill"
if exist "%DEST%\python\python.exe" goto :havedest
set "DEST=%LOCALAPPDATA%\TauTill"
if exist "%DEST%\python\python.exe" goto :havedest
echo    Касса на этом компьютере не найдена.
echo.
echo    Искал здесь:
echo       C:\TauTill\python\python.exe
echo       %LOCALAPPDATA%\TauTill\python\python.exe
echo.
echo    Значит нужна полная установка, а не дополнение.
echo    Возьмите архив полный архив Tau Till и запустите
echo    из него УСТАНОВИТЬ КАССУ.bat
goto :fail

:havedest
echo    Касса найдена: %DEST%
echo.

rem Дополнение годится только поверх Python 3.8.  На кассе успел побывать 3.9,
rem который на Windows 7 вообще не запускается, и подкладывать к нему свежую
rem программу бессмысленно: нужна полная переустановка с правильным Python.
"%DEST%\python\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)" >nul 2>&1
if not errorlevel 1 goto :goodpython
echo    На кассе стоит неподходящий Python.
echo.
echo    Дополнением это не лечится, нужна полная установка:
echo    возьмите полный архив Tau Till и запустите из него
echo    УСТАНОВИТЬ КАССУ.bat
goto :fail

:goodpython

if not exist "%DEST%\app\launch.py" goto :copy
echo    [1/5] Останавливаю кассу, если она работает...
"%DEST%\python\python.exe" "%DEST%\app\launch.py" --stop >nul 2>&1

:copy
echo    [2/5] Кладу недостающие библиотеки Windows...
copy /Y "%~dp0python\*.dll" "%DEST%\python\" >nul
if errorlevel 1 goto :copyfail

echo    [3/5] Обновляю программу...
xcopy "%~dp0app\*" "%DEST%\app\" /E /I /Y /Q >nul
if errorlevel 1 goto :copyfail
copy /Y "%~dp0icons\*.ico" "%DEST%\" >nul
rem Список корневых сертификатов для проверки обновлений.
copy /Y "%~dp0cacert.pem" "%DEST%\" >nul
rem Кладём и сам создатель ярлыков: им пользуются переключатели автозапуска.
copy /Y "%~dp0shortcuts.vbs" "%DEST%\" >nul
copy /Y "%~dp0АВТОЗАПУСК*.bat" "%DEST%\" >nul
copy /Y "%~dp0ПРОВЕРКА КАССЫ.bat" "%DEST%\" >nul
md "%DEST%\data" 2>nul
md "%DEST%\logs" 2>nul
if not exist "%DEST%\settings.json" copy "%~dp0settings.json" "%DEST%\settings.json" >nul

echo    [4/5] Проверяю Python...
"%DEST%\python\python.exe" -c "import sqlite3, ctypes" >nul 2>&1
if errorlevel 1 goto :ucrt

echo    [5/5] Делаю значки на рабочем столе...
set "TAU_DEST=%DEST%"
cscript //nologo "%~dp0shortcuts.vbs"

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
echo    Автозапуск остался таким, каким был. Включить
echo    или выключить его можно в папке кассы файлами
echo    АВТОЗАПУСК ВКЛЮЧИТЬ.bat и АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat
echo.
echo    Сейчас откроется отчёт проверки.
echo.
start notepad "%DEST%\logs\tau-check.txt"
timeout /t 25 /nobreak >nul
exit /b 0

:copyfail
echo.
echo    ОШИБКА при копировании. Похоже, файлы не читаются
echo    с того носителя, откуда вы это запустили.
echo    Скопируйте папку на рабочий стол и запустите оттуда.
goto :fail

:ucrt
echo.
echo    Python по-прежнему не запускается.
echo    Запустите ЕСЛИ КАССА НЕ СТАВИТСЯ.bat из полного архива,
echo    перезагрузите компьютер и повторите.
goto :fail

:fail
echo.
echo    Это окно закроется через минуту.
timeout /t 60 /nobreak >nul
exit /b 1
