"""SQLite schema and access helpers.

One file, no server, no dependencies.  See backup() for how copies are taken:
with WAL on, store.db on its own is not the whole database.
"""
import json
import os
import re
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

-- Отложенные чеки.  Покупатель ушёл за забытым товаром, а очередь стоит: чек
-- убирается в сторону и достаётся обратно, когда он вернётся.
--
-- Корзина лежит здесь как JSON, а не разложенная по строкам, намеренно.  Это
-- черновик, а не бухгалтерия: он никуда не отчитывается, ни в один отчёт не
-- попадает и живёт до того момента, как его заберут обратно.  Разбирать его
-- на таблицу значило бы обещать про него больше, чем есть.
CREATE TABLE IF NOT EXISTS parked (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    cashier TEXT,
    label   TEXT NOT NULL,
    items   TEXT NOT NULL
);
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
    seed_quick(con)
    split_joined_codes(con)
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
    """A product by any of its stickers -- the main one or an extra.

    Ведущие нули сравниваются нестрого, и это не вольность.  Выгрузка UMAG
    приходит таблицей Excel, а Excel держит штрихкод числом: 098006319813
    доезжает до базы как 98006319813.  Наклейка на товаре при этом осталась
    прежней, сканер честно присылает все тринадцать знаков, точное сравнение
    не находит ничего, и касса предлагает завести товар, который у неё уже
    есть.  Так потерялись все коды, начинавшиеся с нуля.

    Сначала всё-таки точное совпадение: оно ходит по индексу и покрывает
    подавляющее большинство сканирований.  Нестрогое ищется только когда
    точного не нашлось.
    """
    code = (code or "").strip()
    if not code:
        return None
    exact = ("SELECT * FROM products WHERE barcode = ?",
             "SELECT p.* FROM products p JOIN barcodes b ON b.product_id = p.id"
             " WHERE b.code = ?")
    for sql in exact:
        row = con.execute(sql, (code,)).fetchone()
        if row is not None:
            return row

    bare = code.lstrip("0") or "0"
    loose = ("SELECT * FROM products WHERE ltrim(barcode, '0') = ?",
             "SELECT p.* FROM products p JOIN barcodes b ON b.product_id = p.id"
             " WHERE ltrim(b.code, '0') = ?")
    for sql in loose:
        row = con.execute(sql, (bare,)).fetchone()
        if row is not None:
            return row
    return None


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
# Категории быстрых товаров, перенесённые с экрана старой программы: тот же
# набор и тот же порядок, чтобы хозяйке не пришлось искать заново.  Порядок
# там алфавитный, а БРЕЛОК дописан в конец, видимо позже остальных, и мы это
# сохраняем.  ЛИСТ, КУБИКАМИ и ВЫБРАТЬ НЕСКОЛЬКО с того экрана не переносятся:
# это органы управления самой программы, а не категории товара.
#
# Категории заводятся пустыми.  Что лежит внутри каждой, из выгрузки UMAG
# восстановить нельзя: там своя, складская разбивка, и 85 % товаров в ней
# «Незаданные».
QUICK_SEED = [
    "АНИМЕ", "БЕЙДЖИКИ", "ВАТМАНЫ", "ВСЕ ДЛЯ ПРАЗДНИКА",
    "ДЕЛО ТРУДОВАЯ ПРИВИВОЧНАЯ ОФИСНАЯ",
    "ИГРУШКИ", "КОНВЕРТЫ ПАКЕТЫ", "КОСМЕТИКА КРАБИК КОЛЬЦА", "КСЕРОКОПИЯ",
    "ЛИНЕЙКИ", "МЕЛОЧИ", "ОБЛОЖКИ", "РАСКРАСКИ", "РУЧКИ КАРАНДАШИ", "СКОТЧ",
    "СТИКЕРЫ", "ФЛЕШКИ", "ЧЕК ЛИСТЫ И ЦЕННИКИ", "ШАРИКИ", "БРЕЛОК",
]
# Что лежит в каждой категории, перенесено с экрана старой программы по
# фотографиям.  Ключ это штрихкод: имена в каталоге меняются, штрихкод нет.
# Второе поле в паре только для чтения глазами, при посеве оно не используется.
#
# Кнопка привязывается к товару каталога, а не копирует его имя и цену: цена
# на кассе меняется в ОСТАТКАХ, и кнопка должна меняться вместе с ней.
QUICK_ITEMS = {
    "АНИМЕ": [
        ("4972150863490", "шопер"),
        ("055015008503", "карточка штучно"),
        ("4172390568210", "карточки 200"),
        ("2009660806838", "протектор штучно"),
        ("1267459308517", "постер"),
    ],
    "БЕЙДЖИКИ": [
        ("2109563784018", "Бейдж на проезд по 200"),
        ("6932150478108", "Бейджик синий по 250"),
        ("1234567890128", "бейджик по 300"),
    ],
    "ВАТМАНЫ": [
        ("4607090582957", "ватман гознак а1 400т"),
        ("066010348297", "ватман А2"),
        ("5029721684", "ватман А3"),
        ("4607112470583", "Ватман А4"),
    ],
    "ВСЕ ДЛЯ ПРАЗДНИКА": [
        ("4870000050109", "КОНВЕРТ ДЕНЕЖН 200т"),
        ("4870007700564", "конверт денежный 250"),
        ("034013604887", "открытка50т"),
        ("044016326501", "открытка 100т"),
        ("7580249134627", "подарочная бумага"),
        ("8320649172534", "подарочная бумага"),
        ("7008909304", "подарочная бумага"),
        ("3846709512133", "Колпак Д/Р"),
        ("9581463275019", "почтовая открытка"),
        ("5281043769153", "упаков.бумага 300т"),
        ("8972631045205", "колпакДР"),
        ("2694059000005", "мишура"),
    ],
    "ДЕЛО ТРУДОВАЯ ПРИВИВОЧНАЯ ОФИСНАЯ": [
        ("4209675312081", "Прививочный паспорт"),
        ("5410289362075", "скросшиватель дела 250"),
        ("5769048132467", "Медицинская книжка 250"),
        ("5870362194359", "Трудовая книжка 250"),
        ("031028982498", "алгыс хат"),
        ("9630842751092", "Паспорт здоровья 600"),
        ("20209476", "листок учета/ табель"),
        ("3945281076542", "автобиография"),
    ],
    "ИГРУШКИ": [
        ("6972516689564", "гравюра"),
        ("056015709995", "сотка музыкальная"),
        ("2086315749207", "жираф"),
        ("2015096214", "фонарик лазер"),
        ("6972575210013", "пазл"),
        ("4059726813094", "фонарик"),
        ("6479103251789", "ПАЗЛ"),
        ("7395182406577", "ПАЗЛ"),
        ("8163204579832", "мыльные пузыри"),
        ("1207569345088", "мыльные пузыри"),
        ("5730198263455", "тарелка с липучкой"),
        ("7206351894272", "вентилятор"),
        ("8934025613070", "орбизы"),
        ("5296308147560", "орбизы"),
        ("1792630453811", "мячик"),
        ("8473295610124", "мячик"),
        ("4623819507438", "фишки"),
        ("2024080873550", "тик ток мяч"),
        ("2562654000006", "вертушка"),
        ("2357469000002", "алмазная мозайка"),
    ],
    "КОНВЕРТЫ ПАКЕТЫ": [
        ("5682314901256", "Евро конверт"),
        ("3456872109636", "Конверт С6"),
        ("7816234902750", "конверт бел с4 150т"),
        ("8904125736191", "конверт с5"),
        ("4673748553044", "крафт конверт С6"),
        ("029014447267", "открытка"),
        ("098006319813", "пакет"),
        ("069007341462", "пакет 200т"),
        ("1004621871", "пакет 320т"),
        ("9807453621575", "пакет 300т"),
        ("6894750231562", "пакет 150т"),
        ("4127590368252", "пакет 350"),
        ("2000996796954", "пакет"),
        ("2119650797920", "пакет"),
        ("2119650797838", "пакет"),
        ("6920205123103", "пакет"),
        ("9408156723902", "открытка"),
        ("4870238936633", "конверт А3"),
    ],
    "КОСМЕТИКА КРАБИК КОЛЬЦА": [
        ("063008943120", "крабик"),
        ("6972664130666", "крабик"),
        ("054008766710", "кольцо детское"),
        ("1945372865019", "невидимки"),
        ("6458917209346", "крабик"),
        ("3074892654310", "резинка для волос спираль"),
        ("8025643194789", "лак для ногтей"),
        ("5638912074953", "крабики резинки"),
        ("4596812735408", "парные кулоны"),
        ("3659127485305", "крабик"),
        ("3196470582348", "ногти"),
        ("1697320451873", "клей для ногтей"),
        ("3064721589401", "кольцо 1400"),
        ("4816957230645", "крабик"),
        ("2561609000009", "невидимки/резинка для волос"),
        ("2966731000000", "лак для ногтей"),
        ("2110000004606", "детские ногти"),
    ],
    "КСЕРОКОПИЯ": [
        ("7928135064298", "бумага А4 20т"),
        ("012016976439", "распечатка/ксерокс"),
        ("022026254779", "цвет распечатка /ксерокс"),
        ("032002706918", "самоклейка распечатка"),
        ("042006847296", "сканирование"),
        ("052017859447", "распечатка/ксерокс"),
        ("072002305063", "файл 60мкр"),
        ("062006406095", "файл 40мкр"),
        ("071010571026", "файл 80мкр"),
        ("023017240597", "цветная распечатка 150т"),
        ("016026790368", "файл А5 штучно"),
        ("6926662310117", "файл а3 150т"),
        ("7210893462158", "файл 100мкр"),
        ("3741250968319", "фото"),
        ("2110000106102", "фото 3х4 6шт"),
    ],
    "ЛИНЕЙКИ": [
        ("6960108620231", "Транспортир"),
        ("4601822000016", "ЛИНЕЙКА"),
        ("4680211172626", "линейка 15 см"),
        ("6926644733101", "Транспортир пластик"),
    ],
    "МЕЛОЧИ": [
        ("020006001337", "переходник"),
        ("6173928045614", "флаг Казахстан"),
        ("5017269384029", "пряжа"),
        ("014028010262", "фоторамка А4"),
        ("057029045925", "стакан непроливайка"),
        ("9165403872157", "одноразовый стакан"),
        ("9384705123860", "ластик Maped"),
        ("6926646801327", "точилки"),
        ("4620000638117", "Стакан-непроливайка двойной, ассорти тонир."),
        ("2000036912788", "леска"),
        ("2000036911644", "ластик 250"),
        ("6930526590928", "ластик мапед"),
        ("6976301202457", "указка тик ток"),
        ("8974102365041", "флаг Казахстан"),
        ("7269518032849", "таспих"),
        ("3452889170249", "Липучка"),
        ("9415837026498", "дет маска"),
        ("4059236812709", "игла для насоса"),
        ("6305794821034", "CD диск"),
        ("6975199668963", "термоклей штучно"),
        ("2009650794367", "значки"),
        ("2037584162952", "пенал"),
        ("9058437219602", "карона"),
        ("6310548729030", "крючок для вязания"),
        ("20150174", "Карандаш"),
    ],
    "ОБЛОЖКИ": [
        ("079010918049", "обложка тетрадь40тенге"),
        ("2371408695432", "обложки150"),
        ("3158792045673", "обложки 100т"),
        ("9523761408249", "набор обложки"),
        ("6946350055014", "ОБЛОЖКА Д ТЕТРАДЕЙ И ДНЕВНИКОВ"),
    ],
    "РУЧКИ КАРАНДАШИ": [
        ("026007032707", "ручка DOMS60тенге"),
        ("036002731178", "ручка Қалам50тенге"),
        ("2112345688886", "РУЧКА айзере"),
        ("6932784201882", "Ручка Ellott 1mm"),
        ("2890154360796", "ручка"),
        ("1345209786423", "текстовыделитель"),
        ("4870254061241", "Ручка DOMs"),
    ],
    "СКОТЧ": [
        ("4607112470606", "скотч качественный"),
        ("060024943134", "скотч"),
        ("4759081267432", "скотч"),
        ("28110262", "скотч"),
        ("7689154230865", "скотч маленький"),
        ("2009660800775", "скотч"),
        ("4156723985103", "СКОТЧ"),
        ("6976045800148", "скотч"),
        ("4256285632276", "скотч"),
    ],
    "СТИКЕРЫ": [
        ("6342075196481", "стикеры"),
    ],
    "ФЛЕШКИ": [
        ("2000000571720", "флешкаЭЦП 1000Т"),
        ("4601135635622", "флешка 2Г"),
        ("4607082994256", "флешка 64г"),
        ("017018882399", "флешка 4Г"),
        ("047018788803", "флешка 8Г"),
        ("6021468737", "флешка 32Г"),
        ("849198003963", "батарейки LR44"),
        ("6979546422345", "Юзб Флешка 32 Г"),
    ],
    "ЧЕК ЛИСТЫ И ЦЕННИКИ": [
        ("089015103890", "чек лента 8 900т"),
        ("099004190374", "чек лента"),
        ("4607178600771", "ценник"),
        ("6927092254385", "чек лента"),
    ],
    "ШАРИКИ": [
        ("033013054715", "шарик 50"),
        ("043022599275", "шарик 30т"),
        ("84495051", "шарик 150"),
    ],
    "БРЕЛОК": [
        ("4000182049", "брелок кошка"),
        ("2734961508722", "брелок"),
        ("4071265893146", "брелок"),
        ("6972081867688", "брелок"),
        ("6973705178692", "сумка брелок"),
        ("6971551887119", "Брелок"),
        ("4607014057004", "Брелок прозрачный"),
        ("2110000001193", "магнит/брелок"),
        ("6971545126866", "брелок рулетка"),
    ],
}

SEED_VERSION = 2


def seed_quick(con):
    """Завести категории быстрых товаров, один раз за всю жизнь базы.

    Отметка живёт в PRAGMA user_version, а не в «таблица пустая»: пустая
    таблица означала бы, что удалённая хозяйкой категория возвращается при
    каждом обновлении.  Удалили значит удалили.
    """
    if con.execute("PRAGMA user_version").fetchone()[0] >= SEED_VERSION:
        return 0
    have = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM quick_groups")}
    added = 0
    for pos, name in enumerate(QUICK_SEED):
        if name not in have:
            cur = con.execute("INSERT INTO quick_groups (name, pos) VALUES (?,?)",
                              (name, pos))
            have[name] = cur.lastrowid
            added += 1

    # Штрихкоды в базе местами лежат без ведущих нулей, а на бумаге они с ними.
    code_of = {}
    for r in con.execute("SELECT id, barcode FROM products WHERE barcode IS NOT NULL"):
        code_of.setdefault((r["barcode"] or "").lstrip("0"), r["id"])
    for r in con.execute("SELECT code, product_id FROM barcodes"):
        code_of.setdefault((r["code"] or "").lstrip("0"), r["product_id"])

    for cat, items in QUICK_ITEMS.items():
        gid = have.get(cat)
        if gid is None:
            continue          # категорию успели убрать, не воскрешаем
        seen = {r["product_id"] for r in con.execute(
            "SELECT product_id FROM quick_items WHERE group_id = ?", (gid,))}
        pos = next_pos(con, "quick_items", "WHERE group_id = ?", (gid,))
        for code, label in items:
            pid = code_of.get(code.lstrip("0"))
            # Товара с таким штрихкодом в этой базе нет: пропускаем молча,
            # выдумывать позицию без товара тут не из чего.
            if pid is None or pid in seen:
                continue
            row = con.execute("SELECT name, price FROM products WHERE id = ?",
                              (pid,)).fetchone()
            con.execute("INSERT INTO quick_items (group_id, product_id, name,"
                        " price, pos) VALUES (?,?,?,?,?)",
                        (gid, pid, row["name"], row["price"], pos))
            seen.add(pid)
            pos += 1
            added += 1

    con.execute("PRAGMA user_version = %d" % SEED_VERSION)
    return added


def split_joined_codes(con):
    """Разделить доп. штрихкоды, слипшиеся в одну строку.

    В первой выгрузке UMAG колонка доп. кода была одна на товар, и когда
    наклеек несколько, они приезжали одной строкой через точку с запятой:
    «6941496207648;6941496207570;...».  Сканер такую строку прислать не может
    никогда, так что товар с ней просто не находится ни по одной из наклеек.
    """
    fixed = 0
    for r in list(con.execute("SELECT id, product_id, code FROM barcodes")):
        parts = [x for x in re.split(r"[,;/|\s]+", r["code"] or "") if x]
        if len(parts) < 2:
            continue
        con.execute("DELETE FROM barcodes WHERE id = ?", (r["id"],))
        for part in parts:
            # OR IGNORE: часть кодов из склейки может уже принадлежать
            # другому товару, и отбирать их у него мы не собираемся.
            con.execute("INSERT OR IGNORE INTO barcodes (product_id, code)"
                        " VALUES (?,?)", (r["product_id"], part))
        fixed += 1
    return fixed


def park_label(con):
    """Свободный номер клиента: КЛИЕНТ 1, КЛИЕНТ 2 и так далее.

    Берём наименьший свободный, а не следующий за наибольшим: забрали первый
    чек, и номер снова свободен.  Иначе к вечеру на экране КЛИЕНТ 47, и это
    ничего не значит.
    """
    used = set()
    for r in con.execute("SELECT label FROM parked"):
        m = re.match(r"^КЛИЕНТ (\d+)$", r["label"] or "")
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return "КЛИЕНТ %d" % n


def park_cart(con, cashier, items):
    """Убрать корзину в сторону и вернуть, под каким именем она легла."""
    label = park_label(con)
    con.execute("INSERT INTO parked (ts, cashier, label, items) VALUES (?,?,?,?)",
                (now_iso(), cashier, label, json.dumps(items, ensure_ascii=False)))
    return label


def parked_list(con):
    """Отложенные чеки для экрана: без содержимого, только сколько и на сколько."""
    out = []
    for r in con.execute("SELECT * FROM parked ORDER BY id"):
        try:
            items = json.loads(r["items"])
        except ValueError:
            items = []
        total = 0.0
        for it in items:
            gross = (it.get("price") or 0) * (it.get("qty") or 0)
            total += gross - gross * (it.get("disc") or 0) / 100.0
        out.append({"id": r["id"], "label": r["label"], "ts": r["ts"],
                    "cashier": r["cashier"], "count": len(items),
                    "total": round(total, 2)})
    return out


def take_parked(con, pid):
    """Достать отложенный чек обратно и убрать его из отложенных."""
    row = con.execute("SELECT * FROM parked WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise ValueError("этот чек уже забрали")
    con.execute("DELETE FROM parked WHERE id = ?", (pid,))
    try:
        return json.loads(row["items"])
    except ValueError:
        return []


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
