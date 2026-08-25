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

rem Не %~dp0.  Этот же файл лежит и в архиве с флешки, рядом со своей копией
rem shortcuts.vbs, поэтому проверка "лежит ли рядом shortcuts.vbs" проходила
rem и там.  Запущенный из распакованной папки на Рабочем столе, он считал
rem кассой её и переписывал ярлыки на неё: значки пропадали, а КАССА начинала
rem открывать копию с флешки, с её собственной, давно устаревшей базой.
set "DEST=C:\TauTill"
if exist "%DEST%\shortcuts.vbs" goto :run
set "DEST=%LOCALAPPDATA%\TauTill"
if exist "%DEST%\shortcuts.vbs" goto :run
echo.
echo    Касса на этом компьютере не найдена.
echo    Искал в C:\TauTill и в папке пользователя.
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
