#!/usr/bin/env python3
"""Bakery order tracker. Standard library only. Run: python3 app.py
Then open http://127.0.0.1:8000

DATA MODEL (two linked tables; one day has many orders)
  orders: number, customer, product, qty, pickup, hours, status, note, source
  days:   date, hours_limit (8, or 12 with a helper), helper_booked, hours_booked, order_count
  (messages: the raw Instagram DMs and what happened to each one, for the activity log)

ORDER LIFECYCLE
  new order arrives -> breaks a rule?  yes: rejected (automatic)
                                       no:  requested -> baker rejects: rejected
                                                      -> "Accept all and go to next day": accepted
  accepted -> 5 PM on its pickup day: picked_up
  accepted -> cancellation with 2 days' notice: cancelled
  accepted -> baker undoes it (before the pickup day is over): back to requested

All automatic decisions are fixed rules. No AI inside.
"""
import csv
import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BAKERY_DB", os.path.join(HERE, "bakery.db"))
PORT = int(os.environ.get("PORT", "8000"))
SCHEMA_VERSION = "2"

# ---- Business rules -------------------------------------------------------
OVEN_HOURS_PER_UNIT = {"cupcakes": 0.5, "cake": 2.0}   # per dozen / per cake
PRICE_PER_UNIT = {"cupcakes": 40, "cake": 120}
DAY_HOURS = 8
HELPER_HOURS = 4
HELPER_COST = 100
NOTICE_DAYS = 2
YEAR = 2026
START_DAY = date(2026, 11, 23)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
BOOKED = ("accepted", "picked_up")   # statuses that use oven hours


class Refused(Exception):
    """A rule stopped the action. The message is shown to the baker."""


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ---- Small helpers --------------------------------------------------------
def hours_of(product, qty):
    return OVEN_HOURS_PER_UNIT[product] * qty


def price_of(product, qty):
    return PRICE_PER_UNIT[product] * qty


def fmt_day(iso):
    d = date.fromisoformat(iso) if isinstance(iso, str) else iso
    return f"{d.strftime('%a %b')} {d.day}"


def g(n):
    return round(float(n), 1)


def notice_ok(pickup_iso, today):
    return (date.fromisoformat(pickup_iso) - today).days >= NOTICE_DAYS


def notice_msg(what, pickup_iso, today):
    latest = date.fromisoformat(pickup_iso) - timedelta(days=NOTICE_DAYS)
    return (f"Needs {NOTICE_DAYS} days' notice: {what} for {fmt_day(pickup_iso)} had to come by "
            f"{fmt_day(latest)} (today is {fmt_day(today)}).")


def get_today(con):
    return date.fromisoformat(con.execute("SELECT v FROM meta WHERE k='today'").fetchone()[0])


def set_today(con, d):
    con.execute("INSERT OR REPLACE INTO meta VALUES('today',?)", (d.isoformat(),))


# ---- Database -------------------------------------------------------------
def create_tables(con):
    con.executescript("""
    CREATE TABLE orders(
        number TEXT PRIMARY KEY, customer TEXT, product TEXT, qty INTEGER, pickup TEXT,
        hours REAL, status TEXT, note TEXT, source TEXT);
    CREATE TABLE days(
        date TEXT PRIMARY KEY, hours_limit REAL, helper_booked INTEGER DEFAULT 0,
        hours_booked REAL DEFAULT 0, order_count INTEGER DEFAULT 0);
    CREATE TABLE messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT, kind TEXT, product TEXT, qty INTEGER,
        pickup TEXT, received TEXT, raw TEXT, handled INTEGER DEFAULT 0, result TEXT, outcome TEXT,
        order_number TEXT, UNIQUE(customer, received, raw));
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)


def init_db():
    con = db_connect()
    have = con.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone()
    version = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone() if have else None
    if not version or version[0] != SCHEMA_VERSION:
        for t in ("orders", "days", "messages", "meta", "helpers"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        create_tables(con)
        con.execute("INSERT INTO meta VALUES('schema',?)", (SCHEMA_VERSION,))
        start_fresh(con)
    con.commit()
    con.close()


def start_fresh(con):
    """Orders from before Monday 23 Nov + all Instagram messages (not yet fetched) + empty days."""
    con.execute("DELETE FROM orders")
    con.execute("DELETE FROM days")
    con.execute("DELETE FROM messages")
    load_csv(con)
    import_messages(con)
    set_today(con, START_DAY)
    last = max([date.fromisoformat(r[0]) for r in con.execute("SELECT pickup FROM orders UNION SELECT pickup FROM messages")])
    d = START_DAY
    while d <= last:
        ensure_day(con, d.isoformat())
        d += timedelta(days=1)
    rebuild_days(con)
    con.commit()


def ensure_day(con, iso):
    con.execute("INSERT OR IGNORE INTO days(date, hours_limit) VALUES(?,?)", (iso, DAY_HOURS))


def get_day(con, iso):
    ensure_day(con, iso)
    return con.execute("SELECT * FROM days WHERE date=?", (iso,)).fetchone()


def hours_left(con, iso):
    d = get_day(con, iso)
    return d["hours_limit"] - d["hours_booked"]


def rebuild_days(con):
    """Recompute hours_booked and order_count for every day from the Orders table."""
    con.execute("UPDATE days SET hours_booked=0, order_count=0")
    for r in con.execute(
            "SELECT pickup, SUM(hours) h, COUNT(*) n FROM orders WHERE status IN ('accepted','picked_up') GROUP BY pickup").fetchall():
        ensure_day(con, r["pickup"])
        con.execute("UPDATE days SET hours_booked=?, order_count=? WHERE date=?", (r["h"], r["n"], r["pickup"]))


def parse_item(text):
    """'3 Dozen Cupcakes' -> ('cupcakes', 3); '1 Cake' -> ('cake', 1)."""
    m = re.match(r"\s*(\d+)\s*(dozen\s+cupcakes?|cakes?)\s*$", text, re.I)
    if not m:
        return None
    return ("cake" if m.group(2).lower().startswith("cake") else "cupcakes"), int(m.group(1))


def parse_csv_day(s):  # 'Mon 23 Nov'
    _, d, mon = s.split()
    return date(YEAR, MONTHS[mon], int(d)).isoformat()


def parse_msg_day(s):  # 'Wed Nov 25'
    _, mon, d = s.split()
    return date(YEAR, MONTHS[mon], int(d)).isoformat()


def load_csv(con):
    with open(os.path.join(HERE, "existing_orders.csv"), newline="") as f:
        for r in csv.DictReader(f):
            product, qty = parse_item(r["item"])
            con.execute("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?, 'accepted', 'Taken before Monday 23 Nov.', 'csv')",
                        (r["order"], r["customer"], product, qty, parse_csv_day(r["pickup_day"]), hours_of(product, qty)))


def import_messages(con):
    """Read the seven InstagramOrders_<Day>.html files. Safe to run again."""
    for day in WEEKDAYS:
        path = os.path.join(HERE, f"InstagramOrders_{day}.html")
        if not os.path.exists(path):
            continue
        t = open(path, encoding="utf-8").read()
        dm = re.search(r'class="day">\w+ (\d+) (\w+) (\d{4})<', t)
        d = date(int(dm.group(3)), MONTHS[dm.group(2)[:3]], int(dm.group(1)))
        for m in re.finditer(r'<b>(.*?)</b> &middot; (\d+):(\d+) (AM|PM)</div><p class="message">(.*?)</p>', t, re.S):
            hh, mm, ap = int(m.group(2)), int(m.group(3)), m.group(4)
            hh = hh % 12 + (12 if ap == "PM" else 0)
            received = datetime(d.year, d.month, d.day, hh, mm).isoformat(timespec="minutes")
            raw = html.unescape(m.group(5)).strip()
            f = dict(re.findall(r"^(Name|Order|Pickup Date):\s*(.+)$", raw, re.M))
            if len(f) != 3:
                continue
            order = f["Order"].strip()
            product = qty = None
            if order.lower() == "cancel":
                kind = "cancel"
            elif order.lower().startswith("change to"):
                kind, (product, qty) = "change", parse_item(order[9:])
            else:
                kind, (product, qty) = "order", parse_item(order)
            con.execute(
                "INSERT OR IGNORE INTO messages(customer,kind,product,qty,pickup,received,raw) VALUES(?,?,?,?,?,?,?)",
                (f["Name"].strip(), kind, product, qty, parse_msg_day(f["Pickup Date"].strip()), received, raw))


# ---- Back end actions (slide 22): each one CHECKS the rules, then EDITS the data ----
def find_active_order(con, customer, pickup):
    return con.execute(
        "SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status IN ('requested','accepted') "
        "ORDER BY number DESC", (customer, pickup)).fetchone()


def next_number(con):
    n = con.execute("SELECT MAX(CAST(substr(number,2) AS INTEGER)) FROM orders").fetchone()[0] or 100
    return f"#{n + 1}"


def helper_hint(con, pickup_iso, hours, today):
    """Hint for the baker: a helper would have made room for an order that was turned away for lack of hours.
    Only when there is no helper that day yet and a helper can still be booked (2 days' notice)."""
    d = get_day(con, pickup_iso)
    if d["helper_booked"] or not notice_ok(pickup_iso, today):
        return ""
    if hours <= hours_left(con, pickup_iso) + HELPER_HOURS:
        return f" A helper (+{HELPER_HOURS} h, ${HELPER_COST}) would have made room for it."
    return ""


def fetch_new_order(con, m, today):
    """Check: 2 days' notice and enough hours left.  Edit: add a row to Orders (requested or rejected)."""
    h = hours_of(m["product"], m["qty"])
    status, note = "requested", "Breaks no rule. Waiting for you."
    if not notice_ok(m["pickup"], today):
        status, note = "rejected", notice_msg("orders", m["pickup"], today)
    elif h > hours_left(con, m["pickup"]):
        left = hours_left(con, m["pickup"])
        note = (f"Not enough oven hours: needs {g(h)}, only {g(left)} left on {fmt_day(m['pickup'])}."
                + helper_hint(con, m["pickup"], h, today))
        status = "rejected"
    num = next_number(con)
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                (num, m["customer"], m["product"], m["qty"], m["pickup"], h, status, note, f"msg{m['id']}"))
    return ("requested" if status == "requested" else "rejected"), note, num


def fetch_change(con, m, today):
    """Check: 2 days' notice and enough hours left.  Edit: update Orders and Days."""
    if not notice_ok(m["pickup"], today):
        return "rejected", notice_msg("changes", m["pickup"], today), None
    o = find_active_order(con, m["customer"], m["pickup"])
    if not o:
        return "rejected", f"No active order for {m['customer']} on {fmt_day(m['pickup'])}.", None
    new_h = hours_of(m["product"], m["qty"])
    left = hours_left(con, m["pickup"]) + (o["hours"] if o["status"] == "accepted" else 0)
    if new_h > left:
        return "rejected", f"Not enough oven hours for the change: needs {g(new_h)}, only {g(left)} available on {fmt_day(m['pickup'])}.", o["number"]
    con.execute("UPDATE orders SET product=?, qty=?, hours=? WHERE number=?", (m["product"], m["qty"], new_h, o["number"]))
    if o["status"] == "accepted":
        con.execute("UPDATE days SET hours_booked=hours_booked+? WHERE date=?", (new_h - o["hours"], m["pickup"]))
    return "applied", f"{o['number']} changed to {m['qty']} {'cake' if m['product']=='cake' else 'dozen cupcakes'}.", o["number"]


def fetch_cancellation(con, m, today):
    """Check: 2 days' notice.  Edit: update Orders and Days."""
    if not notice_ok(m["pickup"], today):
        return "rejected", notice_msg("cancellations", m["pickup"], today), None
    o = find_active_order(con, m["customer"], m["pickup"])
    if not o:
        return "rejected", f"No active order for {m['customer']} on {fmt_day(m['pickup'])}.", None
    con.execute("UPDATE orders SET status='cancelled', note='Cancelled by the customer.' WHERE number=?", (o["number"],))
    if o["status"] == "accepted":
        con.execute("UPDATE days SET hours_booked=hours_booked-?, order_count=order_count-1 WHERE date=?",
                    (o["hours"], m["pickup"]))
    return "applied", f"{o['number']} cancelled. {g(o['hours'])} oven hours freed.", o["number"]


def action_fetch(con):
    today = get_today(con)
    rows = con.execute("SELECT * FROM messages WHERE handled=0 AND substr(received,1,10)=? ORDER BY received",
                       (today.isoformat(),)).fetchall()
    if not rows:
        raise Refused("There are no new messages today.")
    for m in rows:
        fn = {"order": fetch_new_order, "change": fetch_change, "cancel": fetch_cancellation}[m["kind"]]
        result, note, num = fn(con, m, today)
        con.execute("UPDATE messages SET handled=1, result=?, outcome=?, order_number=? WHERE id=?",
                    (result, note, num, m["id"]))
    con.commit()


def action_reject(con, number):
    """Edit: set the order's status to rejected."""
    o = con.execute("SELECT * FROM orders WHERE number=?", (number,)).fetchone()
    if not o or o["status"] != "requested":
        raise Refused("Only requested orders can be rejected.")
    con.execute("UPDATE orders SET status='rejected', note='Rejected by the baker.' WHERE number=?", (number,))
    con.commit()


def action_accept(con, number):
    """Check: enough hours left that day.  Edit: Orders -> accepted, Days += hours and +1 order."""
    o = con.execute("SELECT * FROM orders WHERE number=?", (number,)).fetchone()
    if not o or o["status"] != "requested":
        raise Refused("Only requested orders can be accepted.")
    left = hours_left(con, o["pickup"])
    if o["hours"] > left:
        raise Refused(f"Not enough oven hours: needs {g(o['hours'])}, only {g(left)} left on {fmt_day(o['pickup'])}. "
                      "Book a helper first (needs 2 days' notice) or reject it.")
    con.execute("UPDATE orders SET status='accepted', note='Accepted.' WHERE number=?", (number,))
    con.execute("UPDATE days SET hours_booked=hours_booked+?, order_count=order_count+1 WHERE date=?",
                (o["hours"], o["pickup"]))
    con.commit()


def action_undo(con, number):
    """Check: order is accepted and its pickup day is not over.  Edit: Orders -> requested, Days -= hours and -1 order."""
    today = get_today(con)
    o = con.execute("SELECT * FROM orders WHERE number=?", (number,)).fetchone()
    if not o or o["status"] != "accepted":
        raise Refused("Only accepted orders can be undone.")
    if date.fromisoformat(o["pickup"]) < today:
        raise Refused("That pickup day is already over.")
    con.execute("UPDATE orders SET status='requested', note='Moved back to requested by the baker.' WHERE number=?", (number,))
    con.execute("UPDATE days SET hours_booked=hours_booked-?, order_count=order_count-1 WHERE date=?",
                (o["hours"], o["pickup"]))
    con.commit()


def action_accept_all_next_day(con):
    """Check each order, one by one: enough hours left.  Edit: Orders -> accepted, Days += hours.
    Then 5 PM: today's pickups -> picked_up. Then move to the next day."""
    today = get_today(con)
    unfetched = con.execute("SELECT COUNT(*) FROM messages WHERE handled=0 AND substr(received,1,10)=?",
                            (today.isoformat(),)).fetchone()[0]
    if unfetched:
        raise Refused(f"Fetch today's {unfetched} message{'s' if unfetched > 1 else ''} first.")
    for o in con.execute("SELECT * FROM orders WHERE status='requested' ORDER BY number").fetchall():
        if o["hours"] <= hours_left(con, o["pickup"]):
            con.execute("UPDATE orders SET status='accepted', note='Accepted.' WHERE number=?", (o["number"],))
            con.execute("UPDATE days SET hours_booked=hours_booked+?, order_count=order_count+1 WHERE date=?",
                        (o["hours"], o["pickup"]))
        else:
            con.execute("UPDATE orders SET status='rejected', note=? WHERE number=?",
                        (f"Not enough oven hours left on {fmt_day(o['pickup'])} by the time it was accepted."
                         + helper_hint(con, o["pickup"], o["hours"], today), o["number"]))
    con.execute("UPDATE orders SET status='picked_up', note='Picked up at 5 PM.' WHERE status='accepted' AND pickup<=?",
                (today.isoformat(),))
    set_today(con, today + timedelta(days=1))
    con.commit()


def action_helper(con, day_iso, book):
    """Check: 2 days' notice.  Edit: helper booked, and that day's hour limit becomes 12 (or back to 8)."""
    today = get_today(con)
    d = get_day(con, day_iso)
    if not notice_ok(day_iso, today):
        raise Refused(notice_msg("a helper", day_iso, today))
    if book:
        if d["helper_booked"]:
            raise Refused("A helper is already booked that day.")
        con.execute("UPDATE days SET helper_booked=1, hours_limit=? WHERE date=?", (DAY_HOURS + HELPER_HOURS, day_iso))
    else:
        if d["hours_booked"] > DAY_HOURS:
            raise Refused("The accepted orders need the helper's hours.")
        con.execute("UPDATE days SET helper_booked=0, hours_limit=? WHERE date=?", (DAY_HOURS, day_iso))
    con.commit()


def action_reset(con):
    start_fresh(con)
    con.commit()


# ---- Conflicts: things the baker should look at ---------------------------
def days_mismatches(con):
    """Days must always agree with Orders. Returns the dates where they don't."""
    bad = []
    for d in con.execute("SELECT * FROM days").fetchall():
        r = con.execute("SELECT COALESCE(SUM(hours),0) h, COUNT(*) n FROM orders WHERE pickup=? AND status IN ('accepted','picked_up')",
                        (d["date"],)).fetchone()
        if abs(r["h"] - d["hours_booked"]) > 1e-9 or r["n"] != d["order_count"]:
            bad.append(d["date"])
    return bad


def find_conflicts(con, days, requested, today):
    out = []
    for iso in days_mismatches(con):
        out.append({"level": "error", "day": iso, "title": f"{fmt_day(iso)}: the numbers don't match",
                    "detail": "The day's hours or order count disagree with its orders. This should never happen."})
    for d in days:
        if d["hours_booked"] > d["hours_limit"]:
            out.append({"level": "error", "day": d["date"], "title": f"{d['label']} is overbooked",
                        "detail": f"{g(d['hours_booked'])} oven hours booked, only {g(d['hours_limit'])} available."})
        if d["helper_booked"] and d["hours_booked"] <= DAY_HOURS and not d["past"]:
            out.append({"level": "warn", "day": d["date"], "title": f"Helper on {d['label']} isn't needed",
                        "detail": f"Only {g(d['hours_booked'])} oven hours booked, which fits in {DAY_HOURS}. Removing the helper saves $100."})
        names = {}
        for o in d["orders"]:
            names.setdefault(o["customer"].lower(), []).append(o["number"])
        for n, nums in names.items():
            if len(nums) > 1:
                out.append({"level": "warn", "day": d["date"], "title": f"{n.title()} has {len(nums)} orders on {d['label']}",
                            "detail": f"{', '.join(nums)}. Each order is one product: is this a duplicate?"})
        req = [o for o in requested if o["pickup"] == d["date"]]
        need = sum(o["hours"] for o in req)
        if len(req) > 1 and need > d["hours_left"]:
            out.append({"level": "warn", "day": d["date"], "title": f"Requested orders compete for {d['label']}",
                        "detail": f"{len(req)} requested orders need {g(need)} oven hours but only {g(d['hours_left'])} are left. "
                                  "When you accept all, the later ones will be rejected. Reject the ones you don't want first."})
    out.sort(key=lambda c: c["level"] != "error")
    return out


# ---- State sent to the page -----------------------------------------------
def build_state(con):
    today = get_today(con)
    iso_today = today.isoformat()
    orders = []
    for o in con.execute("SELECT * FROM orders ORDER BY pickup, number").fetchall():
        d = dict(o)
        d["pickup_label"] = fmt_day(o["pickup"])
        d["price"] = price_of(o["product"], o["qty"])
        orders.append(d)
    by_day = {}
    for o in orders:
        if o["status"] in BOOKED:
            by_day.setdefault(o["pickup"], []).append(o)
    requested = [o for o in orders if o["status"] == "requested"]
    days = []
    for r in con.execute("SELECT * FROM days ORDER BY date").fetchall():
        d = dict(r)
        dd = date.fromisoformat(r["date"])
        d["label"] = fmt_day(dd)
        d["past"] = dd < today
        d["is_today"] = dd == today
        d["hours_left"] = r["hours_limit"] - r["hours_booked"]
        d["orders"] = by_day.get(r["date"], [])
        d["revenue"] = sum(o["price"] for o in d["orders"])
        d["requested_hours"] = sum(o["hours"] for o in requested if o["pickup"] == r["date"])
        d["can_change_helper"] = notice_ok(r["date"], today)
        days.append(d)
    msgs = []
    for m in con.execute("SELECT * FROM messages ORDER BY received").fetchall():
        d = dict(m)
        d["received_label"] = datetime.fromisoformat(m["received"]).strftime("%a %-I:%M %p")
        d["pickup_label"] = fmt_day(m["pickup"])
        d["today"] = m["received"][:10] == iso_today
        msgs.append(d)
    unfetched = [m for m in msgs if m["today"] and not m["handled"]]
    last_day = max(date.fromisoformat(x["date"]) for x in days)
    booked = [o for o in orders if o["status"] in BOOKED]
    helpers = sum(1 for d in days if d["helper_booked"])
    revenue = sum(o["price"] for o in booked)
    return {
        "today": iso_today, "today_label": f"{WEEKDAYS[today.weekday()]} {today.strftime('%b')} {today.day}",
        "unfetched": len(unfetched), "can_advance": today <= last_day, "last_day": last_day.isoformat(),
        "messages": msgs, "orders": orders, "days": days, "requested": requested,
        "conflicts": find_conflicts(con, days, requested, today),
        "summary": {"accepted": len([o for o in orders if o["status"] == "accepted"]),
                    "picked_up": len([o for o in orders if o["status"] == "picked_up"]),
                    "requested": len(requested), "revenue": revenue, "helper_cost": helpers * HELPER_COST,
                    "profit": revenue - helpers * HELPER_COST},
    }


# ---- Web server -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            con = db_connect()
            try:
                self.send(200, build_state(con))
            finally:
                con.close()
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad request"})
        con = db_connect()
        try:
            if self.path == "/api/fetch":
                action_fetch(con)
            elif self.path == "/api/accept":
                action_accept(con, body.get("number", ""))
            elif self.path == "/api/undo":
                action_undo(con, body.get("number", ""))
            elif self.path == "/api/reject":
                action_reject(con, body.get("number", ""))
            elif self.path == "/api/next_day":
                action_accept_all_next_day(con)
            elif self.path == "/api/helper":
                action_helper(con, body.get("day", ""), bool(body.get("book")))
            elif self.path == "/api/reset":
                action_reset(con)
            else:
                return self.send(404, {"error": "not found"})
            self.send(200, {"ok": True})
        except Refused as e:
            con.rollback()
            self.send(400, {"error": str(e)})
        finally:
            con.close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bakery Orders</title>
<style>
:root{--ink:#201c17;--muted:#6f675d;--line:#e4e0d9;--bg:#faf8f5;--card:#fff;--track:#ebe7e0;
--accent:#0f766e;--tint:#9fd3cd;--accenthover:#115e59;--over:#c62828}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:16px 20px 0;max-width:1180px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;gap:10px 16px;flex-wrap:wrap}
header h1{margin:0;font-size:22px}
main{max-width:1180px;margin:0 auto;padding:12px 16px 60px}
button.btn{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;padding:7px 12px;font:inherit;font-size:14px;cursor:pointer}
button.btn:hover{background:#f3f0ea}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}button.primary:hover{background:var(--accenthover)}
button.sm{padding:4px 10px;font-size:13px}
button:disabled{opacity:.4;cursor:not-allowed}
.tb{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tb .date{font-weight:600;margin-right:4px}
/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,118px));gap:10px;margin:14px 0}
@media(max-width:560px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}
.tile{aspect-ratio:1/1;background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px;display:flex;flex-direction:column;justify-content:space-between}
.tile span{font-size:12px;color:var(--muted)}.tile b{font-size:26px;line-height:1}
.tile.review b{color:var(--accent)}
/* bake today */
.bake{background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin-bottom:12px}
.bake h2{margin:0 0 6px;font-size:15px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.bake h2 small{font-weight:400;color:var(--muted)}
.bake .it{display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-top:1px solid var(--line);font-size:14px}
.bake .it:first-of-type{border-top:0}
/* conflicts */
.cf{border-radius:12px;border:1px solid var(--line);background:#fff;margin-bottom:14px;overflow:hidden}
.cf>summary{list-style:none;cursor:pointer;padding:10px 14px;font-weight:600}
.cf>summary::-webkit-details-marker{display:none}
.cf.ok>summary{color:var(--muted);font-weight:500}
.cf.err{border-color:var(--over)}.cf.err>summary{color:var(--over)}
.cfi{display:flex;gap:10px;padding:9px 14px;border-top:1px solid var(--line);align-items:flex-start;cursor:pointer}
.cfi:hover{background:#f7f4ef}
.dot{width:9px;height:9px;border-radius:50%;margin-top:7px;flex:none;background:var(--ink)}.dot.error{background:var(--over)}
.cfi small{display:block;color:var(--muted)}
.grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:18px;align-items:start}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.col h2{font-size:16px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.col h2 small{font-weight:400;color:var(--muted)}
.col h2.sp{margin-top:18px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.msg .row{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.msg .who{color:var(--muted);font-size:13px}
.msg h3{margin:2px 0;font-size:17px}
.facts{display:flex;gap:6px 16px;flex-wrap:wrap;font-size:14px;color:var(--muted);margin:2px 0 8px}
.facts b{color:var(--ink)}
.warnline{padding:6px 10px;border-radius:8px;background:#f3f0ea;font-size:13px;margin:8px 0}
.why{font-size:13px;color:var(--muted);margin-top:6px}
.meter{height:10px;background:var(--track);border-radius:5px;overflow:hidden;display:flex}
.meter i{display:block;height:100%}
.seg-used{background:var(--accent)}.seg-add{background:var(--tint)}.seg-pend{background:var(--tint)}
.seg-free{background:transparent}.seg-over{background:var(--over)}
.mlabel{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:3px;gap:8px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.log{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.log>div{padding:8px 12px;border-top:1px solid var(--line);font-size:14px}.log>div:first-child{border-top:0}
.log small{display:block;color:var(--muted)}
.day{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 12px;margin-bottom:8px}
.day.today{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}.day.past{opacity:.65}.day.bad{border-color:var(--over)}
.day .hd{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.day .hd b{font-size:15px}
.hb{border:1px solid var(--line);border-radius:6px;padding:0 7px;font-size:12px;color:var(--muted);font-weight:500}
.alert{color:var(--over);font-weight:600}
.hrow{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:0 0 8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.legend i.f{background:var(--track)}
.od{border-top:1px solid var(--line);padding:8px 0;font-size:14px}
.od:first-child{border-top:0}
.od .l1{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.od .l2{color:var(--muted)}.od .l3{color:var(--muted);font-size:13px}
.ods{margin-top:8px;border-top:1px solid var(--line)}
.st{white-space:nowrap;font-size:13px;color:var(--muted)}
.sd{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:0}
.s-requested{border:2px solid var(--accent);background:#fff}.s-accepted{background:var(--accent)}
.s-picked_up{background:var(--muted)}.s-rejected,.s-cancelled{background:#bdb6ab}
.more{margin-top:20px}.more summary{cursor:pointer;font-weight:600;padding:8px 0}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}th{background:#f3f0ea;font-size:12px;color:var(--muted)}
tr.cancelled td,tr.rejected td{color:#9a9389}
.empty{color:var(--muted);padding:18px;text-align:center;background:#fff;border:1px dashed var(--line);border-radius:12px}
</style></head><body>
<header><h1>🧁 Home Bakery Orders</h1><div class="tb" id="tb"></div></header>
<main>
<div class="tiles" id="tiles"></div>
<div id="bake"></div>
<details class="cf ok" id="cf"></details>
<div class="grid">
 <section class="col" id="leftcol"></section>
 <section class="col" id="weekcol"></section>
</div>
<details class="more"><summary>All orders</summary><div class="tw" id="orders"></div></details>
<details class="more"><summary>How it works</summary><div id="rules"></div></details>
</main>
<script>
let S=null;const openDays=new Set();
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=n=>'$'+Number(n).toLocaleString();
const g=n=>+(+n).toFixed(1);
const itemText=(p,q)=>p==='cake'?`${q} cake${q>1?'s':''}`:`${q} dozen cupcakes`;
const statusLabel={requested:'Requested',accepted:'Accepted',picked_up:'Picked up',rejected:'Rejected',cancelled:'Cancelled'};
const stat=s=>`<span class="st"><i class="sd s-${s}"></i>${statusLabel[s]}</span>`;
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const j=await r.json(); if(!r.ok) alert(j.error||'Something went wrong'); await load(); return r.ok;
}
async function load(){S=await (await fetch('/api/state')).json();render();}
let pending=null;
function ask(k){pending=k;render();}
function nextDay(){
  if(S.requested.length&&pending!=='next')return ask('next');
  pending=null;api('/api/next_day');
}
function startOver(){
  if(pending!=='reset')return ask('reset');
  pending=null;openDays.clear();api('/api/reset');
}
function toolbar(){
  const n=S.requested.length;
  if(pending){
    const msg=pending==='reset'?'Go back to Monday morning and undo everything?'
      :`${n} requested order${n>1?'s are':' is'} still waiting for your decision. Accept ${n>1?'them':'it'} (where they fit) and go to the next day?`;
    $('#tb').innerHTML=`<span class="date" style="font-weight:500">${msg}</span>
    <button class="btn sm primary" onclick="${pending==='reset'?'startOver()':'nextDay()'}">Yes, do it</button>
    <button class="btn sm" onclick="pending=null;render()">Cancel</button>`;
    return;
  }
  $('#tb').innerHTML=`<span class="date">📅 ${S.today_label}</span>
  <button class="btn sm ${S.unfetched?'primary':''}" ${S.unfetched?'':'disabled'} onclick="api('/api/fetch')">Fetch messages (${S.unfetched})</button>
  <button class="btn sm ${S.unfetched?'':'primary'}" ${S.unfetched||!S.can_advance?'disabled':''} title="${S.unfetched?'Fetch the messages first.':''}" onclick="nextDay()">${S.can_advance?'Accept all and go to next day':'Week finished'}</button>
  <button class="btn sm" onclick="startOver()">Start over</button>`;
}
function tiles(){
  const s=S.summary;
  $('#tiles').innerHTML=[['To review',s.requested,'review'],['Accepted',s.accepted],['Picked up',s.picked_up],['Sales',money(s.revenue)],['Helpers',money(s.helper_cost)],['Profit',money(s.profit)]]
   .map(([a,b,c])=>`<div class="tile ${c||''}"><span>${a}</span><b>${b}</b></div>`).join('');
}
function bake(){
  const list=S.orders.filter(o=>o.status==='accepted'&&o.pickup===S.today);
  const h=list.reduce((a,o)=>a+o.hours,0);
  $('#bake').innerHTML=`<div class="bake"><h2><span>Bake today (${S.today_label})</span><small>${list.length?`${g(h)} oven hours · pickups at 5 PM`:''}</small></h2>`+
   (list.length?list.map(o=>`<div class="it"><span>${esc(itemText(o.product,o.qty))} · ${esc(o.customer)}</span><span>${o.number} · ${g(o.hours)} h</span></div>`).join(''):`<div style="color:var(--muted);font-size:14px">Nothing to bake today.</div>`)+`</div>`;
}
function conflicts(){
  const c=S.conflicts, el=$('#cf'), errs=c.filter(x=>x.level==='error').length;
  el.className='cf '+(c.length?(errs?'err':'has'):'ok');
  if(!el.dataset.init){el.open=errs>0;el.dataset.init=1;}
  el.innerHTML=`<summary>${c.length?`${c.length} conflict${c.length>1?'s':''} to look at`+(errs?` (${errs} serious)`:''):'✓ No conflicts: every booked day fits the oven'}</summary>`+
   c.map((x,i)=>`<div class="cfi" onclick="jump(${i})"><span class="dot ${x.level}"></span><div><b>${esc(x.title)}</b><small>${esc(x.detail)}</small></div></div>`).join('');
}
function jump(i){
  const x=S.conflicts[i]; if(!x.day)return;
  openDays.add(x.day);render();const el=document.getElementById('d'+x.day);if(el)el.scrollIntoView({behavior:'smooth',block:'center'});
}
function meter(before,add,limit){
  const after=before+add, max=Math.max(limit,after), p=x=>(x/max*100).toFixed(1)+'%';
  return `<div class="meter"><i class="seg-used" style="width:${p(before)}"></i><i class="${after>limit?'seg-over':'seg-add'}" style="width:${p(add)}"></i></div>`;
}
/* ---- replies for the baker to paste into Instagram ---- */
function first(n){return String(n).split(' ')[0];}
function why(note){
  note=note||'';
  if(note.startsWith('Needs 2 days'))return 'I need at least 2 days\' notice for orders, changes and cancellations.';
  if(note.includes('oven hours'))return 'I\'m fully booked that day.';
  if(note.startsWith('No active order'))return 'I couldn\'t find an order under your name for that day.';
  return 'I\'m not able to fit it in.';
}
function replyOrder(o){
  const it=itemText(o.product,o.qty);
  if(o.status==='accepted'||o.status==='picked_up')return `Hi ${first(o.customer)}, your order ${o.number} (${it}) is confirmed for pickup on ${o.pickup_label} at 5 PM. Total ${money(o.price)}. Thank you!`;
  if(o.status==='cancelled')return `Hi ${first(o.customer)}, your order ${o.number} (${it}, ${o.pickup_label}) has been cancelled. Thank you for letting me know!`;
  if(o.status==='rejected')return `Hi ${first(o.customer)}, sorry, I can't take your order for ${it} on ${o.pickup_label}. ${why(o.note)}`;
  return '';
}
function replyMsg(m){
  const o=m.order_number&&m.kind==='order'?S.orders.find(x=>x.number===m.order_number):null;
  if(o&&o.status!=='requested')return replyOrder(o);
  if(m.kind==='order')return `Hi ${first(m.customer)}, thanks for your order! I'll confirm it shortly.`;
  const ok=m.result==='applied', what=m.kind==='cancel'?'cancellation':'change';
  return ok?`Hi ${first(m.customer)}, your ${what} for ${m.pickup_label} is done. ${m.outcome}`
           :`Hi ${first(m.customer)}, sorry, I can't make that ${what} for ${m.pickup_label}. ${why(m.outcome)}`;
}
function copyBtn(text){return text?`<button class="btn sm" onclick="copyText(this,decodeURIComponent('${encodeURIComponent(text)}'))">Copy reply</button>`:'';}
async function copyText(btn,t){
  try{await navigator.clipboard.writeText(t);}catch(e){const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();}
  const old=btn.textContent;btn.textContent='Copied ✓';setTimeout(()=>btn.textContent=old,1400);
}
function reqCard(o){
  const d=S.days.find(x=>x.date===o.pickup);
  const rivals=S.requested.filter(x=>x.pickup===o.pickup);
  const need=rivals.reduce((a,x)=>a+x.hours,0);
  const fits=o.hours<=d.hours_left;
  const reason=fits?'':`Can't accept: needs ${g(o.hours)} h but only ${g(d.hours_left)} h are left on ${o.pickup_label}. ${d.helper_booked?'':(d.can_change_helper?'Book a helper on that day, or reject.':'A helper can no longer be booked (2 days\' notice). Reject it.')}`;
  return `<div class="msg"><div class="row"><span><b>${esc(o.customer)}</b> · new order</span><span class="who">${o.number}</span></div>
   <h3>${esc(itemText(o.product,o.qty))}</h3>
   <div class="facts"><span>Pickup <b>${o.pickup_label}</b></span><span><b>${money(o.price)}</b> · ${g(o.hours)} oven h</span></div>
   ${meter(d.hours_booked,o.hours,d.hours_limit)}
   <div class="mlabel"><span>Oven on ${o.pickup_label}</span><span>${g(d.hours_booked)} → <b>${g(d.hours_booked+o.hours)}</b> of ${g(d.hours_limit)} h</span></div>
   ${rivals.length>1&&need>d.hours_left?`<div class="warnline">${rivals.length} requested orders for this day need ${g(need)} h but only ${g(d.hours_left)} h are left. Accept the ones you want first; the rest will be rejected.</div>`:''}
   <div class="actions"><button class="btn primary" ${fits?'':'disabled'} onclick="api('/api/accept',{number:'${o.number}'})">Accept</button>
   <button class="btn" onclick="api('/api/reject',{number:'${o.number}'})">Reject</button></div>
   ${reason?`<div class="why">${esc(reason)}</div>`:''}</div>`;
}
function left(){
  const act=S.messages.filter(m=>m.today&&m.handled);
  let h=`<h2>Requested orders <small>${S.requested.length} waiting</small></h2>`+
   (S.requested.length?S.requested.map(reqCard).join(''):`<div class="empty">${S.unfetched?'Fetch today\'s messages to see new orders.':'Nothing waiting.'}</div>`);
  h+=`<h2 class="sp">Handled automatically today</h2>`;
  h+=act.length?`<div class="log">`+act.map(m=>{
    const what=m.kind==='cancel'?'Cancel':(m.kind==='change'?'Change to ':'')+(m.product?itemText(m.product,m.qty):'');
    const good=m.result==='applied'||m.result==='requested';
    const rep=m.result==='requested'?'':copyBtn(replyMsg(m));
    return `<div><b>${esc(m.customer)}</b> · ${esc(what)} · ${m.pickup_label}<small>${good?'✓':'✕'} ${esc(m.outcome)}</small>${rep?`<div class="actions" style="margin-top:6px">${rep}</div>`:''}</div>`;}).join('')+`</div>`
   :`<div class="empty">Nothing yet today.</div>`;
  $('#leftcol').innerHTML=h;
}
function dayOrders(d){
  const rows=[...d.orders,...S.requested.filter(o=>o.pickup===d.date)];
  if(!rows.length)return `<div class="ods"><div class="od l2">No orders</div></div>`;
  return `<div class="ods">`+rows.map(o=>`<div class="od"><div class="l1"><span><b>${o.number}</b> · ${esc(o.customer)}</span>${stat(o.status)}</div>
    <div class="l2">${esc(itemText(o.product,o.qty))} · ${money(o.price)} · ${g(o.hours)} oven h</div>
    <div class="l3">${esc(o.note)}</div>
    ${o.status==='accepted'||o.status==='picked_up'?`<div class="actions" style="margin-top:6px">${copyBtn(replyOrder(o))}${o.status==='accepted'&&!d.past?`<button class="btn sm" onclick="api('/api/undo',{number:'${o.number}'})">Undo accept</button>`:''}</div>`:''}</div>`).join('')+`</div>`;
}
function toggleDay(iso){openDays.has(iso)?openDays.delete(iso):openDays.add(iso);render();}
function week(){
  $('#weekcol').innerHTML=`<h2>Days</h2>
  <div class="legend"><span><i class="seg-used"></i>booked</span><span><i class="seg-pend"></i>requested</span><span><i class="f"></i>free</span><span><i class="seg-over"></i>overbooked</span></div>`+
  S.days.map(d=>{
    const lim=d.hours_limit, max=Math.max(lim,d.hours_booked+d.requested_hours), p=x=>(x/max*100).toFixed(1)+'%';
    const over=d.hours_booked>lim;
    const bar=over?`<i class="seg-used" style="width:${p(lim)}"></i><i class="seg-over" style="width:${p(d.hours_booked-lim)}"></i>`
      :`<i class="seg-used" style="width:${p(d.hours_booked)}"></i><i class="seg-pend" style="width:${p(d.requested_hours)}"></i>`;
    const open=openDays.has(d.date), nReq=S.requested.filter(o=>o.pickup===d.date).length;
    return `<div class="day ${d.is_today?'today':''} ${d.past?'past':''} ${over?'bad':''}" id="d${d.date}">
     <div class="hd"><b>${d.label} ${d.is_today?'<span class="hb">today</span> ':''}${d.helper_booked?'<span class="hb">+ helper</span>':''}</b><span>${g(d.hours_booked)} / ${g(lim)} h</span></div>
     <div class="meter" style="margin-top:6px">${bar}</div>
     <div class="mlabel"><span>${d.order_count} order${d.order_count===1?'':'s'} · ${money(d.revenue)}</span><span>${over?'<span class="alert">overbooked</span>':`${g(d.hours_left)} h left`}</span></div>
     <div class="hrow"><button class="btn sm" onclick="toggleDay('${d.date}')">${open?'Hide orders':'Show orders'}${nReq?` (+${nReq} requested)`:''}</button>
     ${d.past?'':(d.helper_booked
        ?`<button class="btn sm" ${d.can_change_helper&&d.hours_booked<=8?'':'disabled'} onclick="api('/api/helper',{day:'${d.date}',book:false})">Remove helper</button><span>helper booked ($100)</span>`
        :`<button class="btn sm" ${d.can_change_helper?'':'disabled'} onclick="api('/api/helper',{day:'${d.date}',book:true})">Book helper ($100, +4 h)</button>${d.can_change_helper?'':'<span>needs 2 days\' notice</span>'}`)}</div>
     ${open?dayOrders(d):''}</div>`;
  }).join('');
}
function extras(){
  $('#orders').innerHTML=`<table><tr><th>Order</th><th>Customer</th><th>Item</th><th>Pickup</th><th>Oven h</th><th>Price</th><th>Status</th><th>Note</th><th></th></tr>`+
   S.orders.map(o=>`<tr class="${o.status}"><td>${o.number}</td><td>${esc(o.customer)}</td><td>${itemText(o.product,o.qty)}</td><td>${o.pickup_label}</td><td>${g(o.hours)}</td><td>${money(o.price)}</td><td>${stat(o.status)}</td><td>${esc(o.note)}</td><td>${o.status==='requested'?'':copyBtn(replyOrder(o))}</td></tr>`).join('')+'</table>';
  $('#rules').innerHTML=`<ul>
  <li>1 dozen cupcakes: $40, 30 min in the oven. 1 cake: $120, 2 hours. One product per order.</li>
  <li>The oven runs 8 hours a day; a helper ($100) adds 4 hours to one day. Orders are baked on their pickup day and picked up at 5 PM.</li>
  <li>Orders, changes, cancellations and helper bookings need 2 days' notice before the pickup day.</li>
  <li><b>New order:</b> if it breaks the notice rule or doesn't fit in the day's free hours, it's rejected automatically (with a hint when a helper would have made room). Otherwise it becomes <i>requested</i> and waits for you.</li>
  <li><b>Accept / Reject:</b> decide each requested order. Accept needs enough free oven hours that day. <b>Undo accept</b> moves it back to requested.</li>
  <li><b>Change / cancellation:</b> applied automatically if the rules allow it, otherwise refused.</li>
  <li><b>Accept all and go to next day:</b> accepts every requested order that still fits, one by one, then moves to tomorrow.</li>
  <li><b>Copy reply:</b> copies a ready message to paste into the Instagram chat.</li>
  <li>The app uses fixed rules, not AI.</li></ul>`;
}
function render(){toolbar();tiles();bake();conflicts();left();week();extras();}
load();
</script></body></html>"""

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print("ok")
        sys.exit(0)
    print(f"Bakery app running: open http://127.0.0.1:{PORT}  (press Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
