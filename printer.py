"""ESC/POS driver for the store's Xprinter (58mm, 32 columns, CP866, no cutter).

Hardware profile determined by probing the actual device:
    USB 0483:070b "Xprinter / USB Printer P" -> /dev/usb/lp0
    32 columns, CP866 selected with ESC t 17, manual tear (no auto-cutter).

The same printer answers to two different names depending on the machine.  On
the Linux development box it is a character device.  On the shop till, which
runs Windows 7, it is a spooler queue called XP-58 and the bytes go through
winprint.py.  POS_PRINTER overrides whichever default applies.
"""
import os
import sys

WINDOWS = sys.platform.startswith("win")

DEVICE = os.environ.get("POS_PRINTER") or ("XP-58" if WINDOWS else "/dev/usb/lp0")
WIDTH = 32
CODEPAGE = 17  # ESC t 17 -> CP866

ESC, GS = b"\x1b", b"\x1d"

# Kept at import time rather than inside send(): if the Windows plumbing is
# broken it should say so when the till starts, not in front of a customer.
if WINDOWS:
    import winprint

# CP866 has no Kazakh-specific letters, so fold them to the nearest Russian
# glyph instead of printing "?" holes in product names.
KZ_FOLD = str.maketrans({
    "ә": "а", "Ә": "А", "і": "и", "І": "И", "ң": "н", "Ң": "Н",
    "ғ": "г", "Ғ": "Г", "ү": "у", "Ү": "У", "ұ": "у", "Ұ": "У",
    "қ": "к", "Қ": "К", "ө": "о", "Ө": "О", "һ": "х", "Һ": "Х",
})


def enc(s):
    return s.translate(KZ_FOLD).encode("cp866", errors="replace")


class Receipt:
    """Builds an ESC/POS byte buffer. Text helpers all respect WIDTH."""

    def __init__(self):
        self.buf = bytearray()
        self.buf += ESC + b"@"                      # init
        self.buf += ESC + b"t" + bytes([CODEPAGE])  # Cyrillic

    # --- formatting ---------------------------------------------------
    def align(self, mode):
        """left | center | right"""
        self.buf += ESC + b"a" + bytes([{"left": 0, "center": 1, "right": 2}[mode]])
        return self

    def bold(self, on=True):
        self.buf += ESC + b"E" + bytes([1 if on else 0])
        return self

    def big(self, on=True):
        # GS ! n -- double width+height when on
        self.buf += GS + b"!" + bytes([0x11 if on else 0x00])
        return self

    # --- content ------------------------------------------------------
    def text(self, s=""):
        self.buf += enc(s) + b"\n"
        return self

    def rule(self, ch="-"):
        return self.text(ch * WIDTH)

    def wrap(self, s, indent=0):
        """Word-wrap to WIDTH; continuation lines get `indent` spaces."""
        pad = " " * indent
        line, avail = "", WIDTH
        for word in s.split():
            candidate = word if not line else line + " " + word
            if len(candidate) <= avail:
                line = candidate
            else:
                self.text(line)
                line, avail = pad + word, WIDTH
        if line:
            self.text(line)
        return self

    def row(self, left, right):
        """Left text, right text, flush to the margins on one line."""
        gap = WIDTH - len(left) - len(right)
        if gap < 1:  # too long -- right side wins, left gets truncated
            left = left[: WIDTH - len(right) - 1]
            gap = 1
        return self.text(left + " " * gap + right)

    def feed(self, n=4):
        self.buf += b"\n" * n
        return self

    # --- output -------------------------------------------------------
    def bytes(self):
        return bytes(self.buf)

    def send(self, device=None):
        """Отправить чек на принтер.  Raises OSError if the paper stays blank."""
        target = device or DEVICE
        if WINDOWS:
            winprint.send_raw(self.bytes(), target)
        else:
            with open(target, "wb") as f:
                f.write(self.bytes())


def money(v):
    return f"{v:.2f}"


def sale_receipt(items, total, number, when, cashier, shop, payment=None,
                 kind="sale"):
    """Товарный чек.

    items   -- [{"name", "qty", "unit", "price", "disc", "discount"}, ...]
               disc is the percentage, discount the tenge taken off the line.
    payment -- {"kind": card|cash|mixed, "card": .., "cash": .., "given": ..,
                "change": ..}; omit for no payment block.
    kind    -- sale | copy | void.  A copy and a cancellation carry the same
               figures as the original, so they must announce what they are:
               two identical slips in a drawer are impossible to tell apart.
    """
    r = Receipt()
    r.align("center").bold(True).text(shop).bold(False)
    if kind == "void":
        r.bold(True).text("*** ОТМЕНА ЧЕКА ***").bold(False)
    r.text(f"Товарный чек №{number}")
    if kind == "copy":
        r.text("(КОПИЯ)")
    r.text(when.strftime("%d.%m.%Y %H:%M"))
    r.text(f"Кассир: {cashier}")
    r.align("left").rule()

    saved = 0.0
    for i, it in enumerate(items, 1):
        qty, price = float(it["qty"]), float(it["price"])
        discount = float(it.get("discount") or 0)
        line = round(qty * price, 2)
        r.wrap(f"{i}. {it['name']}", indent=3)
        qty_s = f"{qty:g}".rstrip(".")
        r.row(f"   {qty_s} {it.get('unit') or 'шт'} x {money(price)}",
              f"{money(line)} тг")
        if discount:
            # Percentage on the left, what the line actually costs on the right.
            saved += discount
            r.row(f"   скидка: -{money(float(it.get('disc') or 0))} %",
                  f"{money(line - discount)} тг")

    r.rule()
    if saved:
        r.row("СКИДКА:", f"-{money(saved)} тг")
    r.bold(True).row("ИТОГО:", f"{money(total)} тг").bold(False)

    if payment:
        kind = payment.get("kind", "cash")
        card = float(payment.get("card") or 0)
        cash = float(payment.get("cash") or 0)
        given = float(payment.get("given") or 0)
        change = float(payment.get("change") or 0)
        if kind == "card":
            r.row("БАНК. КАРТОЙ", f"{money(card or total)} тг")
        elif kind == "cash":
            r.row("НАЛИЧНЫМИ", f"{money(cash or total)} тг")
            r.row("ПОЛУЧЕНО", f"{money(given)} тг")
            r.row("СДАЧА", f"{money(change)} тг")
        else:  # mixed
            r.row("БАНК. КАРТОЙ", f"{money(card)} тг")
            r.row("НАЛИЧНЫМИ", f"{money(cash)} тг")
            if given:
                r.row("ПОЛУЧЕНО", f"{money(given)} тг")
            if change:
                r.row("СДАЧА", f"{money(change)} тг")

    r.rule()
    r.align("center").text()
    if kind == "void":
        r.bold(True).text("ЧЕК ОТМЕНЁН").bold(False)
        r.text("товар возвращён на склад")
    else:
        r.text("Спасибо за покупку!")
    r.feed()  # clear the tear bar -- no auto-cutter
    return r


def day_report(shift, opened, closed, top=(), shop="МАГАЗИН"):
    """Отчёт за операционный день.  Not a fiscal Z-report -- the terminal
    handles fiscalisation.  This is the owner's own end-of-day summary.

    shift -- mapping with cashier, receipts, revenue, cash, card, profit
    top   -- [(name, qty, sum), ...] best sellers, may be empty
    """
    r = Receipt()
    r.align("center").bold(True).text(shop).bold(False)
    r.text("ОТЧЁТ ЗА ДЕНЬ")
    r.align("left").rule()
    r.row("Кассир:", str(shift["cashier"]))
    r.row("Открыт:", opened.strftime("%d.%m.%Y %H:%M"))
    r.row("Закрыт:", closed.strftime("%d.%m.%Y %H:%M"))
    r.rule()

    n = int(shift["receipts"] or 0)
    revenue = float(shift["revenue"] or 0)
    r.row("ЧЕКОВ", str(n))
    r.bold(True).row("ВЫРУЧКА", f"{money(revenue)} тг").bold(False)
    r.row("  наличными", f"{money(shift['cash'] or 0)} тг")
    r.row("  банк. картой", f"{money(shift['card'] or 0)} тг")
    r.row("СКИДКИ", f"{money(shift['discount'] or 0)} тг")
    r.row("ПРИБЫЛЬ", f"{money(shift['profit'] or 0)} тг")
    r.row("СРЕДНИЙ ЧЕК", f"{money(revenue / n if n else 0)} тг")

    if top:
        r.rule()
        r.text("ЛУЧШЕ ВСЕГО ПРОДАВАЛОСЬ:")
        for name, qty, total in top:
            r.wrap(f"{name}", indent=2)
            r.row(f"   {qty:g} шт", f"{money(total)} тг")

    r.rule()
    r.text()
    r.text("Подпись: ______________")
    r.feed()
    return r
