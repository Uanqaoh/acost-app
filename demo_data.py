#!/usr/bin/env python3
"""Create an isolated, synthetic ledger for the desktop visual review.

All dates and amounts below are invented. Nothing is imported from the iCost
reference image or a real ledger. By default a new temporary directory is used.
An explicit directory must have ``demo`` in its name and be empty; existing
ledgers are never modified, even when they are already marked as demos.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ledger import Ledger


NOTE = "虚构演示数据，仅供界面验收；不是 iCost 导入数据，也不代表真实交易。"
# key, day, time, type, cents, merchant, category
RECORDS = [
    ("opening-income", "2026-09-05", "09:00", "income", 285000, "示例项目结算", "其他"),
    ("prior-shopping", "2026-09-22", "15:20", "expense", 8640, "枫叶生活馆", "购物"),
    ("prior-grocery", "2026-09-28", "18:15", "expense", 4520, "北街杂货", "餐饮-Grocery"),
    ("rent", "2026-10-01", "08:00", "expense", 145500, "松林公寓", "住房"),
    ("internet", "2026-10-01", "09:10", "expense", 6900, "家庭网络", "住房"),
    ("salary", "2026-10-01", "09:30", "income", 130000, "示例项目结算", "其他"),
    ("coffee-02", "2026-10-02", "08:45", "expense", 560, "湖畔咖啡", "餐饮-Eating out"),
    ("grocery-02", "2026-10-02", "17:40", "expense", 4832, "北街杂货", "餐饮-Grocery"),
    ("transit-02", "2026-10-02", "18:20", "expense", 335, "城市公交", "交通"),
    ("shopping-03", "2026-10-03", "13:20", "expense", 2375, "纸间书店", "购物"),
    ("food-03", "2026-10-03", "18:35", "expense", 1920, "小院厨房", "餐饮-Eating out"),
    ("income-04", "2026-10-04", "12:00", "income", 32540, "配送收入 · 第一周", "其他"),
    ("grocery-04", "2026-10-04", "16:15", "expense", 6814, "周末市集", "餐饮-Grocery"),
    ("food-05", "2026-10-05", "12:10", "expense", 1495, "午间便当", "餐饮-Eating out"),
    ("transport-05", "2026-10-05", "19:05", "expense", 980, "共享单车", "交通"),
    ("saving-05", "2026-10-05", "20:00", "transfer", 30000, "储蓄转账", ""),
    ("utility-06", "2026-10-06", "09:00", "expense", 4300, "家庭水电", "住房"),
    ("grocery-06", "2026-10-06", "18:25", "expense", 3126, "北街杂货", "餐饮-Grocery"),
    ("coffee-07", "2026-10-07", "10:10", "expense", 725, "湖畔咖啡", "餐饮-Eating out"),
    ("shopping-07", "2026-10-07", "14:40", "expense", 5940, "枫叶生活馆", "购物"),
    ("income-07", "2026-10-07", "17:00", "income", 23120, "示例设计项目", "其他"),
    ("grocery-08", "2026-10-08", "17:35", "expense", 4295, "周末市集", "餐饮-Grocery"),
    ("museum-08", "2026-10-08", "19:00", "expense", 1800, "城市展馆", "其他"),
    ("income-09", "2026-10-09", "11:20", "income", 41280, "配送收入 · 第二周", "其他"),
    ("food-09", "2026-10-09", "18:40", "expense", 2860, "小院厨房", "餐饮-Eating out"),
    ("transit-09", "2026-10-09", "20:00", "expense", 335, "城市公交", "交通"),
    ("grocery-10", "2026-10-10", "10:15", "expense", 7540, "北街杂货", "餐饮-Grocery"),
    ("shopping-10", "2026-10-10", "15:25", "expense", 4860, "晴日服饰", "购物"),
    ("coffee-11", "2026-10-11", "09:30", "expense", 630, "湖畔咖啡", "餐饮-Eating out"),
    ("ride-11", "2026-10-11", "21:05", "expense", 1685, "城市出行", "交通"),
    ("income-12", "2026-10-12", "12:30", "income", 37700, "示例摄影项目", "其他"),
    ("food-12", "2026-10-12", "18:20", "expense", 2160, "小院厨房", "餐饮-Eating out"),
    ("saving-12", "2026-10-12", "20:00", "transfer", 25000, "储蓄转账", ""),
    ("pharmacy-13", "2026-10-13", "12:10", "expense", 1475, "社区药房", "其他"),
    ("grocery-13", "2026-10-13", "18:45", "expense", 2856, "周末市集", "餐饮-Grocery"),
    ("grocery-14", "2026-10-14", "11:40", "expense", 5647, "北街杂货", "餐饮-Grocery"),
    ("food-14", "2026-10-14", "13:00", "expense", 1840, "午间便当", "餐饮-Eating out"),
    ("shopping-14", "2026-10-14", "16:20", "expense", 3290, "枫叶生活馆", "购物"),
    ("food-15", "2026-10-15", "08:30", "expense", 860, "湖畔咖啡", "餐饮-Eating out"),
    ("transit-15", "2026-10-15", "09:10", "expense", 335, "城市公交", "交通"),
    ("income-15", "2026-10-15", "12:15", "income", 28560, "示例翻译项目", "其他"),
]


def create_demo(directory: Path | None = None) -> Path:
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix="qiao-ledger-visual-demo-"))
    else:
        directory = directory.expanduser()
        if directory.is_symlink() or "demo" not in directory.name.lower():
            raise ValueError("演示目录名称必须包含 demo，且不能是符号链接。")
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError("拒绝修改非空目录或现有账本；请指定新的 demo 目录。")
        directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    marker = directory / ".creating-synthetic-demo"
    with marker.open("x", encoding="utf-8") as handle:
        handle.write(NOTE)

    # demo=True seeds an older example copied from a reference receipt. Start
    # empty instead, and mark only this brand-new isolated database as a demo.
    ledger = Ledger(directory, demo=False)
    with ledger.connect() as con:
        con.execute("UPDATE settings SET value='true' WHERE key='demo'")
        con.execute("UPDATE settings SET value='演示账本' WHERE key='book'")

    created = {}
    for key, day, at, kind, cents, merchant, category in RECORDS:
        values = {
            "type": kind, "amount_cents": cents, "date": day, "time": at,
            "merchant": merchant, "category": category, "account": "Spending",
            "note": NOTE, "idempotency_key": "visual-demo-v1-" + key,
        }
        if kind == "transfer":
            values["to_account"] = "Savings"
        created[key] = ledger.create(values)

    for key, original, day, at, cents in [
        ("prior-refund", "prior-shopping", "2026-10-03", "14:10", 2400),
        ("partial-refund", "shopping-07", "2026-10-12", "13:45", 1650),
        ("grocery-refund", "grocery-14", "2026-10-15", "13:05", 735),
    ]:
        ledger.refund(created[original]["id"], {
            "amount_cents": cents, "date": day, "time": at,
            "note": NOTE + " 部分退款，关联原支出。",
            "idempotency_key": "visual-demo-v1-" + key,
        })

    (directory / "DEMO_ONLY.txt").write_text(
        NOTE + "\n44 笔固定虚构记录；2026 年 9 月 3 笔，10 月 41 笔。"
        "\n无真实小票。账户期初为零；演示账户余额仅由上述记录计算。\n",
        encoding="utf-8",
    )
    marker.unlink()
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="新的空演示目录；目录名必须包含 demo")
    args = parser.parse_args()
    try:
        directory = create_demo(args.data)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    print(json.dumps({"demo": True, "data_dir": str(directory), "month": "2026-10",
                      "records": 44, "notice": NOTE}, ensure_ascii=False))


if __name__ == "__main__":
    main()
