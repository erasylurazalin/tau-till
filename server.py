#!/usr/bin/env python3
"""Local POS server.  Stdlib only.

    python3 server.py            # http://localhost:8000
    python3 server.py --no-print # dev mode, receipts go to stdout

Bind is 127.0.0.1: the till and the browser are the same machine, and anything
wider makes Windows Firewall stop the shop with a permission box that nobody
behind the counter should have to answer.  Set POS_HOST=0.0.0.0 to open the UI
to a phone on the shop wifi, and expect that box the first time.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import db
import version
from printer import sale_receipt, day_report

HERE = Path(__file__).parent
# POS_PORT (like POS_DB) lets a test run stand up a second copy without
# fighting the till that is actually serving the shop.
PORT = int(os.environ.get("POS_PORT") or 8000)
HOST = os.environ.get("POS_HOST") or "127.0.0.1"
PRINTING = "--no-print" not in sys.argv
# Printed at the top of every receipt.  Set in settings.json on the till, so
# the shop can be renamed without anyone editing Python.
SHOP_NAME = os.environ.get("POS_SHOP") or "МАГАЗИН"


def day_bounds(date_str=None):
    d = date_str or datetime.now().strftime("%Y-%m-%d")
    return d, d + "%"


# --- administrator access ------------------------------------------------
# The split is by risk, not by seniority.  Приёмка is everyday work: it only
# ever adds stock, and every delivery is a ledger entry anyone can audit, so
# any cashier may do it.  What stays with the owner is everything that can
# make a discrepancy disappear -- инвентаризация overwrites a balance outright,
# deletion removes goods, and the cashier list controls who can do any of it.
#
# Reaching the admin side requires nothing beyond being logged in as the owner:
# her PIN already opened the operational day.  Every admin route is refused by
# the server itself rather than merely hidden by the interface.  This is not
# protection against someone with real intent on the same network.
ADMIN_ROUTES = {"/api/count", "/api/cashiers/add",
                "/api/cashiers/pin", "/api/cashiers/delete",
                "/api/products/delete", "/api/products/delete-preview",
                "/api/quit", "/api/update"}
ADMIN_GET_ROUTES = {"/api/cashiers"}

EPS = 0.005  # tenge rounding slack, so 17862.00 vs 17861.999 doesn't reject
LABELS = {"card": "КАРТОЙ", "cash": "НАЛИЧНЫМИ", "mixed": "СМЕШАННАЯ"}
KINDS = {v: k for k, v in LABELS.items()}   # stored label -> payment kind


def chunks(seq, size=400):
    """SQLite caps the number of bound variables, and a batch delete can carry
    thousands of ids, so queries are issued in slices."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def number_of(value, field, default=0.0):
    """Parse a form number, in Russian, without leaking a Python traceback."""
    text = str(value if value is not None else "").replace(",", ".").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{field}: введите число, а не «{text}»")


def resolve_payment(pay, total):
    """Validate the payment split and derive change.

    Returns card (charged on terminal), cash (kept in drawer), given (handed
    over by the customer) and change.  Rejects underpayment -- the operator
    must not be able to close a sale for less than the total.
    """
    pay = pay if isinstance(pay, dict) else {}
    kind = pay.get("kind", "cash")
    if kind not in LABELS:
        raise ValueError(f"неизвестный способ оплаты: {kind}")

    if kind == "card":
        card, given = total, 0.0
    elif kind == "cash":
        card = 0.0
        given = round(float(pay.get("given") or 0), 2)
    else:
        card = round(float(pay.get("card") or 0), 2)
        given = round(float(pay.get("given") or 0), 2)
        if card < -EPS or card > total + EPS:
            raise ValueError("сумма по карте вне диапазона")
        card = min(max(card, 0.0), total)

    if kind in ("cash", "mixed") and card + given < total - EPS:
        short = round(total - card - given, 2)
        raise ValueError(f"недостаточно: не хватает {short:.2f} тг")

    cash = round(total - card, 2)                      # actually kept
    change = round(max(0.0, card + given - total), 2)
    return {"kind": kind, "label": LABELS[kind], "card": card,
            "cash": cash, "given": given, "change": change}


class Handler(BaseHTTPRequestHandler):
    # --- plumbing -----------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):
        pass  # quiet

    # --- routing ------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        con = db.connect()
        if u.path in ADMIN_GET_ROUTES and not self.is_admin(con):
            con.close()
            return self._json({"error": "нужен вход администратора",
                               "locked": True}, 403)

        if u.path == "/":
            html = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            # The UI is read fresh from disk on every request; without this the
            # browser happily serves a stale copy and edits appear to do nothing.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(html)

        elif u.path == "/api/lookup":
            term = (q.get("q") or [""])[0].strip()
            if not term:
                return self._json([])
            # Exact barcode first -- that's the scanner path and must win.
            # Any of the product's stickers counts, not just the main one.
            hit = db.find_by_code(con, term)
            if hit and hit["active"]:
                # dict(**a, **b) вместо a | b: слияние словарей знаком «|»
                # появилось только в Python 3.9, а касса работает на 3.8,
                # последнем Python для Windows 7.
                row = dict(hit)
                row["codes"] = db.codes_of(con, hit["id"])
                return self._json([row])
            rows = con.execute(
                "SELECT * FROM products WHERE lower_u(name) LIKE lower_u(?)"
                " AND active = 1 ORDER BY name LIMIT 20",
                (f"%{term}%",)).fetchall()
            self._json([dict(r) for r in rows])

        elif u.path == "/api/products":
            # The catalogue is thousands of rows; never ship it all to the
            # browser -- the till machine is not fast and the table would crawl.
            term = (q.get("q") or [""])[0].strip()
            limit = min(int((q.get("limit") or ["200"])[0]), 1000)
            where, args = ["active = 1"], []
            if term:
                where.append(
                    "(barcode = ? OR lower_u(name) LIKE lower_u(?)"
                    " OR id IN (SELECT product_id FROM barcodes WHERE code = ?))")
                args += [term, f"%{term}%", term]
            if (q.get("uncounted") or [""])[0] == "1":
                where.append("stock IS NULL")
            if (q.get("nocost") or [""])[0] == "1":
                where.append("cost <= 0")
            cond = " AND ".join(where)
            if (q.get("idsonly") or [""])[0] == "1":
                # "Select everything found" must cover the whole filtered set,
                # not just the page the browser happens to be showing.
                found = con.execute(
                    f"SELECT id FROM products WHERE {cond} ORDER BY name",
                    args).fetchall()
                return self._json({"ids": [r["id"] for r in found],
                                   "count": len(found)})
            args.append(limit)
            rows = con.execute(
                f"SELECT * FROM products WHERE {cond}"
                f" ORDER BY name LIMIT ?", args).fetchall()
            tot = con.execute(
                "SELECT COUNT(*) c, SUM(stock IS NULL) u, SUM(cost <= 0) nc,"
                " SUM(price <= 0) np FROM products WHERE active = 1").fetchone()
            self._json({"rows": [dict(r) for r in rows],
                        "shown": len(rows), "total": tot["c"],
                        "uncounted": tot["u"], "nocost": tot["nc"],
                        "noprice": tot["np"]})

        elif u.path == "/api/low-stock":
            rows = con.execute(
                "SELECT * FROM products WHERE active = 1 AND stock IS NOT NULL"
                " AND stock <= min_stock ORDER BY stock").fetchall()
            self._json([dict(r) for r in rows])

        elif u.path == "/api/shift":
            shift = db.current_shift(con)
            who = db.find_cashier(con, shift["cashier"]) if shift else None
            self._json({
                "open": shift is not None,
                "shift": dict(shift) if shift else None,
                # Drives whether the АДМИН tab is offered at all.
                "owner": bool(who and who["is_owner"]),
                "cashiers": [r["name"] for r in con.execute(
                    "SELECT name FROM cashiers ORDER BY is_owner DESC, name")],
                # Версия едет тем же ответом, который страница и так просит при
                # загрузке: отдельный запрос ради одной строки не нужен, а
                # спросить «что написано в углу» по телефону нужно всегда.
                "version": version.label(),
            })

        elif u.path == "/api/cashiers":
            # PINs are never sent to the browser; they can be replaced, not read.
            self._json([{"id": r["id"], "name": r["name"],
                         "is_owner": r["is_owner"]}
                        for r in con.execute(
                            "SELECT * FROM cashiers"
                            " ORDER BY is_owner DESC, name")])

        elif u.path == "/api/day":
            # The figures always describe the OPEN operational day, not the
            # calendar date -- a day that runs past midnight stays one day.
            shift = db.current_shift(con)
            if not shift:
                return self._json({"open": False})
            sid = shift["id"]
            by_pay = con.execute(
                "SELECT payment, COUNT(*) n, SUM(total) sum FROM receipts"
                " WHERE voided = 0 AND shift_id = ? GROUP BY payment",
                (sid,)).fetchall()
            top = con.execute(
                "SELECT i.name, SUM(i.qty) qty, SUM(i.qty * i.price) sum"
                " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
                " WHERE r.voided = 0 AND r.shift_id = ? GROUP BY i.name"
                " ORDER BY sum DESC LIMIT 10", (sid,)).fetchall()
            voided = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(total),0) sum FROM receipts"
                " WHERE voided = 1 AND shift_id = ?", (sid,)).fetchone()
            self._json({
                "open": True,
                "shift": dict(shift),
                "voided": dict(voided),
                **db.shift_totals(con, sid),   # receipts, revenue, cash, card, profit
                "by_payment": [dict(r) for r in by_pay],
                "top": [dict(r) for r in top],
            })

        elif u.path == "/api/receipts":
            # Default to the OPEN operational day, matching /api/day: a day
            # that runs past midnight is still one day, and "чеки за день"
            # must mean the same stretch of time as "выручка за день".
            date = (q.get("date") or [None])[0]
            if date:
                _, like = day_bounds(date)
                rows = con.execute(
                    "SELECT * FROM receipts WHERE ts LIKE ? ORDER BY id DESC",
                    (like,)).fetchall()
            else:
                shift = db.current_shift(con)
                rows = con.execute(
                    "SELECT * FROM receipts WHERE shift_id = ? ORDER BY id DESC",
                    (shift["id"],)).fetchall() if shift else []
            self._json([{**dict(r), "items": self.receipt_lines(con, r["id"])}
                        for r in rows])

        elif u.path == "/api/receipt":
            row = con.execute("SELECT * FROM receipts WHERE id = ?",
                              ((q.get("id") or [0])[0],)).fetchone()
            if row is None:
                return self._json({"error": "чек не найден"}, 404)
            self._json({**dict(row),
                        "items": self.receipt_lines(con, row["id"])})

        else:
            self._json({"error": "not found"}, 404)
        con.close()

    def is_admin(self, con):
        """Admin rights follow whoever holds the open operational day.

        Checked on every request rather than handed out once, so closing the
        day -- or another cashier taking the till -- ends admin access at the
        same moment.
        """
        shift = db.current_shift(con)
        who = db.find_cashier(con, shift["cashier"]) if shift else None
        return bool(who and who["is_owner"])

    # --- cashiers ------------------------------------------------------
    def cashier_add(self, con, data):
        name = (data.get("name") or "").strip().upper()
        pin = str(data.get("pin") or "").strip()
        if not name:
            raise ValueError("укажите имя кассира")
        if not (pin.isdigit() and len(pin) == 4):
            raise ValueError("код должен состоять из 4 цифр")
        if db.find_cashier(con, name):
            raise ValueError(f"кассир «{name}» уже есть")
        con.execute("INSERT INTO cashiers (name, pin) VALUES (?,?)", (name, pin))
        con.commit()
        return {"ok": True, "name": name}

    def cashier_pin(self, con, data):
        pin = str(data.get("pin") or "").strip()
        if not (pin.isdigit() and len(pin) == 4):
            raise ValueError("код должен состоять из 4 цифр")
        row = con.execute("SELECT * FROM cashiers WHERE id=?",
                          (data.get("id"),)).fetchone()
        if row is None:
            raise ValueError("кассир не найден")
        con.execute("UPDATE cashiers SET pin=? WHERE id=?", (pin, row["id"]))
        con.commit()
        return {"ok": True, "name": row["name"]}

    def cashier_delete(self, con, data):
        row = con.execute("SELECT * FROM cashiers WHERE id=?",
                          (data.get("id"),)).fetchone()
        if row is None:
            raise ValueError("кассир не найден")
        if row["is_owner"]:
            raise ValueError("владельца удалить нельзя")
        shift = db.current_shift(con)
        if shift and shift["cashier"] == row["name"]:
            raise ValueError("этот кассир сейчас работает, сначала завершите день")
        con.execute("DELETE FROM cashiers WHERE id=?", (row["id"],))
        con.commit()
        # Past shifts keep the name as text, so history survives the deletion.
        return {"ok": True, "name": row["name"]}

    def classify(self, con, ids):
        """Split a selection into (removable, archivable).

        Goods never sold and never counted can go outright.  Anything with
        history must be archived instead (active = 0): it leaves ОСТАТКИ and
        stops being findable at the till, but the receipts and stock ledger
        that refer to it stay intact.
        """
        ids = [int(i) for i in ids]
        plain, keep = {}, {}
        for part in chunks(ids):
            marks = ",".join("?" * len(part))
            used = {r[0] for r in con.execute(
                f"SELECT DISTINCT product_id FROM receipt_items"
                f" WHERE product_id IN ({marks})", part)}
            used |= {r[0] for r in con.execute(
                f"SELECT DISTINCT product_id FROM stock_moves"
                f" WHERE product_id IN ({marks})", part)}
            for r in con.execute(
                    f"SELECT id, name FROM products WHERE id IN ({marks})", part):
                (keep if r["id"] in used else plain)[r["id"]] = r["name"]
        return plain, keep

    def delete_preview(self, con, data):
        """What a batch delete is about to do, before it does it."""
        ids = data.get("ids") or []
        if not ids:
            raise ValueError("ничего не выбрано")
        plain, keep = self.classify(con, ids)
        missing = len(set(int(i) for i in ids)) - len(plain) - len(keep)
        return {"ok": True,
                "deleted": len(plain), "archived": len(keep), "missing": missing,
                "sample_deleted": sorted(plain.values())[:25],
                "sample_archived": sorted(keep.values())[:25]}

    def product_delete(self, con, data):
        ids = data.get("ids") or ([data["id"]] if data.get("id") is not None else [])
        if not ids:
            raise ValueError("ничего не выбрано")
        plain, keep = self.classify(con, ids)
        if not plain and not keep:
            raise ValueError("товар не найден")
        for part in chunks(list(plain)):
            con.execute("DELETE FROM products WHERE id IN (%s)"
                        % ",".join("?" * len(part)), part)
        for part in chunks(list(keep)):
            con.execute("UPDATE products SET active = 0 WHERE id IN (%s)"
                        % ",".join("?" * len(part)), part)
        con.commit()
        name = next(iter({**plain, **keep}.values()), "")
        return {"ok": True, "deleted": len(plain), "archived": len(keep),
                "name": name}

    def do_POST(self):
        u = urlparse(self.path)
        con = db.connect()
        if u.path in ADMIN_ROUTES and not self.is_admin(con):
            con.close()
            return self._json({"error": "нужен вход администратора",
                               "locked": True}, 403)
        try:
            if u.path == "/api/sale":
                self._json(self.sale(con, self._body()))
            elif u.path == "/api/cashiers/add":
                self._json(self.cashier_add(con, self._body()))
            elif u.path == "/api/cashiers/pin":
                self._json(self.cashier_pin(con, self._body()))
            elif u.path == "/api/cashiers/delete":
                self._json(self.cashier_delete(con, self._body()))
            elif u.path == "/api/products/delete":
                self._json(self.product_delete(con, self._body()))
            elif u.path == "/api/products/delete-preview":
                self._json(self.delete_preview(con, self._body()))
            elif u.path == "/api/quick-add":
                self._json(self.quick_add(con, self._body()))
            elif u.path == "/api/shift/open":
                self._json(self.shift_open(con, self._body()))
            elif u.path == "/api/shift/close":
                self._json(self.shift_close(con, self._body()))
            elif u.path == "/api/shift/print":
                self._json(self.shift_print(con, self._body()))
            elif u.path == "/api/receipt/print":
                self._json(self.receipt_print(con, self._body()))
            elif u.path == "/api/receipt/void":
                self._json(self.receipt_void(con, self._body()))
            elif u.path == "/api/receive":
                self._json(self.receive(con, self._body()))
            elif u.path == "/api/count":
                self._json(self.count(con, self._body()))
            elif u.path == "/api/minimize":
                self._json(self.minimize())
            elif u.path == "/api/quit":
                self._json(self.quit())
            elif u.path == "/api/update":
                self._json(self.update())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            con.rollback()
            self._json({"error": str(e)}, 400)
        finally:
            con.close()

    # --- operational day ----------------------------------------------
    def shift_open(self, con, data):
        """Кассир logs in; that moment is the start of the operational day."""
        who = db.find_cashier(con, data.get("cashier"))
        if who is None:
            raise ValueError("неизвестный кассир")
        if str(data.get("pin") or "") != who["pin"]:
            time.sleep(1)
            raise ValueError("неверный код")
        cashier = who["name"]

        open_now = db.current_shift(con)
        if open_now:
            # Reopening the browser mid-day rejoins that day rather than
            # starting a second one.  Somebody else's day is not yours to
            # walk into, though -- only the owner may take one over, and she
            # needs that to be able to close a day a cashier left running.
            if open_now["cashier"] != who["name"] and not who["is_owner"]:
                raise ValueError(
                    f"день открыт кассиром {open_now['cashier']},"
                    " сначала нужно его завершить")
            holder = db.find_cashier(con, open_now["cashier"])
            return {"ok": True, "resumed": True, "shift": dict(open_now),
                    # Admin follows the day's cashier, matching what the
                    # server will actually allow.
                    "owner": bool(holder and holder["is_owner"])}
        return {"ok": True, "resumed": False, "owner": bool(who["is_owner"]),
                "shift": dict(db.open_shift(con, cashier))}

    def shift_top(self, con, shift_id, limit=5):
        return [dict(r) for r in con.execute(
            "SELECT i.name, SUM(i.qty) qty, SUM(i.qty * i.price) sum"
            " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.voided = 0 AND r.shift_id = ? GROUP BY i.name"
            " ORDER BY sum DESC LIMIT ?", (shift_id, limit))]

    def print_day(self, con, shift):
        """Печать отчёта.  Never raises: a failed print must not undo a
        closed day, it just gets reported back to the screen."""
        if not PRINTING:
            return False, "печать отключена (--no-print)"
        try:
            top = self.shift_top(con, shift["id"])
            day_report(
                shift,
                opened=datetime.fromisoformat(shift["opened_at"]),
                closed=datetime.fromisoformat(shift["closed_at"]),
                top=[(t["name"], t["qty"], t["sum"]) for t in top],
                shop=SHOP_NAME).send()
            return True, None
        except (PermissionError, FileNotFoundError, OSError) as e:
            return False, str(e)

    def backup_day(self, con):
        """Резервная копия при закрытии дня.  Like printing, never raises:
        the day is already closed in the database, and refusing to report that
        because a copy failed would leave the cashier stuck at the till."""
        try:
            return db.backup(con).name, None
        except (OSError, sqlite3.Error) as e:
            return None, str(e)

    def shift_close(self, con, data):
        shift = db.current_shift(con)
        if not shift:
            raise ValueError("операционный день не открыт")
        sid = shift["id"]
        top = self.shift_top(con, sid)
        closed = dict(db.close_shift(con, sid))
        printed, err = (self.print_day(con, closed) if data.get("print")
                        else (False, None))
        # Taken after the close so the copy contains the finished day.
        backup, backup_err = self.backup_day(con)
        return {"ok": True, "shift": closed, "top": top,
                "printed": printed, "print_error": err,
                "backup": backup, "backup_error": backup_err}

    def minimize(self):
        """Свернуть браузер, показать рабочий стол Windows.

        Same problem as quit() -- no window frame, no keyboard, no way out of
        a full-screen kiosk browser from inside the page -- but this exit is
        meant to be temporary rather than final.  The server has no window
        handle of its own, so it only leaves a note next to the database, the
        same way an update request does, and the launcher that actually
        started the browser is the one watching for it and doing the
        minimizing.  On a dev run with no launcher attached the note is
        simply never picked up.
        """
        flag = db.DB_PATH.parent / "minimize-requested"
        try:
            flag.write_text(db.now_iso(), encoding="utf-8")
        except OSError as e:
            raise ValueError(f"не удалось запросить сворачивание: {e}")
        return {"ok": True}

    def quit(self):
        """Выключение кассы с самого экрана.

        The till runs full screen on a machine with no keyboard, so there is no
        Alt+F4 and no visible way out of the browser.  This is that way out:
        the server stops, and the launcher that started it notices and closes
        the browser, leaving the owner on the Windows desktop.  An open
        operational day is left open on purpose, exactly as it survives a power
        cut, and is rejoined when the till starts again.
        """
        def stop():
            time.sleep(0.4)   # let this reply reach the browser first
            self.server.shutdown()
        threading.Thread(target=stop, daemon=True).start()
        return {"ok": True}

    def update(self):
        """Обновление кассы по кнопке из АДМИН.

        Сам сервер обновиться не может: на Windows нельзя переписать файлы
        работающей программы.  Поэтому он только оставляет записку и
        выключается, а всё остальное делает наблюдатель, который его запускал:
        он видит записку, скачивает новую версию, подменяет папку и поднимает
        кассу обратно.  Если новая версия не заведётся, он вернёт старую.
        """
        flag = db.DB_PATH.parent / "update-requested"
        try:
            flag.write_text(db.now_iso(), encoding="utf-8")
        except OSError as e:
            raise ValueError(f"не удалось записать запрос на обновление: {e}")
        self.quit()
        return {"ok": True}

    # --- receipts -------------------------------------------------------
    def receipt_lines(self, con, rid):
        """Lines as they were sold.  Name, price and cost are snapshots on the
        row itself; only the unit is looked up, and only to print it."""
        return [dict(r) for r in con.execute(
            "SELECT i.*, COALESCE(p.unit, 'шт') unit FROM receipt_items i"
            " LEFT JOIN products p ON p.id = i.product_id"
            " WHERE i.receipt_id = ? ORDER BY i.id", (rid,))]

    def receipt_of(self, con, data):
        row = con.execute("SELECT * FROM receipts WHERE id = ?",
                          (data.get("id"),)).fetchone()
        if row is None:
            raise ValueError("чек не найден")
        return row

    def print_receipt(self, con, row, kind):
        """Rebuild the cheque from what was stored and put it on paper."""
        if not PRINTING:
            return False, "печать отключена (--no-print)"
        payment = {"kind": KINDS.get(row["payment"], "cash"),
                   "card": row["paid_card"], "cash": row["paid_cash"],
                   "given": row["given"], "change": row["change"]}
        try:
            sale_receipt(
                items=self.receipt_lines(con, row["id"]),
                total=row["total"], number=row["number"],
                when=datetime.fromisoformat(row["ts"]), cashier=row["cashier"],
                shop=SHOP_NAME, payment=payment, kind=kind).send()
            return True, None
        except (PermissionError, FileNotFoundError, OSError) as e:
            return False, str(e)

    def receipt_print(self, con, data):
        row = self.receipt_of(con, data)
        printed, err = self.print_receipt(
            con, row, "void" if row["voided"] else "copy")
        return {"ok": True, "number": row["number"],
                "printed": printed, "print_error": err}

    def receipt_void(self, con, data):
        """Отмена чека: cancel it and put the goods back on the shelf.

        Restricted to the open day on purpose.  The takings of a finished day
        have already been counted and reported, and reaching back to change
        them would make yesterday's paper disagree with the database.  A
        customer returning goods later is a возврат -- a separate operation.
        """
        row = self.receipt_of(con, data)
        shift = self.require_shift(con)
        if row["voided"]:
            raise ValueError(f"чек №{row['number']} уже отменён")
        if row["shift_id"] != shift["id"]:
            raise ValueError(f"чек №{row['number']} из другого дня,"
                             " отменить можно только чек текущего дня")

        for line in self.receipt_lines(con, row["id"]):
            if line["product_id"] is not None:   # универсальный товар has none
                db.move_stock(con, line["product_id"], line["qty"], "void",
                              ref=row["number"], note="отмена чека")
        con.execute("UPDATE receipts SET voided = 1, voided_at = ?,"
                    " voided_by = ? WHERE id = ?",
                    (db.now_iso(), shift["cashier"], row["id"]))
        con.commit()

        row = self.receipt_of(con, data)
        printed, err = ((False, None) if not data.get("print")
                        else self.print_receipt(con, row, "void"))
        return {"ok": True, "number": row["number"], "total": row["total"],
                "printed": printed, "print_error": err}

    def shift_print(self, con, data):
        """Print (or reprint) the report of a day that is already closed."""
        row = con.execute("SELECT * FROM shifts WHERE id=?",
                          (data.get("id"),)).fetchone()
        if row is None:
            raise ValueError("операционный день не найден")
        if not row["closed_at"]:
            raise ValueError("день ещё не завершён")
        printed, err = self.print_day(con, dict(row))
        return {"ok": True, "printed": printed, "print_error": err}

    # --- operations ---------------------------------------------------
    def sale(self, con, data):
        items = data.get("items") or []
        if not items:
            raise ValueError("пустой чек")
        shift = self.require_shift(con)
        cashier = shift["cashier"]
        number = db.next_receipt_number(con, shift["id"])
        now = datetime.now()

        # Every figure is recomputed here from the percentage; the browser's
        # arithmetic is never taken on trust.
        lines = []
        for i in items:
            qty = number_of(i.get("qty"), "Количество")
            price = number_of(i.get("price"), "Цена")
            disc = number_of(i.get("disc"), "Скидка")
            if not 0 <= disc <= 100:
                raise ValueError("скидка должна быть от 0 до 100 %")
            gross = round(qty * price, 2)
            discount = round(gross * disc / 100, 2)
            lines.append({**i, "qty": qty, "price": price, "disc": disc,
                          "gross": gross, "discount": discount,
                          "net": round(gross - discount, 2)})

        total = round(sum(l["net"] for l in lines), 2)
        saved = round(sum(l["discount"] for l in lines), 2)
        pay = resolve_payment(data.get("payment"), total)

        cur = con.execute(
            "INSERT INTO receipts (shift_id, number, ts, cashier, payment,"
            " total, paid_card, paid_cash, given, change, discount)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (shift["id"], number, now.isoformat(timespec="seconds"), cashier,
             pay["label"], total, pay["card"], pay["cash"], pay["given"],
             pay["change"], saved))
        rid = cur.lastrowid

        for l in lines:
            pid = l.get("product_id")
            if pid is None:
                # Универсальный товар: a one-off line with no catalogue entry.
                # It sells and prints like anything else, but there is no
                # product to take off the shelf, so no ledger entry either.
                con.execute(
                    "INSERT INTO receipt_items (receipt_id, product_id, name,"
                    " qty, price, cost, disc, discount)"
                    " VALUES (?,NULL,?,?,?,0,?,?)",
                    (rid, l.get("name") or "Универсальный товар",
                     l["qty"], l["price"], l["disc"], l["discount"]))
                continue
            p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if p is None:
                raise ValueError(f"товар №{pid} не найден")
            con.execute(
                "INSERT INTO receipt_items (receipt_id, product_id, name, qty,"
                " price, cost, disc, discount) VALUES (?,?,?,?,?,?,?,?)",
                (rid, p["id"], p["name"], l["qty"], l["price"], p["cost"],
                 l["disc"], l["discount"]))
            # Print the catalogue name, not whatever the browser sent, so the
            # paper and the stored line can never disagree.
            l["name"], l["unit"] = p["name"], p["unit"]
            db.move_stock(con, p["id"], -l["qty"], "sale", ref=number)
        con.commit()

        printed, err = False, None
        receipt = sale_receipt(
            items=lines, total=total, number=number, when=now, cashier=cashier,
            shop=SHOP_NAME, payment=pay)
        if PRINTING:
            try:
                receipt.send()
                printed = True
            except (PermissionError, FileNotFoundError, OSError) as e:
                err = str(e)  # sale is already saved; printing is best-effort
        return {"ok": True, "number": number, "total": total,
                "change": pay["change"], "payment": pay["label"],
                "printed": printed, "print_error": err}

    def require_shift(self, con):
        """Stock only moves inside an operational day, so the day's report and
        the ledger always describe the same stretch of time."""
        shift = db.current_shift(con)
        if not shift:
            raise ValueError("операционный день не открыт, войдите как кассир")
        return shift

    def quick_add(self, con, data):
        """Быстрая приёмка: get an unknown item sellable in three fields.

        Deliberately does not ask for a quantity -- the shelf has not been
        counted, so stock stays NULL until инвентаризация says otherwise.
        Cost is left at 0 and shows up in the admin "нет закупочной цены"
        list, which is where the owner fills it in later.
        """
        self.require_shift(con)
        barcode = (data.get("barcode") or "").strip() or None
        name = (data.get("name") or "").strip()
        if not barcode:
            raise ValueError("укажите штрихкод")
        if not name:
            raise ValueError("укажите наименование")
        price = number_of(data.get("price"), "Цена продажи")
        if price <= 0:
            raise ValueError("укажите цену продажи")

        row = db.find_by_code(con, barcode)
        if row:
            # Already known: treat this as a correction of name and price
            # rather than an error, since that is what the operator meant.
            con.execute("UPDATE products SET name=?, price=?, active=1"
                        " WHERE id=?", (name, price, row["id"]))
            created = False
            pid = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO products (barcode, name, price, stock)"
                " VALUES (?,?,?,NULL)", (barcode, name, price))
            created = True
            pid = cur.lastrowid
        con.commit()
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return {"ok": True, "created": created, "product": dict(p)}

    def count(self, con, data):
        """Инвентаризация: record the physically counted quantity.

        This is how an imported product stops being "не посчитан".  The
        counted number wins outright -- it is the shelf, not the database,
        that is authoritative.
        """
        term = (data.get("barcode") or "").strip()
        pid = data.get("product_id")
        if pid is None:
            row = db.find_by_code(con, term)
            if row is None or not row["active"]:
                raise ValueError(f"товар не найден: {term}")
            pid = row["id"]
        qty = number_of(data.get("qty"), "Посчитано")
        if qty < 0:
            raise ValueError("количество не может быть отрицательным")

        res = db.set_stock(con, pid, qty, note=data.get("note") or "инвентаризация")
        con.commit()
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return {"ok": True, "product": dict(p), **res}

    def receive(self, con, data):
        """Приёмка: add stock, creating the product if the barcode is new."""
        self.require_shift(con)
        barcode = (data.get("barcode") or "").strip() or None
        qty = number_of(data.get("qty"), "Количество")
        if qty <= 0:
            raise ValueError("количество должно быть больше нуля")

        row = db.find_by_code(con, barcode) if barcode else None
        if row:
            pid = row["id"]
            # Update prices only when the form actually supplied them.
            for field in ("price", "cost"):
                if data.get(field) not in (None, ""):
                    con.execute(f"UPDATE products SET {field}=? WHERE id=?",
                                (number_of(data[field], field), pid))
            created = False
        else:
            if not data.get("name"):
                raise ValueError("новый товар: укажите наименование")
            # A product being created here genuinely had none before, so its
            # balance starts at a known 0 and the delivery below fills it.
            cur = con.execute(
                "INSERT INTO products (barcode, name, price, cost, unit,"
                " min_stock, stock) VALUES (?,?,?,?,?,?,0)",
                (barcode, data["name"], number_of(data.get("price"), "Цена продажи"),
                 number_of(data.get("cost"), "Закуп. цена"), data.get("unit") or "шт",
                 number_of(data.get("min_stock"), "Мин. остаток")))
            pid = cur.lastrowid
            created = True

        # Extra stickers for the same shelf item, e.g. the same тетрадь in a
        # different cover.  Rejected outright if one already belongs elsewhere,
        # rather than quietly moving it and breaking the other product.
        added = db.add_codes(con, pid, data.get("extra") or [])

        db.move_stock(con, pid, qty, "receive", ref=data.get("invoice"),
                      note=data.get("note"))
        con.commit()
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return {"ok": True, "created": created, "product": dict(p),
                "codes": db.codes_of(con, pid), "added_codes": added}


if __name__ == "__main__":
    db.init()
    mode = "printing enabled" if PRINTING else "PRINT DISABLED (dev)"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        # errno 98 on Linux, 10048 on Windows: the same "address already in
        # use", almost always a copy of this server that is already running.
        # Say so plainly instead of printing a traceback at whoever opens the
        # shop, and give the stop command for the machine actually in use.
        if e.errno not in (98, 10048):
            raise
        stop = ("    taskkill /f /im python.exe" if sys.platform.startswith("win")
                else f"    kill $(ss -ltnp | grep ':{PORT}' | grep -oP 'pid=\\K[0-9]+')")
        sys.exit(
            f"Порт {PORT} уже занят. Похоже, касса уже запущена.\n"
            f"Откройте http://localhost:{PORT}, либо остановите старую копию:\n"
            + stop)

    print(f"POS running on http://{HOST}:{PORT}  [{mode}]")
    try:
        srv.serve_forever()   # returns when /api/quit shuts the server down
        print("касса выключена с экрана")
    except KeyboardInterrupt:
        print("\nостановлено")
