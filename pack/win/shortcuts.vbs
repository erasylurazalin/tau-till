' Ярлыки кассы Tau Till: четыре на рабочем столе и один в автозапуске.
'
' Куда установлена касса, скрипт узнаёт из переменной окружения TAU_DEST,
' которую выставляет установщик.  Через переменную, а не через аргумент,
' потому что аргументы командной строки приходят сюда в кодировке консоли,
' а переменные окружения Windows передаёт как есть.
'
' Файл сохранён в UTF-16, иначе Windows Script Host прочитает русские
' надписи как набор вопросительных знаков.

Option Explicit

Dim sh, dest, desktop, startup
Set sh = CreateObject("WScript.Shell")

dest = sh.ExpandEnvironmentStrings("%TAU_DEST%")
If dest = "" Or dest = "%TAU_DEST%" Then dest = "C:\TauTill"

desktop = sh.SpecialFolders("Desktop")
startup = sh.SpecialFolders("Startup")

MakeLink desktop & "\КАССА.lnk", "", "kassa.ico", _
         "Запустить кассу Tau Till"
MakeLink desktop & "\ПЕРЕЗАПУСТИТЬ КАССУ.lnk", " --restart", "restart.ico", _
         "Закрыть и снова открыть кассу, если она подвисла"
MakeLink desktop & "\ОБНОВИТЬ КАССУ.lnk", " --update", "update.ico", _
         "Скачать и поставить новую версию программы"
MakeLink desktop & "\ОСТАНОВИТЬ КАССУ.lnk", " --stop", "stop.ico", _
         "Полностью остановить кассу"
' Автозапуск ставится только по явной просьбе.  Пока магазин работает на
' старой программе, поднимать новую при каждом включении компьютера нельзя,
' а включить это потом можно одним двойным щелчком.
'
' Три состояния, а не два: "1" ставит ссылку в Автозагрузку, "0" убирает, а
' если переменной нет вовсе, всё остаётся как было.  Иначе установка
' очередного дополнения молча выключала бы автозапуск, который в магазине уже
' решили включить.
Dim autoLink, want, fso
Set fso = CreateObject("Scripting.FileSystemObject")
autoLink = startup & "\TAU (автозапуск).lnk"
want = sh.ExpandEnvironmentStrings("%TAU_AUTOSTART%")
If want = "%TAU_AUTOSTART%" Then want = ""

If want = "1" Then
    MakeLink autoLink, " --boot", "kassa.ico", _
             "Касса запускается сама при включении компьютера"
    WScript.Echo "Ярлыки созданы, автозапуск включён."
ElseIf want = "0" Then
    If fso.FileExists(autoLink) Then fso.DeleteFile autoLink
    WScript.Echo "Ярлыки созданы, автозапуск выключен."
ElseIf fso.FileExists(autoLink) Then
    ' Ссылка уже есть, и её надо переписать под новые пути.
    MakeLink autoLink, " --boot", "kassa.ico", _
             "Касса запускается сама при включении компьютера"
    WScript.Echo "Ярлыки созданы, автозапуск остался включённым."
Else
    WScript.Echo "Ярлыки созданы, автозапуск остался выключенным."
End If

Sub MakeLink(linkPath, extraArgs, icon, note)
    Dim link
    Set link = sh.CreateShortcut(linkPath)
    ' pythonw.exe, а не python.exe: у него нет чёрного окна консоли.
    link.TargetPath = dest & "\python\pythonw.exe"
    link.Arguments = """" & dest & "\app\launch.py""" & extraArgs
    link.WorkingDirectory = dest
    link.IconLocation = dest & "\" & icon
    link.Description = note
    link.Save
End Sub
