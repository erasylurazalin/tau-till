"""SQLite schema and access helpers.

One file, no server, no dependencies.  See backup() for how copies are taken:
with WAL on, store.db on its own is not the whole database.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

# POS_DB lets a test run point at a throwaway copy instead of the shop's data.
DB_PATH = Path(os.environ.get("POS_DB") or Path(__file__).parent / "store.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id        INTEGER PRIMARY KEY,
    barcode   TEXT UNIQUE,              -- NULL allowed: weighed/unbarcoded goods
    name      TEXT NOT NULL,
    price     REAL NOT NULL DEFAULT 0,  -- selling price
    cost      REAL NOT NULL DEFAULT 0,  -- purchase price, drives profit
    unit      TEXT NOT NULL DEFAULT 'шт',
    -- NULL means "not counted yet".  Imported goods start unknown rather than
    -- zero: SQLite makes NULL + delta = NULL, so selling an uncounted item can
    -- never invent a negative balance, and the low-stock report skips it.
    stock     REAL,
    min_stock REAL NOT NULL DEFAULT 0,  -- "critical stock" threshold
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
""" + (BARCODES := """
-- Extra stickers for the same shelf item.  The same тетрадь in three cover
-- designs is one product but three different barcodes, so the codes live in
-- their own table rather than in a fixed "second code" column.
CREATE TABLE IF NOT EXISTS barcodes (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    code       TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_barcodes_product ON barcodes(product_id);
""") + """

-- Who may open a day.  The owner is the only one who reaches the
-- admin side, and the only one who cannot be deleted -- otherwise the store
-- could lock itself out of its own stock and pricing.
CREATE TABLE IF NOT EXISTS cashiers (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    pin      TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0
);

-- An operational day.  Opened when the cashier logs in, closed from the ДЕНЬ
-- screen.  Totals are snapshotted at closing time so a past day's report never
-- changes afterwards, whatever happens to prices or products later.
CREATE TABLE IF NOT EXISTS shifts (
    id        INTEGER PRIMARY KEY,
    cashier   TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,                     -- NULL while the day is still running
    receipts  INTEGER,
    revenue   REAL,
    cash      REAL,
    card      REAL,
    profit    REAL,
    discount  REAL
);
CREATE INDEX IF NOT EXISTS idx_shifts_open ON shifts(closed_at);

CREATE TABLE IF NOT EXISTS receipts (
    id        INTEGER PRIMARY KEY,
    shift_id  INTEGER REFERENCES shifts(id),
    number    TEXT NOT NULL,
    ts        TEXT NOT NULL,            -- ISO8601 local time
    cashier   TEXT NOT NULL,
    payment   TEXT NOT NULL,            -- КАРТОЙ | НАЛИЧНЫМИ | СМЕШАННАЯ
    total     REAL NOT NULL,
    paid_card REAL NOT NULL DEFAULT 0,  -- charged on the terminal
    paid_cash REAL NOT NULL DEFAULT 0,  -- kept in the drawer (given - change)
    given     REAL NOT NULL DEFAULT 0,  -- cash the customer handed over
    change    REAL NOT NULL DEFAULT 0,
    discount  REAL NOT NULL DEFAULT 0,  -- total taken off this cheque
    -- A cancelled cheque is kept, never deleted: every total already filters
    -- on voided = 0, and who cancelled what is exactly what you want to see
    -- later when the drawer does not add up.
    voided    INTEGER NOT NULL DEFAULT 0,
    voided_at TEXT,
    voided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_ts ON receipts(ts);

CREATE TABLE IF NOT EXISTS receipt_items (
    id         INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    product_id INTEGER REFERENCES products(id),
    name       TEXT NOT NULL,           -- snapshot: renaming a product later
    qty        REAL NOT NULL,           -- must not rewrite past receipts
    price      REAL NOT NULL,           -- full price, before any discount
    cost       REAL NOT NULL DEFAULT 0, -- snapshot too, so profit stays honest
    disc       REAL NOT NULL DEFAULT 0, -- discount percentage, for the cheque
    discount   REAL NOT NULL DEFAULT 0  -- tenge taken off this line
);
CREATE INDEX IF NOT EXISTS idx_items_receipt ON receipt_items(receipt_id);

-- Append-only ledger.  products.stock is a cache of this; the ledger is truth.
CREATE TABLE IF NOT EXISTS stock_moves (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    delta      REAL NOT NULL,           -- negative = out
    kind       TEXT NOT NULL,           -- sale | receive | adjust
    ref        TEXT,                    -- receipt number, invoice no, note
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_moves_ts ON stock_moves(ts);

-- Quick buttons on the sale screen, for what sells often and does not sit on
-- a shelf with a sticker: printing, photocopies, a bag.  An item either
-- points at a catalogue product or stands on its own with just a name and a
-- price, the way the универсальный товар line does.
CREATE TABLE IF NOT EXISTS quick_groups (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pos  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quick_items (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES quick_groups(id) ON DELETE CASCADE,
    -- SET NULL rather than CASCADE: deleting a product should not silently
    -- take the button away, it should leave it standing on its stored name
    -- and price for the owner to fix or remove on purpose.
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    name       TEXT NOT NULL,
    price      REAL NOT NULL DEFAULT 0,
    pos        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quick_items_group ON quick_items(group_id);
"""

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")  # survives an unclean power cut
    # SQLite's built-in LIKE/LOWER only case-fold ASCII, so "бумага" would
    # never match "Бумага".  Hand the job to Python, which knows Unicode.
    con.create_function("lower_u", 1, lambda s: s.lower() if s else s,
                        deterministic=True)
    return con


def ensure_column(con, table, column, decl):
    """Add a column if an older store.db predates it.  SQLite has no
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so check the schema first.
    A table that does not exist yet needs nothing -- SCHEMA will create it."""
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    if have and column not in have:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def migrate(con):
    for col, decl in (("paid_card", "REAL NOT NULL DEFAULT 0"),
                      ("paid_cash", "REAL NOT NULL DEFAULT 0"),
                      ("given",     "REAL NOT NULL DEFAULT 0"),
                      ("change",    "REAL NOT NULL DEFAULT 0"),
                      ("discount",  "REAL NOT NULL DEFAULT 0"),
                      ("voided_at", "TEXT"),
                      ("voided_by", "TEXT")):
        ensure_column(con, "receipts", col, decl)
    for col, decl in (("disc",     "REAL NOT NULL DEFAULT 0"),
                      ("discount", "REAL NOT NULL DEFAULT 0")):
        ensure_column(con, "receipt_items", col, decl)
    ensure_column(con, "receipts", "shift_id", "INTEGER REFERENCES shifts(id)")
    ensure_column(con, "shifts", "discount", "REAL")

    # alt_code could hold exactly one extra sticker; the barcodes table holds
    # any number.  Move the old values across and retire the column.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(products)")}
    if "alt_code" in cols:
        con.executescript(BARCODES)
        con.execute(
            "INSERT OR IGNORE INTO barcodes (product_id, code)"
            " SELECT id, TRIM(alt_code) FROM products"
            " WHERE alt_code IS NOT NULL AND TRIM(alt_code) <> ''")
        con.execute("DROP INDEX IF EXISTS idx_products_alt")
        con.execute("ALTER TABLE products DROP COLUMN alt_code")
        con.commit()

    # products.stock used to be NOT NULL DEFAULT 0, which cannot express "not
    # counted yet".  SQLite can't relax a constraint in place, so rebuild the
    # table.  Existing balances are kept; only the constraint changes.
    info = {r["name"]: r for r in con.execute("PRAGMA table_info(products)")}
    if info and info["stock"]["notnull"]:
        con.commit()
        con.execute("PRAGMA foreign_keys = OFF")  # ignored inside a transaction
        con.executescript("""
            CREATE TABLE products_new (
                id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, alt_code TEXT,
                name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'шт',
                stock REAL, min_stock REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1);
            INSERT INTO products_new
                SELECT id, barcode, alt_code, name, price, cost, unit, stock,
                       min_stock, active FROM products;
            DROP TABLE products;
            ALTER TABLE products_new RENAME TO products;
        """)
        con.commit()
        con.execute("PRAGMA foreign_keys = ON")


# Владелец, которого касса заводит при первом запуске на пустой базе.
# Имя и код берутся из настроек магазина, а не из кода: репозиторий открытый,
# и коду доступа владельца в нём не место.  На уже работающей кассе это ничего
# не меняет, потому что запись создаётся только когда кассиров нет вообще.
OWNER = (os.environ.get("POS_OWNER") or "ХОЗЯИН",
         os.environ.get("POS_OWNER_PIN") or "0000")


def init():
    con = connect()
    migrate(con)              # bring an older file up to the current shape
    con.executescript(SCHEMA)  # then create whatever is still missing
    if not con.execute("SELECT 1 FROM cashiers LIMIT 1").fetchone():
        con.execute("INSERT INTO cashiers (name, pin, is_owner) VALUES (?,?,1)",
                    OWNER)
    con.commit()
    return con


# --- backup ---------------------------------------------------------------
# The whole shop is one file, so a copy of it is a complete restore point:
# stop the server, put the copy back as store.db, start again.
BACKUP_DIR = DB_PATH.parent / "backup"
BACKUP_KEEP = 30      # one per closed day, so roughly a month of history
BACKUP_GLOB = "store-*.db"


def backup(con, when=None, keep=BACKUP_KEEP):
    """Write a consistent copy of the database and prune old ones.

    Uses SQLite's own backup API rather than copying the file: with WAL on,
    store.db by itself is not the whole database (recent writes may still be
    in store.db-wal), and a plain copy taken mid-write can be torn.  The API
    copies page by page under a read lock, so the result always opens clean.

    Returns the Path written.  Only files this function made are pruned --
    hand-made snapshots in the same folder are left alone.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"store-{stamp}.db"
    out = sqlite3.connect(dest)
    try:
        con.backup(out)
        # The copy inherits WAL from the source, which would leave a -wal file
        # beside it.  A backup has to be one self-contained file you can drop
        # on a USB stick, so fold the log back in and switch it off.
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()
    # The timestamp sorts the same way lexically as chronologically.
    old = sorted(BACKUP_DIR.glob(BACKUP_GLOB))[:-keep] if keep else []
    for f in old:
        try:
            f.unlink()
        except OSError:
            pass  # a copy we cannot delete is not a reason to fail the day
    return dest


# --- cashiers -------------------------------------------------------------
def find_cashier(con, name):
    return con.execute("SELECT * FROM cashiers WHERE name = ?",
                       ((name or "").strip().upper(),)).fetchone()


def owner(con):
    return con.execute("SELECT * FROM cashiers WHERE is_owner = 1"
                       " ORDER BY id LIMIT 1").fetchone()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# --- barcodes -------------------------------------------------------------
def find_by_code(con, code):
    """A product by any of its stickers -- the main one or an extra."""
    code = (code or "").strip()
    if not code:
        return None
    row = con.execute("SELECT * FROM products WHERE barcode = ?",
                      (code,)).fetchone()
    if row is None:
        row = con.execute(
            "SELECT p.* FROM products p JOIN barcodes b ON b.product_id = p.id"
            " WHERE b.code = ?", (code,)).fetchone()
    return row


def codes_of(con, product_id):
    return [r["code"] for r in con.execute(
        "SELECT code FROM barcodes WHERE product_id = ? ORDER BY id",
        (product_id,))]


def add_codes(con, product_id, codes):
    """Attach extra barcodes, refusing any that belong to something else.

    Returns the codes actually added; already-attached ones are skipped
    silently, since re-scanning a sticker the item already has is harmless.
    """
    added = []
    for code in codes:
        code = (code or "").strip()
        if not code:
            continue
        owner = find_by_code(con, code)
        if owner and owner["id"] != product_id:
            raise ValueError(
                f"штрихкод {code} уже принадлежит товару «{owner['name']}»")
        if owner:
            continue                      # already this product's own sticker
        con.execute("INSERT INTO barcodes (product_id, code) VALUES (?,?)",
                    (product_id, code))
        added.append(code)
    return added


# --- operational day ------------------------------------------------------
def quick_menu(con):
    """Categories with their items, ready for the sale screen.

    A linked item is shown with the catalogue's current name and price, not
    with the copy stored here.  A price changed in ОСТАТКИ has to reach the
    button, otherwise the till would quietly keep selling at last month's
    price.  The stored copy is only the fallback for an item whose product
    has since been deleted.
    """
    groups = []
    for g in con.execute("SELECT * FROM quick_groups ORDER BY pos, id"):
        items = []
        for r in con.execute(
                "SELECT q.*, p.name AS pname, p.price AS pprice,"
                " p.unit AS punit, p.stock AS pstock, p.active AS pactive"
                " FROM quick_items q"
                " LEFT JOIN products p ON p.id = q.product_id"
                " WHERE q.group_id = ? ORDER BY q.pos, q.id", (g["id"],)):
            linked = r["product_id"] is not None
            items.append({
                "id": r["id"],
                "product_id": r["product_id"],
                "name": r["pname"] if linked else r["name"],
                "price": r["pprice"] if linked else r["price"],
                "unit": r["punit"] if linked else "шт",
                "stock": r["pstock"] if linked else None,
                "linked": linked,
            })
        groups.append({"id": g["id"], "name": g["name"], "items": items})
    return groups


def next_pos(con, table, where="", args=()):
    """Куда дописать следующую кнопку, чтобы она встала в конец."""
    row = con.execute(
        "SELECT COALESCE(MAX(pos), -1) + 1 AS n FROM %s %s" % (table, where),
        args).fetchone()
    return row["n"]


def current_shift(con):
    """The open day, or None.  One till, so at most one is ever open."""
    return con.execute(
        "SELECT * FROM shifts WHERE closed_at IS NULL"
        " ORDER BY id DESC LIMIT 1").fetchone()


def open_shift(con, cashier):
    cur = con.execute("INSERT INTO shifts (cashier, opened_at) VALUES (?,?)",
                      (cashier, now_iso()))
    con.commit()
    return con.execute("SELECT * FROM shifts WHERE id=?",
                       (cur.lastrowid,)).fetchone()


def shift_totals(con, shift_id):
    """Live figures for one operational day."""
    head = con.execute(
        "SELECT COUNT(*) receipts, COALESCE(SUM(total),0) revenue,"
        " COALESCE(SUM(paid_cash),0) cash, COALESCE(SUM(paid_card),0) card,"
        " COALESCE(SUM(discount),0) discount"
        " FROM receipts WHERE voided = 0 AND shift_id = ?", (shift_id,)).fetchone()
    # Margin is what the goods earned minus what was given away, otherwise a
    # discount would quietly show up as profit that never reached the drawer.
    profit = con.execute(
        "SELECT COALESCE(SUM((i.price - i.cost) * i.qty - i.discount), 0) p"
        " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
        " WHERE r.voided = 0 AND r.shift_id = ?", (shift_id,)).fetchone()["p"]
    return {"receipts": head["receipts"], "revenue": round(head["revenue"], 2),
            "cash": round(head["cash"], 2), "card": round(head["card"], 2),
            "discount": round(head["discount"], 2), "profit": round(profit, 2)}


def close_shift(con, shift_id):
    """Freeze the day's totals into the shift row and stamp the closing time."""
    t = shift_totals(con, shift_id)
    con.execute(
        "UPDATE shifts SET closed_at = ?, receipts = ?, revenue = ?, cash = ?,"
        " card = ?, profit = ?, discount = ? WHERE id = ?",
        (now_iso(), t["receipts"], t["revenue"], t["cash"], t["card"],
         t["profit"], t["discount"], shift_id))
    con.commit()
    return con.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()


def next_receipt_number(con, shift_id):
    """Sequential within the operational day: 0001, 0002, ...

    Numbered per shift rather than per calendar date, so a day that runs past
    midnight keeps counting instead of restarting at 0001 mid-evening.
    """
    n = con.execute("SELECT COUNT(*) c FROM receipts WHERE shift_id = ?",
                    (shift_id,)).fetchone()["c"]
    return f"{n + 1:04d}"


def move_stock(con, product_id, delta, kind, ref=None, note=None):
    """Write the ledger entry and update the cached balance together.

    A product that has never been counted has stock = NULL, and NULL + delta
    stays NULL in SQLite: selling or receiving an uncounted item records the
    move in the ledger but does not pretend to know a balance.  Counting it
    (set_stock) is what turns the balance into a real number.
    """
    con.execute(
        "INSERT INTO stock_moves (ts, product_id, delta, kind, ref, note)"
        " VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), product_id, delta,
         kind, ref, note))
    con.execute("UPDATE products SET stock = stock + ? WHERE id = ?",
                (delta, product_id))


def set_stock(con, product_id, qty, note="инвентаризация"):
    """Инвентаризация: assert the counted quantity as the new truth.

    Logged as the delta from whatever was believed before, so the ledger still
    adds up.  For a never-counted product the whole quantity is the delta.
    """
    before = con.execute("SELECT stock FROM products WHERE id = ?",
                         (product_id,)).fetchone()["stock"]
    delta = qty - (before or 0)
    con.execute(
        "INSERT INTO stock_moves (ts, product_id, delta, kind, note)"
        " VALUES (?,?,?,'count',?)",
        (datetime.now().isoformat(timespec="seconds"), product_id, delta, note))
    con.execute("UPDATE products SET stock = ? WHERE id = ?", (qty, product_id))
    return {"before": before, "after": qty, "delta": delta}


if __name__ == "__main__":
    init()
    print(f"initialised {DB_PATH}")
