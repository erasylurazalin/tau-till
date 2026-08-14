@echo off
rem Включение автозапуска кассы Tau Till.
rem
rem Лежит в папке кассы рядом с shortcuts.vbs.  Вся работа делается там же:
rem этот файл только говорит создателю ярлыков, что ссылку в Автозагрузку
rem ставить надо.
rem
rem Нигде не ждём нажатия клавиши: на кассовом компьютере нет клавиатуры.
chcp 866 >nul
setlocal EnableExtensions
title Автозапуск кассы: включить
cls

set "DEST=%~dp0"
if "%DEST:~-1%"=="\" set "DEST=%DEST:~0,-1%"

if exist "%DEST%\shortcuts.vbs" goto :run
echo.
echo    Не нашёл shortcuts.vbs рядом с этим файлом.
echo    Запускайте его из папки кассы, а не с флешки.
echo.
timeout /t 30 /nobreak >nul
exit /b 1

:run
set "TAU_DEST=%DEST%"
set "TAU_AUTOSTART=1"
cscript //nologo "%DEST%\shortcuts.vbs"
if errorlevel 1 goto :fail

echo.
echo    ==================================================
echo       АВТОЗАПУСК ВКЛЮЧЁН
echo    ==================================================
echo.
echo    Теперь касса будет открываться сама, как только
echo    компьютер включится и загрузится.
echo.
echo    Передумаете - запустите здесь же
echo    АВТОЗАПУСК ВЫКЛЮЧИТЬ.bat
echo.
echo    Это окно закроется само.
timeout /t 20 /nobreak >nul
exit /b 0

:fail
echo.
echo    Не получилось создать ярлык в Автозагрузке.
echo    Это окно закроется через минуту.
timeout /t 60 /nobreak >nul
exit /b 1
