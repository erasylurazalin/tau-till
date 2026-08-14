@echo off
chcp 866 >nul
setlocal
title УМАГ: проверка компьютера
cls

echo.
echo   ================================================
echo      УМАГ: проверка компьютера перед установкой
echo   ================================================
echo.
echo   Программа НИЧЕГО не меняет на компьютере.
echo   Она только читает сведения и записывает их в файл.
echo.
echo   Перед началом:
echo     1. включите чековый принтер
echo     2. подключите его кабелем к компьютеру
echo.
pause

rem --- куда писать отчёт: сначала рядом с самим файлом (флешка),
rem --- если туда нельзя, то на рабочий стол
set "OUT=%~dp0umag-recon.txt"
>"%OUT%" echo УМАГ: сведения о компьютере 2>nul
if not exist "%OUT%" set "OUT=%USERPROFILE%\Desktop\umag-recon.txt"
>"%OUT%" echo УМАГ: сведения о компьютере

set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%"

call :head "КОГДА И ГДЕ"
>>"%OUT%" echo    Дата и время : %DATE% %TIME%
>>"%OUT%" echo    Компьютер    : %COMPUTERNAME%
>>"%OUT%" echo    Пользователь : %USERNAME%
>>"%OUT%" echo    Папка отчёта : %OUT%

echo   [1/9] Windows...
call :head "WINDOWS"
wmic os get Caption,Version,BuildNumber,OSArchitecture,ServicePackMajorVersion,ServicePackMinorVersion,Locale,SystemDirectory /value >>"%OUT%" 2>&1
call :head "КОДОВАЯ СТРАНИЦА КОНСОЛИ"
chcp >>"%OUT%" 2>&1

echo   [2/9] железо...
call :head "КОМПЬЮТЕР"
wmic computersystem get Manufacturer,Model,SystemType,TotalPhysicalMemory /value >>"%OUT%" 2>&1
call :head "ПРОЦЕССОР"
wmic cpu get Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed /value >>"%OUT%" 2>&1

echo   [3/9] экран...
call :head "ЭКРАН (нужно для размера кнопок и клавиатуры)"
wmic path Win32_VideoController get Name,CurrentHorizontalResolution,CurrentVerticalResolution,VideoModeDescription /value >>"%OUT%" 2>&1
wmic desktopmonitor get Name,ScreenWidth,ScreenHeight /value >>"%OUT%" 2>&1

echo   [4/9] диск...
call :head "ДИСКИ (свободное место в байтах)"
wmic logicaldisk get DeviceID,DriveType,VolumeName,FreeSpace,Size /value >>"%OUT%" 2>&1

echo   [5/9] принтеры...
call :head "ПРИНТЕРЫ (главное: точное имя чекового принтера)"
wmic printer get Name,ShareName,DriverName,PortName,Default,WorkOffline,PrinterStatus /value >>"%OUT%" 2>&1
call :head "ПОРТЫ ПРИНТЕРОВ"
wmic path Win32_TCPIPPrinterPort get Name,HostAddress,PortNumber /value >>"%OUT%" 2>&1

echo   [6/9] устройства USB (это самая долгая часть, до минуты)...
call :head "УСТРОЙСТВА, ПОХОЖИЕ НА ПРИНТЕР"
wmic path Win32_PnPEntity get Name,DeviceID,Status 2>nul | findstr /i "print xprinter POS58 POS-58 VID_0483" >>"%OUT%" 2>&1
call :head "УСТРОЙСТВА, ПОХОЖИЕ НА СЕНСОРНЫЙ ЭКРАН"
wmic path Win32_PnPEntity get Name,DeviceID,Status 2>nul | findstr /i "touch сенсор digitizer" >>"%OUT%" 2>&1

echo   [7/9] браузеры...
call :head "БРАУЗЕРЫ"
call :browser "Google Chrome"     "%PF%\Google\Chrome\Application\chrome.exe"
call :browser "Google Chrome x86" "%PF86%\Google\Chrome\Application\chrome.exe"
call :browser "Google Chrome user" "%LAD%\Google\Chrome\Application\chrome.exe"
call :browser "Mozilla Firefox"   "%PF%\Mozilla Firefox\firefox.exe"
call :browser "Mozilla Firefox x86" "%PF86%\Mozilla Firefox\firefox.exe"
call :browser "Microsoft Edge"    "%PF86%\Microsoft\Edge\Application\msedge.exe"
call :browser "Yandex Browser"    "%LAD%\Yandex\YandexBrowser\Application\browser.exe"
call :browser "Yandex Browser PF" "%PF86%\Yandex\YandexBrowser\Application\browser.exe"
call :browser "Opera"             "%PF86%\Opera\opera.exe"
call :browser "Opera user"        "%LAD%\Programs\Opera\opera.exe"
call :browser "Internet Explorer" "%PF%\Internet Explorer\iexplore.exe"
call :head "БРАУЗЕРЫ, ЗАРЕГИСТРИРОВАННЫЕ В СИСТЕМЕ"
reg query "HKLM\SOFTWARE\Clients\StartMenuInternet" >>"%OUT%" 2>&1

echo   [8/9] Python...
call :head "PYTHON (если уже установлен)"
where python >>"%OUT%" 2>&1
where py >>"%OUT%" 2>&1
python --version >>"%OUT%" 2>&1
reg query "HKLM\SOFTWARE\Python\PythonCore" >>"%OUT%" 2>&1
reg query "HKCU\SOFTWARE\Python\PythonCore" >>"%OUT%" 2>&1

echo   [9/9] обновления и сеть...
call :head "ВАЖНЫЕ ОБНОВЛЕНИЯ WINDOWS"
>>"%OUT%" echo    (KB976932 = SP1, KB4474419 = поддержка подписей SHA-2)
wmic qfe get HotFixID 2>nul | findstr /i "KB976932 KB4474419 KB2533623 KB3063858" >>"%OUT%" 2>&1
call :head "БРАНДМАУЭР"
netsh advfirewall show allprofiles state >>"%OUT%" 2>&1
call :head "ЗАНЯТ ЛИ ПОРТ 8000"
netstat -an 2>nul | findstr ":8000" >>"%OUT%" 2>&1

call :head "КОНЕЦ ОТЧЁТА"

echo.
echo   Готово. Отчёт сохранён:
echo   %OUT%
echo.
echo   Сейчас он откроется в Блокноте.
echo   Сохраните этот файл и передайте его.
echo.
pause
start notepad "%OUT%"
endlocal
goto :eof

rem ---------------------------------------------------------------
:head
>>"%OUT%" echo.
>>"%OUT%" echo ==================================================
>>"%OUT%" echo  %~1
>>"%OUT%" echo ==================================================
goto :eof

rem Проверить браузер: есть ли файл и какая у него версия.
rem Путь для WMI нужен с двойными обратными косыми, иначе запрос не сработает.
:browser
if not exist "%~2" goto :eof
>>"%OUT%" echo.
>>"%OUT%" echo    %~1
>>"%OUT%" echo      файл: %~2
set "WMIPATH=%~2"
set "WMIPATH=%WMIPATH:\=\\%"
wmic datafile where "name='%WMIPATH%'" get Version /value >>"%OUT%" 2>&1
goto :eof
