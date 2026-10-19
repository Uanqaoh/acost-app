#!/usr/bin/env python3
"""Read iCost XLSX exports locally and prepare a reviewed CAD import (stdlib only).

This never connects to a server. Source rows remain available in the private
report; selected rows also retain their original fields in the snapshot audit.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from client_config import ClientError
from cloud_admin import encoded, facts, private_write, write_bundle, MAX_JSON

HEADERS = ('日期 类型 金额 一级分类 二级分类 账户1 账户2 备注 货币 标签 账本 位置 '
           '退款 优惠 手续费 图片链接1 图片链接2 图片链接3 图片附件1 图片附件2 图片附件3').split()
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
KINDS = {'支出': 'expense', '收入': 'income', '转账': 'transfer', '退款入账': 'refund'}


def cents(value):
    try:
        number = Decimal(str(value)) * 100
        if not number.is_finite() or number != number.to_integral_value() or abs(number) > 999999999999:
            raise ValueError()
        return int(number)
    except (InvalidOperation, ValueError):
        raise ClientError('金额不是有效的两位小数：' + str(value)) from None


def read_export(path):
    data = Path(path).read_bytes()
    if len(data) > 64 * 1024 * 1024:
        raise ClientError('导出文件超过本转换器的 64 MiB 上限。')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) != len({i.filename for i in infos}) or sum(i.file_size for i in infos) > 128 * 1024 * 1024:
            raise ClientError('导出包重复文件或解压大小超限。')

        def xml(name):
            content = z.read(name)
            if b'<!DOCTYPE' in content or b'<!ENTITY' in content:
                raise ClientError('不支持 XML 外部实体。')
            return ET.fromstring(content)

        sheets = xml('xl/workbook.xml').findall('s:sheets/s:sheet', NS)
        if len(sheets) != 1 or sheets[0].get('name') != '收支账单':
            raise ClientError('仅支持已识别的单表「收支账单」导出。')
        rels = {x.get('Id'): x for x in xml('xl/_rels/workbook.xml.rels')}
        rel = rels[sheets[0].get(REL)]
        target = rel.get('Target', '')
        member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if rel.get('TargetMode') == 'External' or not member.startswith('xl/'):
            raise ClientError('工作表路径无效。')
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            shared = [''.join(n.itertext()) if not n.findall('.//s:t', NS) else
                      ''.join(t.text or '' for t in n.findall('.//s:t', NS))
                      for n in xml('xl/sharedStrings.xml')]
        grid = []
        seen_rows = set()
        for row in xml(member).findall('s:sheetData/s:row', NS):
            row_num = int(row.get('r'))
            if row_num in seen_rows or not 1 <= row_num <= 100001:
                raise ClientError('工作表行号无效或重复。')
            seen_rows.add(row_num)
            values = [''] * len(HEADERS)
            seen_cells = set()
            for cell in row.findall('s:c', NS):
                address = cell.get('r', '')
                match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', address)
                if not match or int(match[2]) != row_num or address in seen_cells:
                    raise ClientError('单元格地址无效或重复。')
                seen_cells.add(address)
                col = 0
                for letter in match[1]:
                    col = col * 26 + ord(letter) - 64
                if col > len(HEADERS) or cell.find('s:f', NS) is not None:
                    raise ClientError('存在未知列或公式；需要人工检查。')
                kind = cell.get('t', 'n')
                value = cell.findtext('s:v', '', NS)
                if kind == 's':
                    value = shared[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                elif kind not in ('n', 'str'):
                    raise ClientError('不支持的单元格类型：' + kind)
                values[col - 1] = value
            if any(values):
                grid.append((row_num, values))
        if not grid or grid[0] != (1, HEADERS):
            raise ClientError('导出表头不匹配；不能猜测字段。')
        rows = [dict(zip(HEADERS, values), source_row=n) for n, values in grid[1:]]
        media = [i.filename for i in infos if i.filename.startswith('xl/media/') and not i.is_dir()]
        hyperlinks = any(i.filename.endswith('.rels') and b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"' in z.read(i) for i in infos if not i.is_dir())
    return {'filename': Path(path).name, 'sha256': hashlib.sha256(data).hexdigest(),
            'sheet': '收支账单', 'rows': rows, 'embedded_media': media, 'hyperlinks': hyperlinks}


def inspect_export(source):
    groups = defaultdict(lambda: {'count': 0, 'signed_cents': 0})
    months = defaultdict(lambda: {'expense_cents': 0, 'income_cents': 0, 'refund_cents': 0, 'transfer_cents': 0, 'count': 0})
    zero_rows, image_rows = [], []
    for row in source['rows']:
        try:
            timestamp = dt.datetime.strptime(row['日期'], '%Y/%m/%d %H:%M:%S')
            if timestamp.strftime('%Y/%m/%d %H:%M:%S') != row['日期']:
                raise ValueError('noncanonical date')
            amount = cents(row['金额'])
            kind = KINDS[row['类型']]
        except (ValueError, KeyError) as exc:
            raise ClientError('无法识别第 %s 行的日期或类型。' % row['source_row']) from exc
        if (kind == 'expense' and amount > 0) or (kind != 'expense' and amount < 0):
            raise ClientError('第 %s 行金额符号与类型不符。' % row['source_row'])
        if not row['货币'] or not row['账户1'] or not row['账本'] or (kind != 'transfer' and not row['一级分类']):
            raise ClientError('第 %s 行缺少必要字段。' % row['source_row'])
        if (kind == 'transfer') != bool(row['账户2']) or (kind == 'transfer' and row['账户1'] == row['账户2']):
            raise ClientError('第 %s 行转账账户不符。' % row['source_row'])
        for field in ('退款', '优惠', '手续费'):
            if row[field] and cents(row[field]) < 0:
                raise ClientError('第 %s 行%s字段为负。' % (row['source_row'], field))
        if row['退款'] and cents(row['退款']) and (kind != 'expense' or cents(row['退款']) > abs(amount)):
            raise ClientError('原单退款金额无效。')
        group = groups[(row['货币'], row['类型'])]
        group['count'] += 1
        group['signed_cents'] += amount
        month = months[(row['货币'], timestamp.strftime('%Y-%m'))]
        month[kind + '_cents'] += abs(amount)
        month['count'] += 1
        if not amount:
            zero_rows.append(row['source_row'])
        if any(row[h] for h in HEADERS if h.startswith('图片')):
            image_rows.append(row['source_row'])
    return {'source_sha256': source['sha256'], 'source_filename': source['filename'],
            'source_sheet': source['sheet'], 'source_row_count': len(source['rows']),
            'groups': [dict(currency=c, type=t, **v) for (c, t), v in sorted(groups.items())],
            'months': [dict(currency=c, month=m, net_expense_cents=v['expense_cents']-v['refund_cents'],
                            net_change_cents=v['income_cents']-v['expense_cents']+v['refund_cents'], **v)
                       for (c, m), v in sorted(months.items())],
            'zero_rows': zero_rows, 'image_rows': image_rows, 'embedded_media': source['embedded_media'],
            'rows': source['rows']}


def build_snapshot(source, *, exclude_currencies=(), archive_zero=False, timestamp=None):
    report = inspect_export(source)
    present = {r['货币'] for r in source['rows']}
    if set(exclude_currencies) != present - {'CAD'} or 'CAD' in exclude_currencies:
        raise ClientError('必须明确列出所有不导入的非 CAD 币种；不会自动换汇或舍弃。')
    selected = [r for r in source['rows'] if r['货币'] == 'CAD']
    if not selected:
        raise ClientError('没有 CAD 记录。')
    if len({r['账本'] for r in selected}) != 1:
        raise ClientError('不能自动合并多个账本。')
    if source['embedded_media'] or source['hyperlinks'] or any(any(r[h] for h in HEADERS if h.startswith('图片')) for r in selected):
        raise ClientError('检测到图片或链接；本转换器暂不支持附件，必须先制定保全方案。')
    if any(cents(r[h]) for r in selected for h in ('优惠', '手续费') if r[h]):
        raise ClientError('存在优惠或手续费，需先核实其是否已计入金额。')
    zeros = [r for r in selected if cents(r['金额']) == 0]
    if zeros and not archive_zero:
        raise ClientError('存在零金额记录；需明确使用 --archive-zero 保留原行而不生成账单。')
    financial = [r for r in selected if cents(r['金额']) != 0]
    if not financial:
        raise ClientError('只有零金额记录，不能生成空财务快照；请保留本地核对报告。')
    stamp = timestamp or dt.datetime.now(dt.timezone.utc).isoformat()
    def identifier(row):
        return hashlib.sha256(('icost-xlsx-v1:' + source['sha256'] + ':' + str(row['source_row'])).encode()).hexdigest()[:32]
    originals = [r for r in financial if r['类型'] == '支出' and r['退款'] and cents(r['退款']) > 0]
    refunds = [r for r in financial if r['类型'] == '退款入账']
    links = {}
    # A unique one-to-one match needs the export's explicit original refund total,
    # matching currency/account/book/both categories, and chronological order.
    # Split or ambiguous refunds are rejected rather than guessed.
    for refund in refunds:
        candidates = [r for r in originals if cents(r['退款']) == cents(refund['金额'])
                      and r['日期'] <= refund['日期']
                      and all(r[k] == refund[k] for k in ('货币', '账户1', '账本', '一级分类', '二级分类'))]
        if len(candidates) != 1 or candidates[0]['source_row'] in links.values():
            raise ClientError('退款第 %s 行不能唯一关联原单。' % refund['source_row'])
        links[refund['source_row']] = candidates[0]['source_row']
    if set(links.values()) != {r['source_row'] for r in originals}:
        raise ClientError('原单的退款合计与退款入账行不完整。')
    by_row = {r['source_row']: r for r in selected}
    account_names = sorted({r[k] for r in financial for k in ('账户1', '账户2') if r[k]})
    entries, audit = [], []
    for row in selected:
        amount = cents(row['金额'])
        entity = identifier(row)
        if amount:
            category = '' if row['类型'] == '转账' else row['一级分类'] + ('-' + row['二级分类'] if row['二级分类'] else '')
            note = row['备注']
            if row['标签']:
                note += ('\n' if note else '') + '标签：' + row['标签']
            if row['位置']:
                note += ('\n' if note else '') + '地点：' + row['位置']
            if len(category) > 100 or len(note) > 10000 or any(len(n) > 100 for n in account_names):
                raise ClientError('字段超出账本长度限制，拒绝截断。')
            original = by_row.get(links.get(row['source_row']))
            entries.append({'id': entity, 'type': KINDS[row['类型']], 'amount_cents': abs(amount),
                            'date': row['日期'][:10].replace('/', '-'), 'time': row['日期'][11:16],
                            'merchant': '', 'note': note, 'category': category, 'account': row['账户1'],
                            'to_account': row['账户2'] or None, 'original_id': identifier(original) if original else None,
                            'receipt_url': None, 'idempotency_key': 'icost-xlsx:' + source['sha256'] + ':' + str(row['source_row']),
                            'request_hash': None, 'deleted_at': None, 'created_at': stamp, 'updated_at': stamp,
                            'version': 1, 'created_by': 'icost-xlsx-import', 'updated_by': 'icost-xlsx-import'})
        audit.append({'sequence': len(audit) + 1, 'at': stamp, 'actor': 'icost-xlsx-import',
                      'action': 'icost_source' if amount else 'icost_zero_archived', 'entity_id': entity,
                      'before_json': None, 'after_json': {'source_sha256': source['sha256'],
                      'sheet': source['sheet'], 'row': row, 'original_source_row': links.get(row['source_row']),
                      'refund_link_method': 'unique_explicit_refund_total' if row['source_row'] in links else None}})
    categories = sorted({e['category'] for e in entries if e['category']})
    # Joining hierarchical labels must not collapse distinct source categories.
    pairs = {(r['一级分类'], r['二级分类']) for r in financial if r['类型'] != '转账'}
    if len(pairs) != len(categories):
        raise ClientError('一级和二级分类合并后重名，需明确映射。')
    settings = {'schema_version': '2', 'book': selected[0]['账本'], 'currency': 'CAD', 'demo': 'false',
                'opening_balances_initialized': 'false', 'timezone': 'America/Toronto'}
    snapshot = {'format': 'qiao-ledger-snapshot', 'version': 1, 'schema_version': 2, 'exported_at': stamp,
                'settings': [{'key': k, 'value': v} for k, v in settings.items()],
                'accounts': [{'name': n, 'opening_balance_cents': 0, 'sort_order': i} for i, n in enumerate(account_names, 1)],
                'categories': [{'name': n, 'color': '#aa9d89', 'sort_order': i} for i, n in enumerate(categories, 1)],
                'entries': entries, 'receipts': [], 'audit_events': audit}
    if len(encoded(snapshot)) > MAX_JSON:
        raise ClientError('生成快照超过 10 MiB 导入限制。')
    validation = facts(snapshot)
    # Independently reconcile source signed flows (refunds count once) by account.
    flows = dict.fromkeys(account_names, 0)
    for r in financial:
        value = cents(r['金额'])
        flows[r['账户1']] += -value if r['类型'] == '转账' else value
        if r['类型'] == '转账':
            flows[r['账户2']] += value
    if flows != validation['account_balances_cents']:
        raise ClientError('转换前后账户流水不一致。')
    report.update({'selected_currency': 'CAD', 'excluded_currencies': sorted(exclude_currencies),
                   'selected_row_count': len(selected), 'import_entry_count': len(entries),
                   'archived_zero_rows': [r['source_row'] for r in zeros],
                   'refund_links': [{'refund_row': k, 'original_row': v, 'method': 'unique_explicit_refund_total'} for k, v in links.items()],
                   'account_net_flows_cents': flows, 'opening_balances_initialized': False,
                   'receipts_in_this_export': 0, 'status': 'prepared_locally_not_imported'})
    return snapshot, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, help='明确选择 CAD 后生成可核对的本地快照')
    parser.add_argument('--currency', choices=['CAD'])
    parser.add_argument('--exclude-currency', action='append', default=[])
    parser.add_argument('--archive-zero', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.report.exists() or (args.bundle and args.bundle.exists()):
            raise ClientError('输出已存在，拒绝覆盖。')
        if args.bundle and args.currency != 'CAD':
            raise ClientError('生成快照需要明确 --currency CAD。')
        source = read_export(args.source)
        if args.bundle:
            snapshot, report = build_snapshot(source, exclude_currencies=args.exclude_currency, archive_zero=args.archive_zero)
            write_bundle(args.bundle, snapshot, lambda name: b'')
        else:
            report = inspect_export(source)
        private_write(args.report, encoded(report))
        print(json.dumps({k: v for k, v in report.items() if k not in ('rows',)}, ensure_ascii=False))
        return 0
    except (ClientError, OSError, ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
