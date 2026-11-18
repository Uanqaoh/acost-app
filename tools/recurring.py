#!/usr/bin/env python3
"""Monthly fixed charges (rent, loan, subscriptions): post them and feed the dashboard.

The list lives outside the repository because it holds personal amounts:
  ~/acost/recurring.json   (or --file / ACOST_RECURRING_FILE)

Every ledger call goes through a ledgerctl command, so its checks apply:
  --ledger '~/acost/bin/ledger-ai'   (or ACOST_LEDGER_CMD; default: this repo's ledgerctl.py)

  recurring.py post                      record charges that are due and not in the ledger yet
  recurring.py add --name Grok --category Bills-Subscriptions --day 19 --amount 47.45 --account Spending --match grok
  recurring.py add --name 车险 --every year --month-of-year 3 --day 15 ...   once a year
  recurring.py add --name 车贷 --every 2weeks --start 2026-11-06 ...          every 14 days from --start
  recurring.py cancel Grok               stop after today; past entries stay
  recurring.py set-amount Grok --amount 52.00   future charges only
  recurring.py confirm 手机 --amount 45.20       the real bill arrived: fix the amount, drop the "estimate" mark
  recurring.py export --month 2026-11    the dashboard's "recurring" data (read-only key is enough)
  recurring.py list
"""
import argparse
import calendar
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledgerctl import LedgerError, amount_cents, check_date, check_month, toronto_now  # noqa: E402

NOTE = "固定扣费自动记账"
CONFIRMED = "固定扣费已核对"
FIELDS = {"name", "category", "day", "amount_cents", "match", "account", "start", "end",
          "every", "month", "estimate", "allow_new_category"}
EVERY = ("month", "year", "2weeks")


class Ledger:
    """Runs ledgerctl (or a wrapper around it) and parses its JSON."""

    def __init__(self, command=None):
        command = command or os.environ.get("ACOST_LEDGER_CMD")
        self.command = ([os.path.expanduser(part) for part in shlex.split(command)] if command
                        else [sys.executable, str(ROOT / "ledgerctl.py")])

    def run(self, *args, payload=None):
        """Return (ok, parsed JSON). A failure carries ledgerctl's own error object."""
        try:
            done = subprocess.run(self.command + list(args), capture_output=True, text=True, timeout=120,
                                  input=None if payload is None else json.dumps(payload, ensure_ascii=False))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LedgerError("无法运行账本命令：" + str(error)) from None
        text = done.stdout if done.returncode == 0 else done.stderr
        try:
            result = json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise LedgerError("账本命令没有返回 JSON（退出码 %d）。" % done.returncode) from None
        return done.returncode == 0, result

    def need(self, *args):
        ok, result = self.run(*args)
        if not ok:
            raise LedgerError("账本读取失败：" + str(result.get("error")))
        return result

    def entries(self, month):
        found, offset = [], 0
        while offset is not None:
            page = self.need("list", "--month", month, "--limit", "500", "--offset", str(offset))
            found.extend(page.get("entries") or [])
            offset = page.get("next_offset")
        return found

    def categories(self):
        return {item.get("name") for item in self.need("meta").get("categories") or []}


def list_path(value=None):
    return Path(value or os.environ.get("ACOST_RECURRING_FILE") or "~/acost/recurring.json").expanduser()


def check_item(item):
    if not isinstance(item, dict) or not set(item) <= FIELDS:
        raise LedgerError("固定扣费清单里每项只能有：" + ", ".join(sorted(FIELDS)))
    for field in ("name", "category", "account"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise LedgerError("固定扣费缺少 " + field + "。")
    if ":" in item["name"]:
        raise LedgerError("名称里不能有冒号。")
    day, cents = item.get("day"), item.get("amount_cents")
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise LedgerError(item["name"] + "：day 必须是 1–31。")
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        raise LedgerError(item["name"] + "：amount_cents 必须是大于 0 的整数分。")
    check_date(item.get("start"))
    if item.get("end") is not None:
        check_date(item["end"])
    if item.get("every", "month") not in EVERY:
        raise LedgerError(item["name"] + "：every 只能是 month、year 或 2weeks。")
    if item.get("every") == "year":
        month = item.get("month")
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            raise LedgerError(item["name"] + "：按年扣的要写 month（1–12）。")
    for flag in ("estimate", "allow_new_category"):
        if not isinstance(item.get(flag, False), bool):
            raise LedgerError(item["name"] + "：" + flag + " 只能是 true 或 false。")
    if item.get("match") is not None and (not isinstance(item["match"], str) or not item["match"].strip()):
        raise LedgerError(item["name"] + "：match 不能为空。")
    return item


def overlaps(a, b):
    x, y = a["category"], b["category"]
    return x == y or x.startswith(y + "-") or y.startswith(x + "-")


def check_list(items):
    if not isinstance(items, list):
        raise LedgerError("固定扣费清单必须是数组。")
    for item in items:
        check_item(item)
    live = [item for item in items if not item.get("end")]
    names = [item["name"] for item in items]
    if len(set(names)) != len(names):
        raise LedgerError("固定扣费名称重复；换一个名称。")
    for index, a in enumerate(live):
        for b in live[index + 1:]:
            if overlaps(a, b) and not (a.get("match") and b.get("match")):
                raise LedgerError("「%s」和「%s」在同一个分类里，两项都要有 match（商家名里的一段文字）才能分清。"
                                  % (a["name"], b["name"]))
    return items


def load(path, required=False):
    if not path.exists():
        if required:  # an empty run must not look like "nothing was due"
            raise LedgerError("找不到固定扣费清单 %s。清单丢了就告诉用户，重新录入；不要当作没有要记的。" % path)
        return []
    try:
        return check_list(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise LedgerError("读不了固定扣费清单：" + str(error)) from None


def save(path, items):
    check_list(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".recurring-")
    with os.fdopen(handle, "w", encoding="utf-8") as output:
        json.dump(items, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def previous_month(month):
    first = dt.date(int(month[:4]), int(month[5:]), 1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def due_dates(item, month):
    """This item's charge dates in one month, inside its start/end dates."""
    year, mon = (int(part) for part in month.split("-"))
    every, last = item.get("every", "month"), calendar.monthrange(year, mon)[1]
    if every == "2weeks":
        day, dates = dt.date.fromisoformat(item["start"]), []
        if day < dt.date(year, mon, 1):
            day += dt.timedelta(days=14 * -((day - dt.date(year, mon, 1)).days // 14))
        while day <= dt.date(year, mon, last):
            dates.append(day.isoformat())
            day += dt.timedelta(days=14)
    elif every == "year" and item["month"] != mon:
        dates = []
    else:
        dates = ["%s-%02d" % (month, min(item["day"], last))]
    return [due for due in dates if item["start"] <= due and (not item.get("end") or due <= item["end"])]


def matches(item, entry):
    category = entry.get("category") or ""
    if entry.get("type") != "expense" or not (category == item["category"] or category.startswith(item["category"] + "-")):
        return False
    want = (item.get("match") or "").lower()
    return not want or want in ((entry.get("merchant") or "") + " " + (entry.get("note") or "")).lower()


def recorded(item, due, entries):
    """The entry already in the ledger for this charge: same month, or within 3 days for 2-week charges."""
    for entry in entries:
        if not matches(item, entry):
            continue
        if item.get("every") == "2weeks":
            near = abs((dt.date.fromisoformat(entry["date"]) - dt.date.fromisoformat(due)).days) <= 3
        else:
            near = entry["date"][:7] == due[:7]
        if near:
            return entry
    return None


def charge(ledger, item, date, due=None):
    """Record one charge. The key makes a second run for the same charge return the same entry."""
    note = NOTE
    if item.get("match") and item["match"].lower() not in item["name"].lower():
        note += " · " + item["match"]
    period = (due or date) if item.get("every") == "2weeks" else (due or date)[:7]
    args = ["add", "--json"] + (["--allow-new-category"] if item.get("allow_new_category") else [])
    ok, result = ledger.run(*args, payload={
        "type": "expense", "amount_cents": item["amount_cents"], "date": date, "merchant": item["name"],
        "category": item["category"], "account": item["account"], "note": note,
        "idempotency_key": "recurring:%s:%s" % (item["name"], period),
    })
    if ok:
        return {"name": item["name"], "posted": True, "id": result["entry"]["id"],
                "date": date, "amount_cents": item["amount_cents"]}
    return {"name": item["name"], "posted": False, "error": result.get("error"),
            "candidates": result.get("candidates")}


def post(ledger, items, today):
    """Charges due this month or last month, up to today, that the ledger does not have yet."""
    months = (previous_month(today[:7]), today[:7])
    due = [(item, date) for item in items for month in months for date in due_dates(item, month) if date <= today]
    entries = [entry for month in months for entry in ledger.entries(month)] if due else []
    return [charge(ledger, item, date, date) for item, date in due if not recorded(item, date, entries)]


def export(items, month):
    """The dashboard's "recurring" rows for one month: one per charge, without accounts."""
    keep = ("name", "category", "amount_cents", "match", "start", "end", "estimate")
    return [dict({key: item[key] for key in keep if item.get(key) is not None}, day=int(date[8:]))
            for item in items for date in due_dates(dict(item, end=None), month)
            if not item.get("end") or item["end"] >= month + "-01"]


def find(items, name):
    for item in items:
        if item["name"] == name:
            return item
    raise LedgerError("清单里没有「" + name + "」。")


def execute(args):
    path = list_path(args.file)
    items = load(path)
    today = args.date and check_date(args.date) or toronto_now().strftime("%Y-%m-%d")
    if args.command == "list":
        return {"ok": True, "file": str(path), "recurring": items}
    if args.command == "export":
        return export(items, check_month(args.month or today[:7]))
    ledger = Ledger(args.ledger)
    if args.command == "post":
        results = post(ledger, load(path, required=True), today)
        return {"ok": all(result["posted"] for result in results), "date": today, "results": results}
    if args.command == "add":
        start = check_date(args.start) if args.start else today
        item = {"name": args.name.strip(), "category": args.category, "amount_cents": amount_cents(args.amount),
                "day": args.day or int(start[8:]), "account": args.account, "start": start}
        if args.every != "month":
            item["every"] = args.every
        if args.every == "year":
            item["month"] = args.month_of_year
        elif args.every == "month" and not args.day:
            raise LedgerError("每月扣的要写 --day。")
        for field in ("match", "estimate", "allow_new_category"):
            if getattr(args, field):
                item[field] = getattr(args, field)
        check_list(items + [item])
        if not args.allow_new_category and item["category"] not in ledger.categories():
            raise LedgerError("分类「" + item["category"] + "」不在账本里。先问用户；用户同意新建后加 --allow-new-category。")
        save(path, items + [item])
        result, notes, existing = {"ok": True, "added": item["name"], "charges": []}, [], None
        if start[:7] > today[:7]:
            notes.append("开始日期 %s 在以后的月份，本月（%s）的固定扣费不会算这一项。刚订阅的，开始日期要写订阅当天；"
                         "已经手动记过的，程序会认出来，不会重复记。" % (start, today[:7]))
        if args.every == "month" and start[:7] == today[:7]:
            existing = recorded(item, today, ledger.entries(today[:7]))
            if existing:
                notes.append("本月已有这笔（%s，C$%.2f，id %s），不会重复记。"
                             % (existing["date"], existing["amount_cents"] / 100, existing["id"]))
        if notes:
            result["notes"] = notes
        if not args.no_post:
            if args.every == "month" and start == today:   # a new subscription is charged on the day it starts
                if not existing:
                    result["charges"] = [charge(ledger, item, today)]
            else:
                result["charges"] = post(ledger, [item], today)
            result["ok"] = all(done["posted"] for done in result["charges"])
        return result
    item = find(items, args.name)
    if args.command == "cancel":
        item["end"] = check_date(args.end) if args.end else today
        save(path, items)
        return {"ok": True, "cancelled": item["name"], "end": item["end"]}
    if args.command == "set-amount":
        item["amount_cents"] = amount_cents(args.amount)
        save(path, items)
        return {"ok": True, "name": item["name"], "amount_cents": item["amount_cents"]}
    if args.command == "confirm":
        months = (today[:7], previous_month(today[:7]))
        auto = [entry for month in months for entry in ledger.entries(month)
                if matches(item, entry) and NOTE in (entry.get("note") or "")]
        if not auto:
            raise LedgerError("最近两个月没有「" + item["name"] + "」的自动记账；如果这笔还没记，按普通账单记。")
        entry = max(auto, key=lambda found: found["date"])
        change = {"note": entry["note"].replace(NOTE, CONFIRMED), "expected_version": entry["version"]}
        if args.amount:
            change["amount_cents"] = amount_cents(args.amount)
        ok, result = ledger.run("edit", entry["id"], "--json", payload=change)
        if not ok:
            raise LedgerError("改不了这一笔：" + str(result.get("error")))
        return {"ok": True, "confirmed": item["name"], "id": entry["id"], "date": entry["date"],
                "amount_cents": result["entry"].get("amount_cents")}
    raise LedgerError("未知命令。")


def build_parser():
    parser = argparse.ArgumentParser(description="固定扣费：自动记账、增删改、给看板导出。")
    parser.add_argument("--file", help="清单路径；默认 ~/acost/recurring.json")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ai；默认本仓库的 ledgerctl.py")
    parser.add_argument("--date", help="把这一天当作今天（YYYY-MM-DD）；默认多伦多当天")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("post", help="到了扣款日、本月还没有的，按预计金额记一笔")
    commands.add_parser("list", help="显示清单")
    add = commands.add_parser("add", help="新增一项，并在当天记上第一笔")
    add.add_argument("--name", required=True)
    add.add_argument("--category", required=True, help="账本里已有的分类")
    add.add_argument("--day", type=int, help="几号扣；两周一次的不用写")
    add.add_argument("--every", choices=EVERY, default="month", help="month 每月（默认）、year 每年、2weeks 从 --start 起每 14 天")
    add.add_argument("--month-of-year", type=int, help="每年几月扣；--every year 时必填")
    add.add_argument("--estimate", action="store_true", help="金额每次不一样（如手机费）：自动记的是预计，看板会标出来")
    add.add_argument("--allow-new-category", action="store_true", help="仅在用户同意新建这个分类后使用")
    add.add_argument("--amount", required=True, help="预计金额，如 47.45")
    add.add_argument("--account", required=True)
    add.add_argument("--match", help="商家名或备注里的一段文字；同分类有多项时必填")
    add.add_argument("--start", help="开始日期；默认今天。也决定从哪个月起算进固定扣费：新订阅写订阅当天")
    add.add_argument("--no-post", action="store_true", help="只加进清单，不记今天这一笔")
    cancel = commands.add_parser("cancel", help="记下结束日期；历史账单保留")
    cancel.add_argument("name")
    cancel.add_argument("--end", help="结束日期；默认今天")
    change = commands.add_parser("set-amount", help="改预计金额，只影响以后")
    change.add_argument("name")
    change.add_argument("--amount", required=True)
    confirm = commands.add_parser("confirm", help="真实账单到了：改成实际金额，去掉预计标记")
    confirm.add_argument("name")
    confirm.add_argument("--amount", help="实际金额；和预计一样就不用写")
    export = commands.add_parser("export", help="输出看板模板的 recurring 数据")
    export.add_argument("--month", help="YYYY-MM；默认本月")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
    except LedgerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
