#!/usr/bin/env python3
"""Build the dashboard's data file from the ledger. Read-only.

  dashboard_data.py --ledger '~/acost/bin/ledger-ro' [2026-10 2026-11]

Writes {"YYYY-MM": <data for design/template.html>, ...} to ~/acost/data/template_data.json
(--out). Without months it covers the last 12 months, leaving out the empty months before
the first entry but always keeping last month and this month. How the page looks
(fixed categories, Chinese labels, colours) comes from ~/acost/dashboard.json (--config);
see design/dashboard.example.json. Chinese names and colours it leaves out come from the
category catalog, categories.json (--catalog). Spending limits come from ~/acost/budget.json
(--budget; see tools/budget.py). The old file is kept if anything fails.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import budget  # noqa: E402
import categories  # noqa: E402
import recurring  # noqa: E402
from recurring import Ledger, LedgerError, check_month, previous_month, toronto_now  # noqa: E402

SETTINGS = ("fixedCategories", "categoryLabels", "subcategoryLabels", "categoryColors", "recurringView")
RECENT_MONTHS = 12
LABELS = ("categoryLabels", "subcategoryLabels", "categoryColors")
ENTRY = ("id", "date", "type", "amount_cents", "items")
PAGE = 500   # the most the ledger returns in one listing


def load_config(path):
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LedgerError("读不了看板配置 %s：%s" % (path, error)) from None
    if not isinstance(config, dict) or not set(config) <= set(SETTINGS) | {"accountsNote"}:
        raise LedgerError("看板配置只能有：" + ", ".join(SETTINGS + ("accountsNote",)))
    return config


def entries_back_to(ledger, oldest):
    """Every entry from the newest back to the month `oldest`, read at a single ledger revision.

    The ledger lists newest first, so one unfiltered listing covers all the months in a page or two.
    Each ledger call costs a second or more where the key is fetched per call, so calls are what to save.
    """
    found, offset, revision = [], 0, None
    while offset is not None:
        page = ledger.need("list", "--limit", str(PAGE), "--offset", str(offset))
        if revision not in (None, page.get("revision")):
            raise LedgerError("读取时账本有变动")
        revision = page.get("revision")
        entries = page.get("entries") or []
        found.extend(entries)
        offset = page.get("next_offset")
        if entries and (entries[-1].get("date") or "")[:7] < oldest:
            break
    return found, revision


def totals(entries):
    """The page's three totals, worked out the way the ledger's own summary does (refunds reduce spending)."""
    def sum_of(kind):
        return sum(entry["amount_cents"] for entry in entries if entry.get("type") == kind and not entry.get("deleted_at"))
    return {"expense_cents": sum_of("expense") - sum_of("refund"), "income_cents": sum_of("income"),
            "refund_cents": sum_of("refund")}


def recent_months(month, count=RECENT_MONTHS):
    months = [month]
    while len(months) < count:
        months.append(previous_month(months[-1]))
    return months[::-1]


def read(ledger, months, trim):
    meta = ledger.need("meta")
    entries, revision = entries_back_to(ledger, min(months))
    if revision is not None and revision != meta.get("revision"):
        raise LedgerError("读取时账本有变动")
    listed = {month: [] for month in months}
    for entry in entries:
        month = (entry.get("date") or "")[:7]
        if month in listed:
            listed[month].append(entry)
    keep = list(months)
    while trim and len(keep) > 2 and not listed[keep[0]]:   # nothing was recorded that far back
        keep.pop(0)
    return meta, {month: (totals(listed[month]), listed[month]) for month in keep}


def build(ledger, months, config, recurring_items, now, trim=False, catalog_labels=None, limits=None):
    try:
        meta, packs = read(ledger, months, trim)
    except LedgerError as error:
        if "账本有变动" not in str(error):
            raise
        meta, packs = read(ledger, months, trim)  # someone was recording; one more try
    note = config.get("accountsNote") or (
        "数据来自 Acost 云端账本" if meta.get("opening_balances_initialized")
        else "期初余额未设置，余额只按记录计算 · 数据来自 Acost 云端账本")
    data = {}
    for month, (summary, entries) in packs.items():
        page = {"month": month, "today": now.strftime("%Y-%m-%d"),
                "refreshedAt": "%d 月 %d 日 %s 刷新" % (now.month, now.day, now.strftime("%H:%M")),
                "summary": summary,
                "fixedCategories": [], "recurringView": "bar"}
        page.update({key: config[key] for key in SETTINGS if key in config})
        for key in LABELS:
            page[key] = dict((catalog_labels or {}).get(key) or {}, **(config.get(key) or {}))
        page["recurring"] = recurring.export(recurring_items, month)
        if limits:
            page["budget"] = limits
        page["accounts"] = [{"name": account["name"], "balance_cents": account["balance_cents"]}
                            for account in meta.get("accounts") or []]
        page["accountsNote"] = note
        page["entries"] = [dict({key: entry.get(key) for key in ENTRY}, time=entry.get("time") or "",
                                merchant=entry.get("merchant") or "", category=entry.get("category") or "",
                                account=entry.get("account") or "", note=entry.get("note") or "",
                                receipt=bool(entry.get("receipt_url")),
                                **({"to_account": entry["to_account"]} if entry.get("to_account") else {}))
                           for entry in entries if not entry.get("deleted_at")]
        data[month] = page
    return data


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def catalog_labels(path=None):
    path = Path(path or os.environ.get("ACOST_CATEGORIES") or categories.ROOT / "categories.json").expanduser()
    return categories.labels(categories.load(path)) if path.exists() else {}


def run(ledger, months=None, config=None, out=None, recurring_file=None, now=None, catalog=None, budget_file=None):
    now = now or toronto_now()
    month = now.strftime("%Y-%m")
    chosen = [check_month(value) for value in months] if months else None
    config_path = Path(config or os.environ.get("ACOST_DASHBOARD_CONFIG") or "~/acost/dashboard.json").expanduser()
    out_path = Path(out or os.environ.get("ACOST_DASHBOARD_DATA") or "~/acost/data/template_data.json").expanduser()
    data = build(ledger, chosen or recent_months(month), load_config(config_path),
                 recurring.load(recurring.list_path(recurring_file)), now, trim=not chosen,
                 catalog_labels=catalog_labels(catalog), limits=budget.load(budget.budget_path(budget_file)))
    write(out_path, data)
    return {"ok": True, "output": str(out_path), "months": {key: len(page["entries"]) for key, page in data.items()},
            "config_found": config_path.exists()}


REFRESHED = "看板数据已经更新好，打开「Acost 看板」就是最新的。"


def refresh(ledger, out=None):
    """After a change to the ledger: rebuild the data file so the caller does not have to remember to.
    None when there is no dashboard yet (its data folder was never made); never raises."""
    out_path = Path(out or os.environ.get("ACOST_DASHBOARD_DATA") or "~/acost/data/template_data.json").expanduser()
    if not out_path.parent.is_dir():
        return None
    try:
        run(ledger, out=out_path)
        return {"ok": True, "output": str(out_path)}
    except (LedgerError, KeyError, OSError, ValueError) as error:
        return {"ok": False, "error": str(error)}


def after_change(result, ledger):
    """Rebuild the dashboard data after a saved change and say so in result["next"] instead of "然后刷新看板"."""
    if not (result.get("ok") and result.get("status", "saved") == "saved"):
        return result
    board = refresh(ledger)
    if board is None:
        return result
    result["dashboard"] = board
    said = REFRESHED if board["ok"] else ("看板数据没更新（%s），旧数据还在；运行 dashboard_data.py 再试一次。" % board["error"])
    result["next"] = result.get("next", "").replace("，然后刷新看板。", "。" + said).replace("然后刷新看板。", said)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="从账本生成看板数据文件（只读）。")
    parser.add_argument("months", nargs="*", help="YYYY-MM；默认最近 12 个月（第一笔账之前的空月份不要）")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ro")
    parser.add_argument("--config", help="看板配置；默认 ~/acost/dashboard.json")
    parser.add_argument("--out", help="输出；默认 ~/acost/data/template_data.json")
    parser.add_argument("--recurring-file", help="固定扣费清单；默认 ~/acost/recurring.json")
    parser.add_argument("--catalog", help="分类目录；默认仓库里的 categories.json")
    parser.add_argument("--budget", help="预算文件；默认 ~/acost/budget.json")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(Ledger(args.ledger), args.months, args.config, args.out, args.recurring_file,
                             catalog=args.catalog, budget_file=args.budget), ensure_ascii=False))
        return 0
    except (LedgerError, KeyError) as error:
        print(json.dumps({"ok": False, "error": "看板数据没有更新，旧数据保留：" + str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
