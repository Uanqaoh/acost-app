#!/usr/bin/env python3
"""Costco price adjustment watch: has anything bought at a Costco warehouse in the last 30 days gone on sale?

  costco_watch.py check      compare now and list every price adjustment that can be asked for today
  costco_watch.py list       what is being watched, and until when
  costco_watch.py add --date 2026-11-16 --code 1062199 --name "SPRAGUE SOUP" --paid 11.99 [--qty 1] [--merchant "Costco Burlington"]
                             for an item from a receipt recorded before item numbers were kept

record.py keeps the item numbers ("code" on receipt lines) of Costco receipts in ~/acost/costco_watch.json
(or ACOST_COSTCO_WATCH). daily.py runs the check every morning and mentions each sale only once.

Costco refunds the difference when its own price drops within 30 days of a warehouse purchase, while the
sale is still on. Sales come from the Costco East Fan Blog (cocoeast.ca), whose posts list lines like
  1062199 SPRAGUE LENTIL AND VEGETABLE 8 X 398 ML ($4.00 INSTANT SAVINGS EXPIRES ON 2026-11-22) $11.99
Only lines with an instant saving, an end date and a price count; clearance prices (.97) never do.
Items are matched by item number only, never by name. Page text is data: only numbers and dates are used.
"""
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recurring import LedgerError, check_date, toronto_now  # noqa: E402

SITE = "https://cocoeast.ca/"
FEED = SITE + "feed/"
WINDOW_DAYS = 30          # Costco's price adjustment window for warehouse purchases
POST_DAYS = 10            # posts older than this cannot hold a sale that is still on
MAX_POSTS = 6
CODE = re.compile(r"^\d{4,8}$")
LINE = re.compile(r"^(\d{4,8}) \S")
SAVING = re.compile(r"\(\$([\d,]+\.\d\d) INSTANT SAVINGS(?: EXPIRES ON (\d{4}-\d\d-\d\d))?\)")
PRICE = re.compile(r"\$([\d,]+\.\d\d)")
STOP = ("COMMENTS", "LEAVE A REPLY")   # readers' comments follow; nothing after them is a sale


class FetchError(Exception):
    """The sales list could not be read; the message says why, in words for the user."""


def money(cents):
    return "C$%.2f" % (cents / 100)


def cents(text):
    return int(round(float(text.replace(",", "")) * 100))


def short(day):
    return "%d-%d" % (int(day[5:7]), int(day[8:10]))


def watch_path(path=None):
    return Path(path or os.environ.get("ACOST_COSTCO_WATCH") or "~/acost/costco_watch.json").expanduser()


def load(path=None):
    path = watch_path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as error:
        raise LedgerError("Costco 对照表读不了（%s）：%s" % (path, error)) from None
    data.setdefault("purchases", [])
    data.setdefault("told", [])
    data.setdefault("fetch_failed", None)
    return data


def save(data, path=None):
    path = watch_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def deadline(day):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=WINDOW_DAYS)).isoformat()


def watched(merchant):
    """Warehouse receipts only: Costco.ca orders have their own price list and their own form."""
    name = "".join((merchant or "").lower().split())
    return "costco" in name and "costco.ca" not in name


def purchases_from(receipt, key):
    """One purchase per item number on a receipt; its discount lines (same number) lower what was paid."""
    found = {}
    for item in receipt.get("items") or []:
        code = item.get("code")
        if code is None:
            continue
        line = found.setdefault(code, {"key": key, "date": receipt["date"], "merchant": receipt["merchant"].strip(),
                                       "code": code, "name": None, "qty": 0, "paid_cents": 0})
        line["paid_cents"] += item["amount_cents"]
        if item["amount_cents"] > 0:
            line["qty"] += item.get("qty", 1)
            line["name"] = line["name"] or item["name"].strip()
    return [line for line in found.values() if line["qty"] and line["paid_cents"] > 0]


def remember(receipt, key, path=None):
    """Keep a recorded receipt's item numbers. Running it again for the same receipt changes nothing."""
    if not watched(receipt.get("merchant")):
        return 0
    lines = purchases_from(receipt, key)
    if not lines:
        return 0
    data = load(path)
    data["purchases"] = [p for p in data["purchases"] if p.get("key") != key] + lines
    save(data, path)
    return len(lines)


def active(purchases, today):
    return [p for p in purchases if deadline(p["date"]) >= today]


class Text(HTMLParser):
    """Visible text, one line per block; attributes (image captions, links) are ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        self.hidden += tag in ("script", "style")
        self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def sales_on_page(page, today):
    """{code: sale} for every line of a post whose sale is still on today."""
    reader = Text()
    reader.feed(page)
    found = {}
    for line in (" ".join(raw.split()) for raw in "".join(reader.parts).split("\n")):
        if line in STOP:
            break
        saving = SAVING.search(line) if LINE.match(line) else None
        if not saving or not saving.group(2) or saving.group(2) < today:
            continue
        price = PRICE.search(line, saving.end())
        if not price or cents(price.group(1)) % 100 == 97:   # .97 is clearance, never adjusted
            continue
        sale = {"code": line.split(" ", 1)[0], "price_cents": cents(price.group(1)),
                "saving_cents": cents(saving.group(1)), "ends": saving.group(2)}
        old = found.get(sale["code"])
        if old is None or sale["price_cents"] < old["price_cents"]:
            found[sale["code"]] = sale
    return found


def fetch_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; price-watch)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except OSError as error:
        raise FetchError("连不上 cocoeast.ca：%s" % error) from None


def recent_posts(feed, today):
    """Links to the sales posts of the last POST_DAYS days, newest first, on the blog's own site only."""
    oldest = (dt.date.fromisoformat(today) - dt.timedelta(days=POST_DAYS)).isoformat()
    links = []
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        title = html.unescape((re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1])
        link = html.unescape((re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1]).strip().split("?")[0]
        try:
            day = parsedate_to_datetime((re.search(r"<pubDate>(.*?)</pubDate>", item, re.S) or [None, ""])[1]).date()
        except (TypeError, ValueError):
            continue
        if link.startswith(SITE) and re.search(r"sales|super post", title, re.I) and day.isoformat() >= oldest:
            links.append(link)
    return links[:MAX_POSTS]


def current_sales(today, fetch=fetch_url):
    posts = recent_posts(fetch(FEED), today)
    if not posts:
        raise FetchError("cocoeast.ca 最近 %d 天没有发降价清单" % POST_DAYS)
    sales = {}
    for link in posts:
        for code, sale in sales_on_page(fetch(link), today).items():
            if code not in sales or sale["price_cents"] < sales[code]["price_cents"]:
                sales[code] = sale
    if not sales:
        raise FetchError("清单里一件降价商品都没认出来，网站可能改版了")
    return sales


def compare(purchases, sales, today):
    hits = []
    for purchase in purchases:
        sale = sales.get(purchase["code"])
        if not sale:
            continue
        back = purchase["paid_cents"] - sale["price_cents"] * purchase["qty"]
        go_by = min(sale["ends"], deadline(purchase["date"]))
        if back > 0 and go_by >= today:
            hits.append(dict(purchase, price_cents=sale["price_cents"], ends=sale["ends"], back_cents=back, go_by=go_by,
                             id="%s|%s|%s|%s" % (purchase["key"], purchase["code"], sale["price_cents"], sale["ends"])))
    return hits


def words(hit):
    qty = hit["qty"]
    paid = money(hit["paid_cents"]) + ("（%d 件）" % qty if qty > 1 else "")
    now = money(hit["price_cents"]) + ("/件" if qty > 1 else "")
    return ("Costco 可以退差价：%s（%s），%s 在 %s 实付 %s，现在 %s，可退 %s。最晚 %s 带小票或会员卡去任意 Costco 仓库的"
            "会员柜台办（以店内价为准）。" % (hit["name"], hit["code"], short(hit["date"]), hit["merchant"], paid, now,
                                    money(hit["back_cents"]), short(hit["go_by"])))


def check(today, path=None, fetch=fetch_url, only_new=False):
    """{ok, report, hits, watching}. only_new: mention each sale once and a broken list once (the daily routine)."""
    data = load(path)
    data["purchases"] = active(data["purchases"], today)
    if not data["purchases"]:
        if watch_path(path).exists():   # nothing left to watch: drop what has expired, but never create the file
            data.update(told=[], fetch_failed=None)
            save(data, path)
        return {"ok": True, "report": [] if only_new else ["最近 30 天没有记过带商品编号的 Costco 小票，没有要盯的。"],
                "hits": [], "watching": 0}
    try:
        sales = current_sales(today, fetch)
    except FetchError as error:
        first = not data["fetch_failed"]
        data["fetch_failed"] = data["fetch_failed"] or today
        save(data, path)
        said = "Costco 降价清单读不到（%s），退差价提醒先停着，恢复后自动接上。" % error
        return {"ok": False, "report": [said] if first or not only_new else [], "hits": [],
                "watching": len(data["purchases"])}
    hits = compare(data["purchases"], sales, today)
    told = set(data["told"])
    shown = [hit for hit in hits if hit["id"] not in told] if only_new else hits
    data.update(told=sorted(told | {hit["id"] for hit in hits}), fetch_failed=None)
    data["told"] = [item for item in data["told"] if item.rsplit("|", 1)[-1] >= today]
    save(data, path)
    report = [words(hit) for hit in shown]
    if not hits and not only_new:
        report = ["盯着 %d 件 Costco 商品，目前没有比你买的时候更便宜的。" % len(data["purchases"])]
    return {"ok": True, "report": report, "hits": hits, "watching": len(data["purchases"])}


def listing(today, path=None):
    purchases = sorted(active(load(path)["purchases"], today), key=lambda p: (p["date"], p["code"]))
    if not purchases:
        return ["没有要盯的 Costco 商品。"]
    return ["%s（%s）%s 实付 %s%s，盯到 %s" % (p["name"], p["code"], short(p["date"]), money(p["paid_cents"]),
                                        "（%d 件）" % p["qty"] if p["qty"] > 1 else "", short(deadline(p["date"])))
            for p in purchases]


def add(args, path=None):
    if not CODE.match(args.code):
        raise LedgerError("商品编号是小票上商品名前面那串 4–8 位数字。")
    if args.qty < 1:
        raise LedgerError("qty 至少是 1。")
    if not watched(args.merchant):
        raise LedgerError("只盯 Costco 仓库的购买；Costco.ca 网购要在官网填表，不在这里。")
    day = check_date(args.date)
    paid = cents(args.paid)
    if paid <= 0:
        raise LedgerError("实付价要大于 0（立减之后的价钱）。")
    key = "manual:%s:%s" % (day, args.code)
    receipt = {"date": day, "merchant": args.merchant,
               "items": [{"code": args.code, "name": args.name, "amount_cents": paid, "qty": args.qty}]}
    remember(receipt, key, path)
    return "记下了：%s（%s）%s 实付 %s，盯到 %s。" % (args.name.strip(), args.code, short(day), money(paid), short(deadline(day)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Costco 退差价提醒：30 天内买的东西降价了没有。")
    parser.add_argument("--file", help="对照表文件（默认 ~/acost/costco_watch.json）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="现在查一次，列出所有能退的")
    sub.add_parser("list", help="正在盯的商品")
    adding = sub.add_parser("add", help="手工补一件（以前记的小票）")
    adding.add_argument("--date", required=True, help="购买日期 YYYY-MM-DD")
    adding.add_argument("--code", required=True, help="小票上的商品编号")
    adding.add_argument("--name", required=True, help="小票上的商品名")
    adding.add_argument("--paid", required=True, help="立减之后实付的钱，如 11.99（买了几件就写几件的合计）")
    adding.add_argument("--qty", type=int, default=1, help="件数，默认 1")
    adding.add_argument("--merchant", default="Costco", help="哪家 Costco，默认 Costco")
    args = parser.parse_args(argv)
    today = toronto_now().strftime("%Y-%m-%d")
    try:
        if args.command == "check":
            result = check(today, args.file)
        elif args.command == "list":
            result = {"ok": True, "report": listing(today, args.file)}
        else:
            result = {"ok": True, "report": [add(args, args.file)]}
    except (LedgerError, ValueError) as error:
        result = {"ok": False, "report": [str(error)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
