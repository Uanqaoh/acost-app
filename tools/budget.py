#!/usr/bin/env python3
"""Monthly spending limits: one for everything, and one per category if wanted.

The limits live outside the repository because they are personal amounts:
  ~/acost/budget.json   (or --file / ACOST_BUDGET_FILE)

  budget.py set --amount 3000                        every month's total limit
  budget.py set --category Food --amount 600         one category; a parent name covers its subcategories
  budget.py remove [--category Food]                 drop the total limit, or one category's
  budget.py list
  budget.py status --ledger ~/acost/bin/ledger-ro    this month: spent, limit and what is left

Spending is counted the way the dashboard counts it: expenses minus refunds, in the month they are dated.
The dashboard shows the limits after its data file is rebuilt (tools/dashboard_data.py).
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recurring import Ledger, LedgerError, amount_cents, check_month, toronto_now  # noqa: E402


def budget_path(value=None):
    return Path(value or os.environ.get("ACOST_BUDGET_FILE") or "~/acost/budget.json").expanduser()


def check(budget):
    def cents(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if (not isinstance(budget, dict) or not set(budget) <= {"total_cents", "categories"}
            or ("total_cents" in budget and not cents(budget["total_cents"]))
            or not isinstance(budget.get("categories", {}), dict)
            or any(not isinstance(name, str) or not name.strip() or not cents(limit)
                   for name, limit in budget.get("categories", {}).items())):
        raise LedgerError("预算文件只能有 total_cents（整数分）和 categories（分类名: 整数分）。")
    return budget


def load(path):
    if not path.exists():
        return {}
    try:
        return check(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise LedgerError("读不了预算文件 %s：%s" % (path, error)) from None


def save(path, budget):
    check(budget)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".budget-")
    with os.fdopen(handle, "w", encoding="utf-8") as output:
        json.dump(budget, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def covers(name, category):
    return category == name or (category or "").startswith(name + "-")


def spent(entries, name=None):
    """Expenses minus refunds; with a name, only that category and its subcategories."""
    return sum(entry["amount_cents"] * (1 if entry.get("type") == "expense" else -1) for entry in entries
               if entry.get("type") in ("expense", "refund") and not entry.get("deleted_at")
               and (name is None or covers(name, entry.get("category"))))


def status(budget, entries):
    """One row per limit: [{name, spent_cents, limit_cents}], the total first under the name None."""
    rows = [{"name": None, "spent_cents": spent(entries), "limit_cents": budget["total_cents"]}] if budget.get("total_cents") else []
    return rows + [{"name": name, "spent_cents": spent(entries, name), "limit_cents": limit}
                   for name, limit in (budget.get("categories") or {}).items()]


def money(cents):
    return "%sC$%.2f" % ("-" if cents < 0 else "", abs(cents) / 100)


def sentence(row):
    name, used, limit = row["name"] or "总预算", row["spent_cents"], row["limit_cents"]
    left = "超了 %s" % money(used - limit) if used > limit else "还剩 %s" % money(limit - used)
    return "%s：已用 %s / %s，%s" % (name, money(used), money(limit), left)


def execute(args):
    path = budget_path(args.file)
    budget = load(path)
    if args.command == "list":
        return {"ok": True, "file": str(path), "budget": budget}
    if args.command == "status":
        month = check_month(args.month) if args.month else toronto_now().strftime("%Y-%m")
        if not budget:
            return {"ok": True, "month": month, "rows": [], "report": ["还没有设预算。"]}
        rows = status(budget, Ledger(args.ledger).entries(month))
        return {"ok": True, "month": month, "rows": rows, "report": [sentence(row) for row in rows]}
    name = (args.category or "").strip()
    if args.command == "set":
        limit = amount_cents(args.amount)
        if name:
            known = Ledger(args.ledger).categories()
            if not any(covers(name, category) for category in known):
                raise LedgerError("账本里没有「%s」这个分类，也没有它下面的子分类。核对分类名（ledger-ro meta）。" % name)
            budget.setdefault("categories", {})[name] = limit
        else:
            budget["total_cents"] = limit
        save(path, budget)
        return {"ok": True, "budget": budget, "next": "已设好。然后重新生成看板数据，看板上就有预算了。"}
    if name:
        if name not in budget.get("categories", {}):
            raise LedgerError("预算里没有「%s」。" % name)
        del budget["categories"][name]
        if not budget["categories"]:
            del budget["categories"]
    elif "total_cents" in budget:
        del budget["total_cents"]
    else:
        raise LedgerError("没有设总预算。")
    save(path, budget)
    return {"ok": True, "budget": budget, "next": "已去掉。然后重新生成看板数据。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="预算：每月总额和按分类的上限。金额由用户定。")
    parser.add_argument("--file", help="预算文件；默认 ~/acost/budget.json")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ro；set 分类和 status 时要用")
    anywhere = argparse.ArgumentParser(add_help=False)   # --ledger may also come after the command
    anywhere.add_argument("--ledger", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    setting = commands.add_parser("set", parents=[anywhere], help="设每月上限；不写 --category 就是总预算")
    setting.add_argument("--category", help="分类名；写父类（如 Food）就包含它下面所有子分类")
    setting.add_argument("--amount", required=True, help="每月上限，如 600")
    removing = commands.add_parser("remove", parents=[anywhere], help="去掉一个上限；不写 --category 就是去掉总预算")
    removing.add_argument("--category")
    commands.add_parser("list", parents=[anywhere], help="显示预算")
    showing = commands.add_parser("status", parents=[anywhere], help="这个月用了多少、还剩多少")
    showing.add_argument("--month", help="YYYY-MM；默认本月")
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except LedgerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
