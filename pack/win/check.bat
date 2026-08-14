@echo off
rem Проверка кассы: что с Python, с браузером, с принтером и с базой.
rem Лежит рядом с папками app и python, поэтому все пути от %~dp0.
chcp 866 >nul
title Проверка кассы Tau Till
cls
echo.
echo    Проверяю кассу. Это займёт несколько секунд.
echo.
"%~dp0python\python.exe" "%~dp0app\launch.py" --check
echo.
echo    Отчёт сейчас откроется в Блокноте.
start notepad "%~dp0logs\tau-check.txt"
timeout /t 8 /nobreak >nul
exit /b 0
