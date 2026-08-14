#!/usr/bin/env python3
"""Print a sample товарный чек to validate layout against the real printer.

Uses the exact basket from the UMAG screenshot so the output can be compared
side by side.  Run with --preview to dump to the terminal instead of paper.

    python3 demo_print.py --preview
    python3 demo_print.py            # needs lp group membership, else sudo
"""
import sys
from datetime import datetime

from printer import sale_receipt

def item(name, qty, price, disc=0.0):
    line = round(qty * price, 2)
    return {"name": name, "qty": qty, "unit": "шт", "price": price,
            "disc": disc, "discount": round(line * disc / 100, 2)}


ITEMS = [
    item("пенал", 1, 900.00),
    item("графит карандаш", 1, 35.00),
    item("мел антошка", 1, 100.00),
    item("Бумага офисная А4 класс с, svetocopy, 500 листов", 6, 2700.00),
    item("Универсальный продукт", 1, 660.00, disc=5.0),
]

TOTAL = round(sum(i["qty"] * i["price"] - i["discount"] for i in ITEMS), 2)

r = sale_receipt(
    items=ITEMS,
    total=TOTAL,
    number="0001",
    when=datetime.now(),
    cashier="Акмарал",
    shop="МАГАЗИН",
    payment="БАНКОВСКОЙ КАРТОЙ",
)

if "--preview" in sys.argv:
    # Decode back to show the 32-column layout as it will land on paper.
    for line in r.bytes().split(b"\n"):
        stripped = bytearray()
        i = 0
        while i < len(line):  # drop ESC/GS control sequences for readability
            if line[i : i + 1] == b"\x1b" and line[i + 1 : i + 2] == b"@":
                i += 2  # ESC @ is two bytes; the rest below are three
                continue
            if line[i : i + 1] in (b"\x1b", b"\x1d"):
                i += 3
                continue
            stripped.append(line[i])
            i += 1
        print("|" + stripped.decode("cp866", errors="replace").ljust(32) + "|")
else:
    r.send()
    print(f"printed, total {TOTAL:.2f}")
