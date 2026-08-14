#!/usr/bin/env python3
"""Import the UMAG "Список товаров" export into store.db.

    python3 import_umag.py "Список товаров_export_13.08.2026 18_02.xlsx"
    python3 import_umag.py file.xlsx --dry-run     # report only, no writes
    python3 import_umag.py file.xlsx --deactivate  # hide goods absent from file

Matching is by barcode.  Known goods get their name and prices refreshed;
new goods are inserted with stock = NULL ("не посчитан") so that nothing
claims a quantity the shop has not physically counted.  Stock is never
touched by an import -- only приёмка and инвентаризация move it.
"""
import sys
from pathlib import Path

import db
import xlsx

# The export's column headers, in the order UMAG writes them.
COLUMNS = ["Название товара", "Штрихкод", "Доп. код",
           "Закуп. цена", "Прод. цена", "Ед. изм"]


def number(s):
    """UMAG writes plain integers, but tolerate a comma decimal separator."""
    s = (s or "").strip().replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_export(path):
    """Return (goods, problems).  Rejects rows that cannot be sold safely."""
    rows = iter(xlsx.rows(path))
    header = [h.strip() for h in next(rows)]
    if header[:len(COLUMNS)] != COLUMNS:
        raise SystemExit(
            "Неожиданные столбцы в файле:\n"
            f"  ожидалось: {COLUMNS}\n  получено:  {header}")

    goods, problems, seen = [], [], {}
    for n, row in enumerate(rows, start=2):
        row = (row + [""] * len(COLUMNS))[:len(COLUMNS)]
        name, barcode, alt, cost, price, unit = (c.strip() for c in row)
        if not any(row):
            continue
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
            "alt_code": alt or None,
            "name": name,
            "cost": number(cost),
            "price": number(price),
            "unit": unit or "шт",
        })
    return goods, problems


def apply(con, goods, deactivate=False):
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deactivated": 0,
             "codes": 0, "code_clashes": []}
    for g in goods:
        row = con.execute("SELECT * FROM products WHERE barcode = ?",
                          (g["barcode"],)).fetchone()
        if row is None:
            cur = con.execute(
                "INSERT INTO products (barcode, name, price, cost,"
                " unit, stock) VALUES (?,?,?,?,?,NULL)",
                (g["barcode"], g["name"], g["price"], g["cost"], g["unit"]))
            pid = cur.lastrowid
            stats["added"] += 1
        else:
            pid = row["id"]
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
        if g["alt_code"]:
            try:
                stats["codes"] += len(db.add_codes(con, pid, [g["alt_code"]]))
            except ValueError as e:
                stats["code_clashes"].append(f"{g['name']}: {e}")

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

    goods, problems = read_export(path)
    print(f"Файл: {path.name}")
    print(f"Прочитано товаров: {len(goods)}")

    no_price = [g for g in goods if g["price"] <= 0]
    no_cost = [g for g in goods if g["cost"] <= 0]
    inverted = [g for g in goods if 0 < g["price"] < g["cost"]]

    con = db.init()
    stats = apply(con, goods, deactivate="--deactivate" in flags)
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
    print("  остатки: не заданы (не посчитан), заполняются инвентаризацией")
    con.close()


if __name__ == "__main__":
    main(sys.argv)
