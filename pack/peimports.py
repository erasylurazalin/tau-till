"""Какие библиотеки требует программа для Windows.

Читает таблицу импорта PE-файла без внешних инструментов, чтобы сборка могла
проверить сама себя.

Понадобилось это дорогой ценой.  В магазин уехала сборка с Python 3.9, и касса
отказалась работать с сообщением про отсутствующий api-ms-win-core-path-l1-1-0.dll.
Windows 7 такой библиотеки не знает: наборы api-ms-win-core-* появились позже,
и python39.dll на неё опирается, а python38.dll нет.  Проверять это глазами
после каждой смены версии нельзя, поэтому проверяет сборка.
"""
import struct


def imports(path):
    """Имена библиотек, которые этот exe или dll требует при запуске."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []

    sections_count = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    # У 64-битных файлов каталоги данных лежат на 16 байт дальше: несколько
    # полей заголовка в них шире.
    dirs = opt + (112 if magic == 0x20B else 96)
    import_rva = struct.unpack_from("<I", data, dirs + 8)[0]
    if not import_rva:
        return []

    sections = []
    base = opt + opt_size
    for i in range(sections_count):
        s = base + i * 40
        virt_size, virt_addr, raw_size, raw_ptr = struct.unpack_from(
            "<IIII", data, s + 8)
        sections.append((virt_addr, max(virt_size, raw_size), raw_ptr))

    def offset(rva):
        for virt_addr, size, raw_ptr in sections:
            if virt_addr <= rva < virt_addr + size:
                return raw_ptr + (rva - virt_addr)
        return None

    names, entry = [], offset(import_rva)
    if entry is None:
        return []
    while True:
        chunk = data[entry:entry + 20]
        if len(chunk) < 20 or chunk == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", chunk, 12)[0]
        pos = offset(name_rva)
        if pos is not None:
            end = data.index(b"\0", pos)
            names.append(data[pos:end].decode("ascii", "replace"))
        entry += 20
    return names


def too_new_for_win7(folder):
    """Файлы, требующие наборов api-ms-win-core-*, которых неоткуда взять.

    Сама по себе ссылка на api-ms-win-core-* не беда: половина этих наборов
    едет в той же папке вместе с Universal C Runtime, и ucrtbase.dll опирается
    именно на них.  Беда, когда требуемого файла нет ни в Windows 7, ни рядом
    с программой, и вот это здесь и ищется.
    """
    have = {f.name.lower() for f in folder.iterdir() if f.is_file()}
    found = {}
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in (".exe", ".dll", ".pyd"):
            continue
        bad = [n for n in imports(f)
               if n.lower().startswith("api-ms-win-core-")
               and n.lower() not in have]
        if bad:
            found[f.name] = bad
    return found
