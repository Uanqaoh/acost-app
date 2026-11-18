#!/usr/bin/env python3
"""The whole morning routine in one command; prints a few lines to pass on.

  daily.py     (uses ~/acost/bin/ledger-ai and ~/acost/bin/ledger-ro; --ledger-ai / --ledger-ro to change)

1. checks the local files against MANIFEST.json
2. records fixed charges that are due        (needs the bookkeeping key)
3. runs the check-up                         (read-only)
4. rebuilds the dashboard data file          (read-only)
5. looks for Costco price adjustments        (cocoeast.ca; only when there are Costco purchases to watch)
Every step runs even if an earlier one fails. The output is one JSON object:
  report   lines to tell the user, already worded; say nothing more than these
  refresh  true when the dashboard data file was rebuilt and the page should reload it
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkup  # noqa: E402
import costco_watch  # noqa: E402
import dashboard_data  # noqa: E402
import manifest  # noqa: E402
import recurring  # noqa: E402
from recurring import Ledger, LedgerError, toronto_now  # noqa: E402


def money(cents):
    return "C$%.2f" % (cents / 100)


def run(ledger_ai, ledger_ro, now=None, recurring_file=None, state=None, config=None, out=None, root=None,
        budget_file=None, costco_file=None, fetch=costco_watch.fetch_url):
    now = now or toronto_now()
    today, report, ok = now.strftime("%Y-%m-%d"), [], True
    stale = manifest.stale(root) if root else manifest.stale()
    if stale:
        ok = False
        report.append("程序文件不是最新，需要重新拉取：" + "、".join(stale))
    try:
        results = recurring.post(ledger_ai, recurring.load(recurring.list_path(recurring_file), required=True), today)
        for done in results:
            if done["posted"]:
                report.append("自动记了固定扣费：%s %s（%s）" % (done["name"], money(done["amount_cents"]), done["date"]))
            else:
                ok = False
                report.append("固定扣费「%s」没记上：%s" % (done["name"], done.get("error")))
    except LedgerError as error:
        ok = False
        report.append("固定扣费没有运行：" + str(error))
    try:
        state_path = Path(state or os.environ.get("ACOST_CHECKUP_STATE") or "~/acost/checkup_state.json").expanduser()
        report += ["体检：" + problem for problem in checkup.run(ledger_ro, state_path, today, budget_file)["problems"]]
    except LedgerError as error:
        ok = False
        report.append("体检没有运行：" + str(error))
    refresh = False
    try:
        dashboard_data.run(ledger_ro, None, config, out, recurring_file, now, budget_file=budget_file)
        refresh = True
    except (LedgerError, KeyError) as error:
        ok = False
        report.append("看板数据没有更新，仍是旧数据：" + str(error))
    try:   # a sale is mentioned once; an unreadable sales list once, until it is readable again
        report += costco_watch.check(today, costco_file, fetch, only_new=True)["report"]
    except (LedgerError, OSError) as error:
        report.append("Costco 退差价没有检查：" + str(error))
    return {"ok": ok, "date": today, "report": report or ["今天没有要报告的。"], "refresh": refresh}


def main(argv=None):
    parser = argparse.ArgumentParser(description="每日例行：固定扣费、体检、看板数据，一条命令。")
    parser.add_argument("--ledger-ai", default=os.environ.get("ACOST_LEDGER_AI") or "~/acost/bin/ledger-ai")
    parser.add_argument("--ledger-ro", default=os.environ.get("ACOST_LEDGER_RO") or "~/acost/bin/ledger-ro")
    args = parser.parse_args(argv)
    result = run(Ledger(args.ledger_ai), Ledger(args.ledger_ro))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
