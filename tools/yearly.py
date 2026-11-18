#!/usr/bin/env python3
"""A whole year at a glance, and the same year as two spreadsheet files. Read-only.

  yearly.py --ledger ~/acost/bin/ledger-ro [--year 2026] [--out ~/acost/exports]

It prints one JSON object: `report` holds a few lines to tell the user, already worded, and `files`
the two CSV files it wrote (they open in Excel, Numbers and Google Sheets):
  acost-2026-汇总.csv   one row per category, one column per month, refunds already taken off
  acost-2026-明细.csv   every entry of the year
The files hold real amounts: give them to the user only, and do not post them anywhere else.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_data  # noqa: E402
from recurring import Ledger, LedgerError, toronto_now  # noqa: E402

KINDS = {"expense": "支出", "income": "收入", "refund": "退款", "transfer": "转账"}


def money(cents):
    return "%sC$%.2f" % ("-" if cents < 0 else "", abs(cents) / 100)


def dollars(cents):
    return "%.2f" % (cents / 100)


def summarise(entries, year):
    """Per month and per category: spending is expenses minus refunds; transfers count as neither."""
    months = ["%d-%02d" % (year, number) for number in range(1, 13)]
    blank = lambda: {month: 0 for month in months}   # noqa: E731
    spending, income, totals = {}, {}, {month: {"expense_cents": 0, "income_cents": 0} for month in months}
    for entry in entries:
        month, kind, cents = entry["date"][:7], entry.get("type"), entry["amount_cents"]
        category = entry.get("category") or "其他"
        if kind in ("expense", "refund"):
            signed = cents if kind == "expense" else -cents
            spending.setdefault(category, blank())[month] += signed
            totals[month]["expense_cents"] += signed
        elif kind == "income":
            income.setdefault(category, blank())[month] += cents
            totals[month]["income_cents"] += cents
    used = [month for month in months if any(entry["date"][:7] == month for entry in entries)]
    return {"months": months, "used": used, "spending": spending, "income": income, "totals": totals}


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as output:   # the BOM lets Excel read the Chinese
        csv.writer(output).writerows(rows)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def summary_rows(summary, labels):
    months = summary["months"]
    head = ["类型", "分类", "中文名"] + ["%d 月" % int(month[5:]) for month in months] + ["全年"]
    rows = [head]
    for kind, table in (("支出", summary["spending"]), ("收入", summary["income"])):
        ordered = sorted(table, key=lambda name: -sum(table[name].values()))
        rows += [[kind, name, labels(name)] + [dollars(table[name][month]) for month in months]
                 + [dollars(sum(table[name].values()))] for name in ordered]
        key = "expense_cents" if kind == "支出" else "income_cents"
        rows.append([kind, "合计", ""] + [dollars(summary["totals"][month][key]) for month in months]
                    + [dollars(sum(summary["totals"][month][key] for month in months))])
    return rows


def detail_rows(entries, labels):
    rows = [["日期", "时间", "类型", "金额", "商家", "分类", "中文名", "账户", "转入账户", "备注", "ID"]]
    for entry in sorted(entries, key=lambda e: (e["date"], e.get("time") or "", e.get("id") or "")):
        rows.append([entry["date"], entry.get("time") or "", KINDS.get(entry.get("type"), entry.get("type")),
                     dollars(entry["amount_cents"]), entry.get("merchant") or "", entry.get("category") or "",
                     labels(entry.get("category") or ""), entry.get("account") or "", entry.get("to_account") or "",
                     entry.get("note") or "", entry.get("id") or ""])
    return rows


def labeller(catalog=None):
    found = dashboard_data.catalog_labels(catalog)
    parents, children = found.get("categoryLabels") or {}, found.get("subcategoryLabels") or {}

    def label(name):
        if not name or name in parents:
            return parents.get(name, "")
        parent, _, child = name.partition("-")
        text = parents.get(parent, parent) + (" › " + children.get(child, child) if child else "")
        return "" if text == name else text
    return label


def run(ledger, year, out, catalog=None):
    found, _ = dashboard_data.entries_back_to(ledger, "%d-01" % year)
    entries = [entry for entry in found if entry.get("date", "")[:4] == str(year) and not entry.get("deleted_at")]
    if not entries:
        return {"ok": True, "year": year, "files": [], "report": ["%d 年没有账。" % year]}
    summary, label = summarise(entries, year), labeller(catalog)
    files = [out / ("acost-%d-汇总.csv" % year), out / ("acost-%d-明细.csv" % year)]
    write_csv(files[0], summary_rows(summary, label))
    write_csv(files[1], detail_rows(entries, label))
    used = summary["used"]
    spent = sum(summary["totals"][month]["expense_cents"] for month in used)
    earned = sum(summary["totals"][month]["income_cents"] for month in used)
    span = "全年" if len(used) == 12 else "%d–%d 月有账" % (int(used[0][5:]), int(used[-1][5:]))
    report = ["%d 年（%s）：支出 %s，收入 %s，结余 %s，平均每月支出 %s。" % (
        year, span, money(spent), money(earned), money(earned - spent), money(round(spent / len(used))))]
    parents = {}
    for name, table in summary["spending"].items():
        parents[name.partition("-")[0]] = parents.get(name.partition("-")[0], 0) + sum(table.values())
    for name in sorted(parents, key=lambda key: -parents[key])[:5]:
        if parents[name] > 0 and spent > 0:
            report.append("%s：%s，占 %.0f%%" % (label(name) or name, money(parents[name]), parents[name] / spent * 100))
    most = max(used, key=lambda month: summary["totals"][month]["expense_cents"])
    report.append("花得最多的是 %d 月：%s。" % (int(most[5:]), money(summary["totals"][most]["expense_cents"])))
    return {"ok": True, "year": year, "entries": len(entries), "files": [str(path) for path in files], "report": report,
            "next": "把 report 的每一行告诉用户，把两个文件发给用户。文件里是真实金额，不要发到别处。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="年度汇总和导出：全年按分类、按月的合计，外加两个表格文件。只读。")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ro")
    parser.add_argument("--year", type=int, help="哪一年；默认今年")
    parser.add_argument("--out", help="文件放在哪个文件夹；默认 ~/acost/exports")
    parser.add_argument("--catalog", help="分类目录；默认仓库里的 categories.json")
    args = parser.parse_args(argv)
    year = args.year or toronto_now().year
    out = Path(args.out or os.environ.get("ACOST_EXPORT_DIR") or "~/acost/exports").expanduser()
    try:
        if not 2000 <= year <= 2100:
            raise LedgerError("年份不对。")
        print(json.dumps(run(Ledger(args.ledger), year, out, args.catalog), ensure_ascii=False))
        return 0
    except (LedgerError, OSError) as error:
        print(json.dumps({"ok": False, "error": "年度汇总没有生成：%s" % error}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
