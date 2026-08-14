"""Minimal .xlsx reader.  Stdlib only -- an xlsx is just a zip of XML.

Handles what the UMAG export actually uses: shared strings, inline strings and
numbers.  Not a general Excel implementation (no formulas, no dates, no styles).
"""
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column(ref):
    """'BC12' -> 'BC'"""
    return "".join(ch for ch in ref if ch.isalpha())


def rows(path, sheet="xl/worksheets/sheet1.xml"):
    """Yield each row as a list of cell strings, padded to the widest column.

    Blank cells are omitted entirely by Excel, so cells are placed by their
    column letter rather than by order -- otherwise a missing "Доп. код"
    would shift every later value one column to the left.
    """
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                strings.append("".join(t.text or "" for t in si.iter(NS + "t")))
        sheet_xml = ET.fromstring(z.read(sheet))

    def value(c):
        kind = c.get("t")
        if kind == "s":
            v = c.find(NS + "v")
            return strings[int(v.text)] if v is not None else ""
        if kind == "inlineStr":
            return "".join(t.text or "" for t in c.iter(NS + "t"))
        v = c.find(NS + "v")
        return v.text if v is not None else ""

    for r in sheet_xml.find(NS + "sheetData"):
        cells = {_column(c.get("r")): (value(c) or "").strip() for c in r}
        if not cells:
            continue
        width = max(_col_index(k) for k in cells) + 1
        yield [cells.get(_col_letter(i), "") for i in range(width)]


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_letter(index):
    s, n = "", index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s
