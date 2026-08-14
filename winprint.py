"""Печать сырых байтов через диспетчер печати Windows.

Imported only on Windows, from printer.py.

On Linux the receipt printer is a character device: you open /dev/usb/lp0 and
write ESC/POS bytes to it.  Windows has no such door.  A USB printer there is
reachable only through the spooler, and the vendor driver sitting in front of
it would happily rasterise our control codes into a picture of themselves.
Declaring the job datatype as RAW switches all of that off, so every byte
arrives at the port exactly as written.

The printer is addressed by the name Windows shows in «Устройства и принтеры»,
not by a port.  On the shop machine that name is XP-58.
"""
import ctypes
from ctypes import wintypes

# use_last_error keeps GetLastError() from being clobbered by whatever ctypes
# itself does between the call and our reading of the error code.
_spool = ctypes.WinDLL("winspool.drv", use_last_error=True)

PRINTER_ENUM_LOCAL = 0x02        # queues installed on this machine
PRINTER_ENUM_CONNECTIONS = 0x04  # queues on other machines this user added


class DOC_INFO_1(ctypes.Structure):
    _fields_ = [("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR)]


class PRINTER_INFO_1(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD),
                ("pDescription", wintypes.LPWSTR),
                ("pName", wintypes.LPWSTR),
                ("pComment", wintypes.LPWSTR)]


# Declaring these is not decoration.  Without argtypes ctypes passes a handle
# as a 32-bit int, which silently truncates it on 64-bit Windows and turns
# every later call into a mystery failure.  The till is 64-bit.
_spool.OpenPrinterW.argtypes = [wintypes.LPWSTR,
                                ctypes.POINTER(wintypes.HANDLE),
                                ctypes.c_void_p]
_spool.OpenPrinterW.restype = wintypes.BOOL
_spool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    ctypes.POINTER(DOC_INFO_1)]
_spool.StartDocPrinterW.restype = wintypes.DWORD
_spool.StartPagePrinter.argtypes = [wintypes.HANDLE]
_spool.StartPagePrinter.restype = wintypes.BOOL
_spool.WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_spool.WritePrinter.restype = wintypes.BOOL
_spool.EndPagePrinter.argtypes = [wintypes.HANDLE]
_spool.EndPagePrinter.restype = wintypes.BOOL
_spool.EndDocPrinter.argtypes = [wintypes.HANDLE]
_spool.EndDocPrinter.restype = wintypes.BOOL
_spool.ClosePrinter.argtypes = [wintypes.HANDLE]
_spool.ClosePrinter.restype = wintypes.BOOL
_spool.EnumPrintersW.argtypes = [wintypes.DWORD, wintypes.LPWSTR,
                                 wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD),
                                 ctypes.POINTER(wintypes.DWORD)]
_spool.EnumPrintersW.restype = wintypes.BOOL


def _fail(what):
    """Turn the Win32 error code into something a shopkeeper can act on.

    Raises OSError because that is what the callers in server.py already catch
    around every print, so a printer problem reports itself on screen instead
    of undoing a sale that has already been recorded.
    """
    code = ctypes.get_last_error()
    raise OSError(f"{what}: {ctypes.FormatError(code).strip()} (код {code})")


def printers():
    """Имена всех принтеров, установленных на этом компьютере."""
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed, count = wintypes.DWORD(), wintypes.DWORD()
    # The first call is expected to fail: it exists only to report how large a
    # buffer the second one needs.
    _spool.EnumPrintersW(flags, None, 1, None, 0,
                         ctypes.byref(needed), ctypes.byref(count))
    if not needed.value:
        return []
    buf = ctypes.create_string_buffer(needed.value)
    if not _spool.EnumPrintersW(flags, None, 1, buf, needed.value,
                                ctypes.byref(needed), ctypes.byref(count)):
        _fail("не удалось получить список принтеров")
    # Windows packs the structures at the front of the buffer and the strings
    # they point at behind them, so the array has to be read in place.
    info = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_1))
    return [info[i].pName for i in range(count.value)]


def resolve(name):
    """Найти очередь печати по имени.

    An exact name wins.  Failing that, a prefix match: Windows invents a new
    queue called «XP-58 (копия 1)» when the printer is plugged into a different
    USB socket, and a shop should not lose its till over a moved cable.
    Anything less certain is an error that lists what is actually installed,
    because a wrong guess here prints the day's takings into the XPS writer.
    """
    have = printers()
    for p in have:
        if p.lower() == name.lower():
            return p
    near = [p for p in have if p.lower().startswith(name.lower())]
    if len(near) == 1:
        return near[0]
    raise OSError("принтер «{}» не найден. Установлены: {}".format(
        name, ", ".join(have) if have else "ни одного принтера"))


def send_raw(data, name, doc="УМАГ: чек"):
    """Отправить готовые байты ESC/POS на принтер `name`."""
    printer = resolve(name)
    handle = wintypes.HANDLE()
    if not _spool.OpenPrinterW(printer, ctypes.byref(handle), None):
        _fail(f"не удалось открыть принтер «{printer}»")
    try:
        info = DOC_INFO_1(doc, None, "RAW")
        if not _spool.StartDocPrinterW(handle, 1, ctypes.byref(info)):
            _fail(f"принтер «{printer}» не принял задание печати")
        try:
            if not _spool.StartPagePrinter(handle):
                _fail(f"принтер «{printer}» не принял страницу")
            try:
                buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
                written = wintypes.DWORD()
                if not _spool.WritePrinter(handle, buf, len(data),
                                           ctypes.byref(written)):
                    _fail(f"не удалось передать чек принтеру «{printer}»")
                if written.value != len(data):
                    raise OSError(
                        f"принтер «{printer}» принял только {written.value}"
                        f" байт из {len(data)}, чек напечатан не полностью")
            finally:
                _spool.EndPagePrinter(handle)
        finally:
            # Closing the job is what actually hands it to the spooler, so it
            # has to happen even when the write above went wrong.
            _spool.EndDocPrinter(handle)
    finally:
        _spool.ClosePrinter(handle)
