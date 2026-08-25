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

dest = Tidy(sh.ExpandEnvironmentStrings("%TAU_DEST%"))
If dest = "" Or dest = "%TAU_DEST%" Then dest = "C:\TauTill"

' Последняя проверка перед тем, как переписывать ярлыки: похоже ли dest на
' установленную кассу.  Значки лежат в корне папки кассы, потому что установщик
' кладёт их туда из icons\, а в самом архиве с флешки в корне их нет.  Значит
' отсутствие kassa.ico означает ровно одно: нам подсунули не ту папку, обычно
' распакованный архив на Рабочем столе.  Переписать ярлыки на него это не
' только потерять значки, но и заставить КАССУ открывать копию с флешки с её
' собственной базой, где нет ни одной сегодняшней продажи.
Dim probe
Set probe = CreateObject("Scripting.FileSystemObject")
If Not probe.FileExists(dest & "\kassa.ico") Then
    WScript.Echo "Это не папка установленной кассы: " & dest
    WScript.Echo "Ярлыки не тронуты."
    WScript.Quit 1
End If

desktop = Folder("Desktop", Tidy(sh.ExpandEnvironmentStrings("%USERPROFILE%")) _
                 & "\Desktop")
startup = Folder("Startup", Tidy(sh.ExpandEnvironmentStrings("%APPDATA%")) _
                 & "\Microsoft\Windows\Start Menu\Programs\Startup")

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
If Not fso.FolderExists(startup) Then MakePath fso, startup
autoLink = startup & "\TAU (автозапуск).lnk"
want = Tidy(sh.ExpandEnvironmentStrings("%TAU_AUTOSTART%"))
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

' Убрать с конца пути то, что там быть не должно: пробелы, закрывающую
' обратную косую (установщик мог передать путь с ней) и невидимые символы.
' Один такой хвост превращает C:\TauTill\kassa.ico в несуществующий файл, и
' ярлык молча остаётся без значка или не создаётся вовсе.
Function Tidy(s)
    Dim c
    Do While Len(s) > 0
        c = Right(s, 1)
        If c = " " Or c = "\" Or Asc(c) < 32 Then
            s = Left(s, Len(s) - 1)
        Else
            Exit Do
        End If
    Loop
    Tidy = s
End Function

' Куда класть ярлык.  Обычно это спрашивают у самой Windows, но список особых
' папок реализован не везде одинаково, и на пустой ответ есть запасной путь.
' С Vista имена этих папок на диске всегда английские, а по-русски они лишь
' показываются, так что запасной путь верен и на русской Windows.
Function Folder(what, fallback)
    Dim p
    p = ""
    On Error Resume Next
    p = sh.SpecialFolders(what)
    On Error GoTo 0
    If p = "" Then p = fallback
    Folder = p
End Function

' Создать папку вместе со всеми родительскими.  На настоящей Windows папка
' автозапуска есть всегда, но полагаться на это, когда ошибка выльется в
' молча не включившийся автозапуск, не стоит.
Sub MakePath(fso, path)
    Dim parent
    parent = fso.GetParentFolderName(path)
    If parent <> "" And Not fso.FolderExists(parent) Then MakePath fso, parent
    If Not fso.FolderExists(path) Then fso.CreateFolder path
End Sub

Sub MakeLink(linkPath, extraArgs, icon, note)
    Dim link
    Set link = sh.CreateShortcut(linkPath)
    ' pythonw.exe, а не python.exe: у него нет чёрного окна консоли.
    link.TargetPath = dest & "\python\pythonw.exe"
    link.Arguments = """" & dest & "\app\launch.py""" & extraArgs
    link.WorkingDirectory = dest
    ' Со значком обязательно нужен номер картинки внутри файла: без него
    ' Windows значок иногда берёт, а иногда отказывается вовсе.
    link.IconLocation = dest & "\" & icon & ",0"
    link.Description = note
    link.Save
End Sub
