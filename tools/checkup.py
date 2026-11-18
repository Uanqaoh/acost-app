#!/usr/bin/env python3
"""Daily ledger check-up. Read-only; prints problems only when there are some.

  checkup.py --ledger '~/acost/bin/ledger-ro'

Looks at this month and last month for:
  - categories that were not there at the previous check-up
  - entries whose line items do not add up to the amount
  - likely duplicates the service cannot catch (same amount, account and merchant, 1–3 days apart)
  - a spending limit from tools/budget.py that this month has reached 80% of, or gone over
Each problem is reported once; what was already reported is remembered in
~/acost/checkup_state.json (or --state / ACOST_CHECKUP_STATE). That file holds only
category names and entry IDs.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import budget  # noqa: E402
from recurring import Ledger, LedgerError, check_date, toronto_now  # noqa: E402


def previous_month(month):
    first = dt.date(int(month[:4]), int(month[5:]), 1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def money(cents):
    return "C$%.2f" % (cents / 100)


def label(entry):
    return "%s %s %s（ID %s）" % (entry.get("date"), entry.get("merchant") or "无商家",
                                 money(entry.get("amount_cents") or 0), entry.get("id"))


def item_problems(entries):
    for entry in entries:
        items = entry.get("items")
        if items and sum(item.get("amount_cents", 0) for item in items) != entry.get("amount_cents"):
            yield "items:" + str(entry.get("id")), "明细合计 %s 对不上金额：%s" % (
                money(sum(item.get("amount_cents", 0) for item in items)), label(entry))


def duplicate_problems(entries):
    spending = sorted((e for e in entries if e.get("type") == "expense" and (e.get("merchant") or "").strip()),
                      key=lambda e: e["date"])
    for index, a in enumerate(spending):
        for b in spending[index + 1:]:
            gap = (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days
            if gap > 3:
                break
            if (gap >= 1 and a["amount_cents"] == b["amount_cents"] and a.get("account") == b.get("account")
                    and a["merchant"].strip().lower() == b["merchant"].strip().lower()):
                yield "dup:" + ":".join(sorted((str(a.get("id")), str(b.get("id"))))), \
                    "可能重复：%s 和 %s" % (label(a), label(b))


def budget_problems(entries, month, limits):
    """Said once when a limit reaches 80%, and once more when it is passed."""
    this_month = [entry for entry in entries if entry.get("date", "")[:7] == month]
    for row in budget.status(limits, this_month):
        used, limit = row["spent_cents"], row["limit_cents"]
        mark = "over" if used > limit else "near" if used * 5 >= limit * 4 else None
        if mark:
            yield "budget:%s:%s:%s" % (month, row["name"] or "", mark), "预算 " + budget.sentence(row)


def run(ledger, state_path, today, budget_file=None):
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    month = today[:7]
    entries = ledger.entries(previous_month(month)) + ledger.entries(month)
    categories = sorted(name for name in ledger.categories() if name)
    problems = []
    if "categories" in state:  # the first run only records what exists
        problems += ["新分类：" + name for name in categories if name not in state["categories"]]
    reported = set(state.get("reported") or [])
    try:
        limits = budget.load(budget.budget_path(budget_file))
    except LedgerError as error:
        limits = {}
        problems.append(str(error))
    for key, text in (list(item_problems(entries)) + list(duplicate_problems(entries))
                      + list(budget_problems(entries, month, limits))):
        if key not in reported:
            reported.add(key)
            problems.append(text)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"categories": categories, "reported": sorted(reported)},
                                     ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"ok": True, "date": today, "problems": problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description="每日体检：只读，有问题才列出来。")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ro")
    parser.add_argument("--state", help="记住已报告内容的文件；默认 ~/acost/checkup_state.json")
    parser.add_argument("--date", help="把这一天当作今天（YYYY-MM-DD）")
    args = parser.parse_args(argv)
    state = Path(args.state or os.environ.get("ACOST_CHECKUP_STATE") or "~/acost/checkup_state.json").expanduser()
    try:
        today = check_date(args.date) if args.date else toronto_now().strftime("%Y-%m-%d")
        print(json.dumps(run(Ledger(args.ledger), state, today), ensure_ascii=False))
        return 0
    except LedgerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
