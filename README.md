# Tau Till

A point-of-sale system for a one-till family grocery store in Kazakhstan, written
to replace the proprietary till software the shop had been renting. It runs on the
machine the shop already owns: a touchscreen all-in-one with Windows 7 SP1, 2 GB
of RAM and no keyboard, operated by the author's elderly parents.

Those three facts decided almost every technical choice in here.

**Status:** version 0.5.0 beta, installed in the shop and printing real receipts.

<!-- screenshot placeholder: sale screen at 1024x768 -->

## What it does

- **Selling.** Barcode scanner or search by name, cart with per-line quantity and
  discount, cash or card, change calculation, receipt printed on a 58 mm thermal
  printer over ESC/POS.
- **Shifts.** An operational day is opened and closed by a cashier; closing prints
  a summary, writes a dated backup of the database and produces the day's totals.
- **Stock.** An append-only ledger (`stock_moves`) records every sale, delivery,
  count and correction. `products.stock` is only a cache of that ledger, and a
  `NULL` balance means "never counted", which is different from zero.
- **Products.** Add, edit and price goods from the till itself, several barcodes
  per product, quick-add straight from a failed scan during a sale.
- **Reports.** Day totals, receipts of the day, top products, low stock, all
  exportable to `.xlsx` written by hand in 60 lines because openpyxl cannot be
  installed on that machine.
- **Cashiers.** PIN login, per-cashier attribution on every receipt, an owner
  account that can see money-related screens the cashiers cannot.

Receipts are non-fiscal (товарный чек): fiscal reporting in Kazakhstan happens on
the shop's separate Kaspi terminal, which this software deliberately does not touch.

## Design constraints

**Windows 7, permanently.** The machine cannot be upgraded and will not be
replaced. Python 3.8.10 is the last release whose `python38.dll` does not import
`api-ms-win-core-path-l1-1-0.dll`, an API set Windows 7 does not have, so 3.8 is
the ceiling. The build verifies this rather than trusting it: `pack/peimports.py`
is a small PE parser that walks the import table of every shipped DLL and fails
the build if anything asks for an API set this Windows does not provide.

**No installer, no admin rights.** The bundle ships an embedded CPython plus the
41 Universal C Runtime DLLs extracted from python.org's `ucrt.msi`, placed next to
`python.exe` so the app-local search path finds them. The shop's Windows is never
modified. A copy of KB2999226 rides along as a fallback that has not been needed.

**No keyboard.** Nothing in the install path waits for a keypress; every `.bat`
closes itself on a timer. The UI has its own on-screen numeric and text keyboards,
and no browser-native `alert`, `confirm` or `prompt` appears anywhere, because
those dialogs are small, they are dismissed with a keyboard, and they look like
the virus warnings the operators have learned to be afraid of.

**1024x768, and it never scrolls.** The page is a flex column pinned to the
viewport. Only the cart, the search results and the report tables scroll, each
inside itself, under a sticky header. Every screen state is checked at that exact
resolution with a headless-Chrome harness that screenshots all 15 of them.

**One process, one file.** `server.py` is a `ThreadingHTTPServer` on
`127.0.0.1:8000` with no dependencies outside the standard library, serving one
HTML file and a small JSON API. The whole shop is one SQLite file in WAL mode with
`PRAGMA foreign_keys=ON`, backed up through `sqlite3.Connection.backup()` and
rewritten to `journal_mode=DELETE` so a backup is a single self-contained file you
can carry away on a phone.

## Updates from another city

The author does not live in the shop. The operators cannot be walked through a
terminal over the phone, and driving over to fix a typo is not a support model.

So releasing is `git push`, and updating is one button on the desktop labelled
**ОБНОВИТЬ КАССУ**.

```
pack/release.py 0.5.1 -m "what changed"   # rewrites version.py, hashes every
                                          # app file into update/version.json,
                                          # commits and tags
git push --follow-tags
```

The till reads `update/version.json` over HTTPS from raw.githubusercontent.com,
compares versions, downloads each file and checks it against the sha256 in the
manifest before anything is touched. No tokens, no release assets, no `gh` on that
machine. GitHub's `/archive/` tarballs were rejected because their bytes are not
stable, and a manifest of per-file hashes is stronger anyway: a truncated download
cannot install.

TLS does not rely on Windows 7's root store, which is stale and cannot be updated
without Windows Update. The bundle carries its own `cacert.pem`, and it lives
outside `app/` so an update cannot delete the thing the next update needs.

The update itself is transactional:

1. Download and verify everything into a staging directory. Any failure here and
   the running till never noticed.
2. Copy the database aside.
3. Stop the server, move `app` to `app.prev`, move staging into place.
4. Start the new version and wait for it to answer.
5. If it does not, move `app` to `app.bad`, restore `app.prev`, start that, and
   leave both the failure and the broken version on disk for the next visit.

The point is that the worst realistic outcome of a bad release is that the shop
keeps selling on yesterday's version while the author reads a log.

## Layout

```
server.py         HTTP server and JSON API
db.py             schema, migrations, the stock ledger, backups
index.html        the entire client, one file, no build step
printer.py        ESC/POS receipt composition
winprint.py       RAW printing through winspool.drv via ctypes
xlsx.py           minimal .xlsx writer
version.py        the single place the version number lives

pack/build.py     assembles the Windows bundle
pack/peimports.py PE import reader, the Windows 7 compatibility guard
pack/release.py   version bump, manifest, commit, tag
pack/win/         launcher, supervisor, updater, installer .bat files
```

`pack/win/launch.py` is the piece that turns a Python script into an appliance: it
starts the server with `stdin=DEVNULL` under `CREATE_NO_WINDOW` (without the first
of those, `init_sys_streams` fails on a console it cannot open), waits for the port
to answer, opens Chrome in kiosk mode against it, supervises the process, performs
updates, and writes a plain-language diagnostic report the operators can read to
someone over the phone.

## Running it

Anywhere with Python 3.8 or newer:

```
python3 server.py
```

Then open `http://127.0.0.1:8000`. Without a thermal printer, receipts fail
loudly and the sale is still recorded, which is the correct order of priorities.

Environment: `POS_PORT`, `POS_HOST`, `POS_PRINTER`, `POS_SHOP`, `POS_OWNER`,
`POS_OWNER_PIN`.

Building the Windows bundle needs network access once, to fetch CPython and the
UCRT from python.org, and Pillow if you want the desktop icons redrawn:

```
python3 pack/build.py
```

It produces a full archive of about 10 MB and a 1 MB patch archive for a till that
already has Python, both with `.sha256` files next to them, because a flash drive
in this story has already failed three times.

## Interface language

The UI, the installer messages and most code comments are in Russian, which is
what the people using this software read, and what the release notes shown on the
till have to be written in. This file is in English. Comments explaining a
decision rather than a screen tend to be in English too.
