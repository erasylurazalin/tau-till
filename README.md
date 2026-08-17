# Tau Till

Cash register software for my parents' grocery store in Kazakhstan. It replaces
the proprietary till they had been renting.

It runs on the computer the shop already owns: a touchscreen all-in-one with
Windows 7, 2 GB of RAM and no keyboard, operated by my elderly parents. That is
the entire design brief, and it decided most of what is below.

**Status:** v0.5.5 beta, installed in the shop, printing real receipts.

## What it does

Scan a barcode or search by name, build a cart, take cash or card, print a
receipt on a 58 mm thermal printer. Open and close the operational day. Track
stock as an append-only ledger. Add and price products from the till itself.
Day totals, top products, low stock, all exportable to `.xlsx`. PIN login per
cashier, plus an owner account that sees the money screens the others don't.

## Constraints

**Windows 7 forever.** That machine is not getting upgraded.

**No admin rights.** The bundle carries its own CPython and the UCRT next to it,
so nothing gets installed into the shop's Windows.

**No keyboard.** Nothing waits for a keypress, and the UI has its own on-screen
keyboards. No browser `alert`, `confirm` or `prompt` anywhere.

**One process, one file.** `server.py` is a stdlib `ThreadingHTTPServer` on
localhost serving a single HTML file. The whole shop is one SQLite file.

## Layout

```
server.py     HTTP server and JSON API      printer.py   ESC/POS receipts
db.py         schema, stock ledger, backups winprint.py  RAW printing on Windows
index.html    the whole client, no build    xlsx.py      minimal .xlsx writer
pack/         the Windows bundle, release manifest, launcher and installer
```

## Running it

```
python3 server.py
```

Then open `http://127.0.0.1:8000`. Python 3.8 or newer, no dependencies. Without
a thermal printer the receipt fails loudly and the sale is still recorded.

To build the Windows bundle:

```
python3 pack/build.py
```

It downloads CPython and the UCRT.

## Language

The UI, the installer and most comments are in Russian, which is what the people
using this read.
