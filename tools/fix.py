#!/usr/bin/env python3
"""Corrections that take several careful steps by hand, each as one command.

  fix.py --ledger ~/acost/bin/ledger-ai category ID Food-Dining         one entry gets another category
  fix.py --ledger ~/acost/bin/ledger-ai move-category OLD NEW           every entry in OLD moves to NEW

Without --apply nothing changes: it prints what it would do. Show that to the user, then add --apply.

An expense that has refunds cannot change category in place. So its refunds are taken off, the
category is changed, and the same refunds are put back (same amount, date, time and note; the key is
"category-fix:<old refund id>"). What is in progress is kept in ~/acost/journal, so a run that stops
half-way finishes when the same command is run again. Afterwards the totals of every month that was
touched are compared with what they were before; a category change must leave them the same.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_data  # noqa: E402
from record import Journal, money  # noqa: E402
from recurring import Ledger, LedgerError, toronto_now  # noqa: E402

TOTALS = ("expense_cents", "income_cents", "refund_cents")
REFUND = ("amount_cents", "date", "time", "note", "receipt_url", "items")


def must(ledger, *args, payload=None):
    ok, result = ledger.run(*args, payload=payload)
    if not ok:
        raise LedgerError("%s 没成功：%s" % (args[0], result.get("message") or result.get("error")))
    return result


def listing(ledger, *filters):
    found, offset = [], 0
    while offset is not None:
        page = ledger.need("list", *filters, "--limit", "500", "--offset", str(offset))
        found.extend(page.get("entries") or [])
        offset = page.get("next_offset")
    return found


def month_totals(ledger, months):
    totals = {}
    for month in sorted(months):
        summary = ledger.need("summary", "--month", month)
        totals[month] = {key: summary.get(key) for key in TOTALS}
    return totals


def brief(entry):
    return "%s %s %s（%s，ID %s）" % (entry.get("date"), entry.get("merchant") or "无商家",
                                   money(entry.get("amount_cents") or 0), entry.get("category") or "", entry.get("id"))


def refunds_of(ledger, entry, refunds=None):
    """This expense's live refunds, as the payloads that will put them back."""
    if not entry.get("refunded_cents"):
        return []
    found = refunds if refunds is not None else listing(ledger, "--type", "refund")
    now = toronto_now().strftime("%H:%M")
    plan = []
    for refund in found:
        if refund.get("original_id") != entry["id"]:
            continue
        payload = {key: refund[key] for key in REFUND if refund.get(key)}
        payload.setdefault("time", now)   # a refund cannot be recorded without a time
        payload["idempotency_key"] = "category-fix:" + refund["id"]
        plan.append({"old_id": refund["id"], "payload": payload})
    return plan


def change(ledger, entry, category, refunds=None):
    """Move one expense or income to `category`, taking its refunds off and putting them back."""
    journal = Journal("fix:category:" + entry["id"])
    if journal.data.get("to") not in (None, category):
        raise LedgerError("这一笔上次改成「%s」还没做完（ID %s）。先用同一条命令把那次跑完。" % (journal.data["to"], entry["id"]))
    if not journal.data:
        if entry.get("category") == category:
            return False
        journal.data = {"to": category, "refunds": refunds_of(ledger, entry, refunds)}
        journal.save()
    for item in journal.data["refunds"]:
        old = must(ledger, "get", item["old_id"], "--include-deleted")["entry"]
        if not old.get("deleted_at"):
            must(ledger, "delete", old["id"], "--expected-version", str(old["version"]))
    current = must(ledger, "get", entry["id"])["entry"]
    if current.get("category") != category:
        must(ledger, "edit", entry["id"], "--json", payload={"category": category, "expected_version": current["version"]})
    back = []
    for item in journal.data["refunds"]:
        args = ["refund", entry["id"], "--json"]
        ok, result = ledger.run(*args, payload=item["payload"])
        if not ok and result.get("error") == "possible_duplicate":   # two equal refunds of the same purchase
            ids = [candidate.get("id") for candidate in result.get("candidates") or []]
            if ids and set(ids) <= set(back):
                extra = [part for found in ids for part in ("--acknowledge-duplicate", found)]
                ok, result = ledger.run(*args, *extra, payload=item["payload"])
        if not ok:
            raise LedgerError("退款没能放回去（原退款 ID %s）：%s。原样再运行一次。"
                              % (item["old_id"], result.get("message") or result.get("error")))
        back.append(result["entry"]["id"])
    journal.path.unlink(missing_ok=True)
    return True


def run(ledger, name, entries, category, months, apply, refunds=None):
    """Change every entry, then check that no month's totals moved."""
    progress = Journal(name)
    if progress.data:   # an interrupted run: finish exactly the entries it set out to change
        entries = [must(ledger, "get", entry_id)["entry"] for entry_id in progress.data["ids"]]
        months = set(progress.data["before"])
    todo = [entry for entry in entries if entry.get("category") != category
            or Journal("fix:category:" + entry["id"]).data]
    with_refunds = [entry for entry in todo if entry.get("refunded_cents")]
    result = {"ok": True, "applied": apply, "to": category, "count": len(todo), "months": sorted(months),
              "with_refunds": len(with_refunds), "examples": [brief(entry) for entry in todo[:8]]}
    if not todo and not progress.data:
        result["next"] = "没有要改的：已经都在「%s」里了。" % category
        return result
    if not apply:
        result["next"] = "还没改。要改 %d 笔到「%s」%s。把这些给用户看；用户同意后，加 --apply 原样再运行。" % (
            len(todo), category, "，其中 %d 笔有退款，退款会先撤掉再照原样放回" % len(with_refunds) if with_refunds else "")
        return result
    if not progress.data:   # totals from before the first change, kept across an interrupted run
        progress.data = {"before": month_totals(ledger, months), "ids": [entry["id"] for entry in todo]}
        progress.save()
    for entry in todo:
        change(ledger, entry, category, refunds)
    after, before = month_totals(ledger, progress.data["before"]), progress.data["before"]
    progress.path.unlink(missing_ok=True)
    if after != before:
        result.update(ok=False, before=before, after=after,
                      next="分类改完了，但月份合计和改之前不一样。可能是改的时候正好有新账记进来。把 before 和 after 如实告诉用户。")
    else:
        result["next"] = "已改好 %d 笔，现在都在「%s」。%s 的支出、收入、退款合计和改之前一样。然后刷新看板。" % (
            len(todo), category, "、".join(sorted(months)))
    return result


def execute(args):
    ledger = Ledger(args.ledger)
    if args.to not in ledger.categories():
        raise LedgerError("分类「%s」不在账本里。先问用户；目录里有的话用 categories.py enable 启用。" % args.to)
    if args.command == "category":
        entry = must(ledger, "get", args.id)["entry"]
        if entry.get("type") not in ("expense", "income"):
            raise LedgerError("只能改支出或收入的分类。退款跟着原支出走：改原支出那一笔。转账没有分类。")
        plan = Journal("fix:category:" + entry["id"]).data.get("refunds")
        refunds = listing(ledger, "--type", "refund") if entry.get("refunded_cents") else []
        months = {entry["date"][:7]} | {refund["date"][:7] for refund in refunds if refund.get("original_id") == entry["id"]}
        months |= {item["payload"]["date"][:7] for item in plan or []}
        return run(ledger, "fix:one:" + entry["id"], [entry], args.to, months, args.apply, refunds)
    if args.old == args.to:
        raise LedgerError("两个分类名一样。")
    found = listing(ledger, "--category", args.old)
    entries = [entry for entry in found if entry.get("type") in ("expense", "income")]
    refunds = listing(ledger, "--type", "refund") if any(entry.get("refunded_cents") for entry in entries) else []
    ids = {entry["id"] for entry in entries}
    months = {entry["date"][:7] for entry in found} | {r["date"][:7] for r in refunds if r.get("original_id") in ids}
    return run(ledger, "fix:move:%s:%s" % (args.old, args.to), entries, args.to, months, args.apply, refunds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="改账小工具：改一笔的分类、把整个分类搬到另一个分类。不加 --apply 只预览。")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ai")
    parser.add_argument("--apply", action="store_true", help="真的改；不加只显示会改什么")
    anywhere = argparse.ArgumentParser(add_help=False)   # the same two options may also come after the command
    anywhere.add_argument("--ledger", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    anywhere.add_argument("--apply", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("category", parents=[anywhere], help="改一笔支出或收入的分类；有退款的也能改")
    one.add_argument("id", help="账单 ID")
    one.add_argument("to", help="新分类；必须是账本里已有的")
    move = commands.add_parser("move-category", parents=[anywhere], help="把一个分类下的所有账单搬到另一个分类（合并或改名）")
    move.add_argument("old", help="现在的分类")
    move.add_argument("to", help="搬去的分类；必须是账本里已有的")
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if result.get("applied") and result.get("count"):    # something changed: rebuild the dashboard's data
            dashboard_data.after_change(result, Ledger(args.ledger))
    except LedgerError as error:
        result = {"ok": False, "next": "没改完：%s" % error}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
