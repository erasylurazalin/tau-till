#!/usr/bin/env python3
"""Import a UMAG "Список товаров" export into store.db.

    python3 import_umag.py "Список товаров_export.xlsx"
    python3 import_umag.py file.xlsx --dry-run     # report only, no writes
    python3 import_umag.py file.xlsx --deactivate  # hide goods absent from file
    python3 import_umag.py file.xlsx --stock       # also import quantities
    python3 import_umag.py file.xlsx --stock --zero  # ... including zeros

Matching is by barcode.  Known goods get their name and prices refreshed;
new goods are inserted with stock = NULL ("не посчитан") so that nothing
claims a quantity the shop has not physically counted.

Columns are found by name, not by position.  UMAG exports the same list in
several shapes: a short one of six columns and a detailed one of sixteen,
where the columns we need sit in different places and unfamiliar ones
(Категория, Артикул, Поставщик, суммы по строке) appear in between.  Anything we do not
recognise is ignored rather than treated as an error.

Quantities are imported only when the file actually carries a quantity column
AND --stock is given, because writing stock is a much bigger claim than
refreshing a price.  Each quantity goes in through db.set_stock, so it lands
in the append-only ledger as a "count" move with the correct delta, exactly
as if it had been counted on the shelf.
"""
import re
import sys
from pathlib import Path

import db
import xlsx

# What each field we care about is called across the different exports.  The
# first name that matches wins, so put the common spelling first.
ALIASES = {
    "name":    ["Название товара", "Наименование товара", "Наименование"],
    "barcode": ["Штрихкод", "Штрих-код", "Штрих код"],
    "alt":     ["Доп. штрихкоды", "Доп. штрихкод", "Доп. код", "Доп.код",
                "Дополнительные штрихкоды", "Дополнительный код"],
    "cost":    ["Закуп. цена", "Закуп.цена", "Закупочная цена"],
    "price":   ["Прод. цена", "Прод.цена", "Продажная цена", "Цена продажи"],
    "unit":    ["Ед. изм", "Ед.изм", "Ед. изм.", "Единица измерения"],
    "qty":     ["Количество", "Кол-во", "Остаток", "Остатки",
                "Количество на складе", "Кол-во на складе", "Текущий остаток"],
}

# Without these two a row cannot become a sellable product at all.
REQUIRED = ["name", "barcode"]


def norm(s):
    """Fold a header into something comparable: case, spacing and ё."""
    return " ".join((s or "").split()).strip().lower().replace("ё", "е")


def map_columns(header):
    """Where each field lives in this particular export."""
    seen = {}
    for i, h in enumerate(header):
        key = norm(h)
        if key and key not in seen:      # first column of a name wins
            seen[key] = i
    cols = {}
    for field, names in ALIASES.items():
        for name in names:
            if norm(name) in seen:
                cols[field] = seen[norm(name)]
                break
    missing = [f for f in REQUIRED if f not in cols]
    if missing:
        want = ", ".join(ALIASES[f][0] for f in missing)
        raise SystemExit(
            "В файле не нашлось обязательных столбцов: " + want + "\n"
            "  что есть в файле: " + ", ".join(h for h in header if h.strip()))
    return cols


def codes(s):
    """Split the extra-barcode cell into separate codes.

    The column is called "Доп. штрихкоды" in the plural for a reason: one
    shelf item can carry several stickers, and UMAG puts them all in one cell.
    Separators vary between exports, so accept the usual suspects and drop
    anything that is not a code.
    """
    out = []
    for part in re.split(r"[,;/|\s]+", (s or "").strip()):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def number(s):
    """UMAG writes plain integers, but tolerate a comma decimal separator."""
    s = (s or "").strip().replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def quantity(s):
    """Quantity, or None when the cell is empty.

    Empty and zero are different claims.  Empty means UMAG never tracked this
    product; zero means it tracked it and there is none left.
    """
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def read_export(path):
    """Return (goods, problems, cols).  Rejects rows that cannot be sold."""
    rows = iter(xlsx.rows(path))
    cols = map_columns([h.strip() for h in next(rows)])
    width = max(cols.values()) + 1

    def cell(row, field):
        i = cols.get(field)
        return row[i].strip() if i is not None else ""

    goods, problems, seen = [], [], {}
    for n, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        row = (row + [""] * width)[:max(width, len(row))]
        name, barcode = cell(row, "name"), cell(row, "barcode")
        if not name:
            problems.append((n, barcode, "нет наименования"))
            continue
        if not barcode:
            problems.append((n, name, "нет штрихкода"))
            continue
        if barcode in seen:
            problems.append((n, name, f"штрихкод повторяется (строка {seen[barcode]})"))
            continue
        seen[barcode] = n
        goods.append({
            "barcode": barcode,
            "alt_codes": codes(cell(row, "alt")),
            "name": name,
            "cost": number(cell(row, "cost")),
            "price": number(cell(row, "price")),
            "unit": cell(row, "unit") or "шт",
            "qty": quantity(cell(row, "qty")),
        })
    return goods, problems, cols


def apply(con, goods, deactivate=False, stock_note=None, zeros=False):
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deactivated": 0,
             "codes": 0, "code_clashes": [], "counted": 0, "skipped_zero": 0,
             "already": 0, "recounted": []}
    for g in goods:
        row = con.execute("SELECT * FROM products WHERE barcode = ?",
                          (g["barcode"],)).fetchone()
        if row is None:
            cur = con.execute(
                "INSERT INTO products (barcode, name, price, cost,"
                " unit, stock) VALUES (?,?,?,?,?,NULL)",
                (g["barcode"], g["name"], g["price"], g["cost"], g["unit"]))
            pid = cur.lastrowid
            known_stock = None
            stats["added"] += 1
        else:
            pid = row["id"]
            known_stock = row["stock"]
            changed = any(row[k] != g[k]
                          for k in ("name", "price", "cost", "unit"))
            if changed:
                # `active` is deliberately left alone: a product the owner
                # removed from ОСТАТКИ must not return on the next import.
                con.execute(
                    "UPDATE products SET name=?, price=?, cost=?, unit=?"
                    " WHERE id=?",
                    (g["name"], g["price"], g["cost"], g["unit"], pid))
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

        # "Доп. код" is just one more sticker for the same shelf item.  A code
        # already claimed by a different product is reported, never stolen.
        if g["alt_codes"]:
            try:
                stats["codes"] += len(db.add_codes(con, pid, g["alt_codes"]))
            except ValueError as e:
                stats["code_clashes"].append(f"{g['name']}: {e}")

        if stock_note is not None and g["qty"] is not None:
            if g["qty"] == 0 and not zeros:
                # Leaving these "не посчитан" keeps the ТОЛЬКО НЕ ПОСЧИТАННЫЕ
                # filter useful.  A file where every untracked good says 0
                # would otherwise mark the whole catalogue as counted-empty.
                stats["skipped_zero"] += 1
            elif known_stock == g["qty"]:
                # Already holds exactly this.  Writing it again would put a
                # zero-delta line in the ledger, and running the import twice
                # would leave one such line per product in the table that is
                # supposed to be the truth about stock.
                stats["already"] += 1
            else:
                if known_stock is not None and known_stock != g["qty"]:
                    stats["recounted"].append(
                        (g["name"], known_stock, g["qty"]))
                db.set_stock(con, pid, g["qty"], note=stock_note)
                stats["counted"] += 1

    if deactivate:
        codes = {g["barcode"] for g in goods}
        for row in con.execute("SELECT id, barcode FROM products WHERE active = 1"):
            if row["barcode"] not in codes:
                con.execute("UPDATE products SET active = 0 WHERE id = ?",
                            (row["id"],))
                stats["deactivated"] += 1
    return stats


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        raise SystemExit(__doc__)
    path = Path(args[0])
    if not path.exists():
        raise SystemExit(f"нет файла: {path}")

    goods, problems, cols = read_export(path)
    has_qty = "qty" in cols
    want_stock = "--stock" in flags
    print(f"Файл: {path.name}")
    print(f"Прочитано товаров: {len(goods)}")
    print("Столбцы, которые пригодились: "
          + ", ".join(sorted(cols)))

    no_price = [g for g in goods if g["price"] <= 0]
    no_cost = [g for g in goods if g["cost"] <= 0]
    inverted = [g for g in goods if 0 < g["price"] < g["cost"]]

    stock_note = None
    if has_qty and want_stock:
        stock_note = f"перенос из UMAG, {path.name}"
    elif want_stock and not has_qty:
        print("\n  ! --stock указан, но столбца с количеством в файле нет.")
        print("    Остатки не тронуты. Нужна выгрузка остатков из UMAG.")

    con = db.init()
    stats = apply(con, goods, deactivate="--deactivate" in flags,
                  stock_note=stock_note, zeros="--zero" in flags)
    if "--dry-run" in flags:
        con.rollback()
        print("\n[--dry-run] изменения отменены")
    else:
        con.commit()

    print(f"  добавлено:   {stats['added']}")
    print(f"  обновлено:   {stats['updated']}")
    print(f"  без изменений: {stats['unchanged']}")
    if stats["codes"]:
        print(f"  доп. штрихкодов: {stats['codes']}")
    for clash in stats["code_clashes"]:
        print(f"  ! {clash}")
    if stats["deactivated"]:
        print(f"  скрыто (нет в файле): {stats['deactivated']}")

    if problems:
        print(f"\nПропущено строк: {len(problems)}")
        for n, what, why in problems[:20]:
            print(f"  строка {n}: {what} ({why})")
        if len(problems) > 20:
            print(f"  ... и ещё {len(problems) - 20}")

    print("\nНа что обратить внимание:")
    print(f"  без продажной цены: {len(no_price)}  (продать не получится)")
    print(f"  без закупочной цены: {len(no_cost)}  (прибыль за день будет занижена)")
    if inverted:
        print(f"  цена ниже закупки:  {len(inverted)}")
        for g in inverted[:5]:
            print(f"    {g['name']}: закуп {g['cost']:g} > цена {g['price']:g}")

    if stock_note:
        print(f"  остатки перенесены: {stats['counted']}")
        if stats["already"]:
            print(f"  уже столько же, не трогали: {stats['already']}")
        if stats["skipped_zero"]:
            print(f"  нулевые остатки пропущены: {stats['skipped_zero']}"
                  "  (--zero чтобы перенести и их)")
        if stats["recounted"]:
            print(f"  перебиты уже посчитанные: {len(stats['recounted'])}")
            for name, was, now in stats["recounted"][:5]:
                print(f"    {name}: было {was:g}, стало {now:g}")
    elif has_qty:
        withzero = sum(1 for g in goods if g["qty"] is not None)
        print(f"  в файле есть количество ({withzero} строк), но остатки не тронуты")
        print("    добавьте --stock, чтобы перенести их инвентаризацией")
    else:
        print("  остатки: не заданы (не посчитан), заполняются инвентаризацией")
    con.close()


if __name__ == "__main__":
    main(sys.argv)
