#!/usr/bin/env python3
"""The category catalog (categories.json in this repo) and which of it a ledger uses.

The catalog lists every category name Acost knows, with its Chinese name, colour and
when to use it. A ledger only holds the part it has enabled; recording refuses names
the ledger does not hold.

  categories.py list                 every catalog name: enabled or not; plus ledger names outside the catalog
  categories.py enable Bills-Internet Car-Gas    add catalog names to the ledger (needs a key that may record)
  categories.py enable --starter     the starter set, for a new ledger
  categories.py candidates           ledger names outside the catalog, with the catalog name they probably mean

  --ledger '~/acost/bin/ledger-ai'   (or ACOST_LEDGER_CMD; default: this repo's ledgerctl.py)
  --catalog PATH                     (or ACOST_CATEGORIES; default: categories.json next to tools/)
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from recurring import Ledger, LedgerError  # noqa: E402

COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def split(name):
    return name.split("-", 1) if "-" in name else [name, None]


def load(path=None):
    path = Path(path or os.environ.get("ACOST_CATEGORIES") or ROOT / "categories.json").expanduser()
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LedgerError("读不了分类目录 %s：%s" % (path, error)) from None
    groups, items = catalog.get("groups"), catalog.get("categories")
    if not isinstance(groups, dict) or not isinstance(items, list):
        raise LedgerError("分类目录要有 groups 和 categories")
    for group, info in groups.items():
        if not isinstance(info, dict) or not info.get("zh") or not COLOR.match(str(info.get("color"))):
            raise LedgerError("父类 %s 要有 zh 和 #RRGGBB 颜色" % group)
    seen, subs = set(), {}
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name or name != name.strip() or len(name) > 100:
            raise LedgerError("分类名不对：%r" % (name,))
        if name in seen:
            raise LedgerError("分类重复：" + name)
        seen.add(name)
        group, sub = split(name)
        if group not in groups:
            raise LedgerError("分类 %s 的父类 %s 不在 groups 里" % (name, group))
        if not item.get("zh") or not item.get("use") or item.get("type", "expense") not in ("expense", "income"):
            raise LedgerError("分类 %s 要有 zh、use，type 只能是 expense 或 income" % name)
        if sub and subs.setdefault(sub, item["zh"]) != item["zh"]:
            raise LedgerError("子类 %s 在不同父类下中文名不一致" % sub)
    return catalog


def names(catalog):
    return [item["name"] for item in catalog["categories"]]


def color_of(catalog, name):
    return catalog["groups"][split(name)[0]]["color"]


def labels(catalog):
    """The dashboard settings the catalog provides; ~/acost/dashboard.json can override each key."""
    category = {group: info["zh"] for group, info in catalog["groups"].items()}
    subcategory = {}
    for item in catalog["categories"]:
        group, sub = split(item["name"])
        if sub:
            subcategory[sub] = item["zh"]
        else:
            category[group] = item["zh"]
    return {"categoryLabels": category, "subcategoryLabels": subcategory,
            "categoryColors": {group: info["color"] for group, info in catalog["groups"].items()}}


def guess(catalog, name):
    """The catalog names an outside ledger name probably means (same subcategory, or named in "use")."""
    sub = (split(name)[1] or name).lower()
    found = [item["name"] for item in catalog["categories"]
             if (split(item["name"])[1] or item["name"]).lower() == sub]
    word = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
    found += [item["name"] for item in catalog["categories"]
              if item["name"] not in found and word.search(item["use"].lower())]
    return found


def listing(catalog, enabled):
    return {"ok": True,
            "catalog": [{"name": item["name"], "zh": item["zh"], "use": item["use"],
                         "enabled": item["name"] in enabled} for item in catalog["categories"]],
            "outside_catalog": sorted(enabled - set(names(catalog)))}


def candidates(catalog, enabled):
    outside = sorted(enabled - set(names(catalog)))
    return {"ok": True, "outside_catalog": [{"name": name, "probably": guess(catalog, name)} for name in outside],
            "hint": "目录外的分类：核对后把账单改到目录里的分类，或者把它加进 categories.json。改账前先备份。"}


def enable(ledger, catalog, wanted, starter=False):
    known = {item["name"]: item for item in catalog["categories"]}
    wanted = list(dict.fromkeys(list(wanted) + [n for n, item in known.items() if starter and item.get("starter")]))
    if not wanted:
        raise LedgerError("说出要启用的分类名，或加 --starter")
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise LedgerError("不在分类目录里：%s。先问用户，再把它加进 categories.json" % "、".join(unknown))
    have = ledger.categories()
    added, already = [], [name for name in wanted if name in have]
    for name in wanted:
        if name in have:
            continue
        ok, result = ledger.run("add-category", name, "--color", color_of(catalog, name))
        if not ok:
            raise LedgerError("启用 %s 失败（之前已启用：%s）：%s" % (name, "、".join(added) or "无", result.get("error")))
        added.append(name)
    return {"ok": True, "enabled": added, "already_enabled": already}


def main(argv=None):
    parser = argparse.ArgumentParser(description="分类目录：列出、启用、找出目录外的分类。")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ai")
    parser.add_argument("--catalog", help="分类目录；默认仓库里的 categories.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="目录里每个分类是否已启用；账本里目录外的分类")
    turn_on = commands.add_parser("enable", help="把目录里的分类加进账本（先问用户）")
    turn_on.add_argument("names", nargs="*")
    turn_on.add_argument("--starter", action="store_true", help="新账本默认的一组")
    commands.add_parser("candidates", help="账本里不在目录中的分类，以及它们可能对应的目录分类")
    args = parser.parse_args(argv)
    try:
        catalog, ledger = load(args.catalog), Ledger(args.ledger)
        if args.command == "enable":
            result = enable(ledger, catalog, args.names, args.starter)
        else:
            enabled = ledger.categories()
            result = listing(catalog, enabled) if args.command == "list" else candidates(catalog, enabled)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except LedgerError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
