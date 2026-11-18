#!/usr/bin/env python3
"""Personal deployment administration and portable, verified backups (stdlib only)."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid
import zipfile
from client_config import ClientError, Connection, default_config, load_connection, save_connection, validate_url

RECEIPT = re.compile(r'[a-f0-9]{32}\.(?:png|jpg|webp)')
ENTRY_TIME = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')
MAX_BUNDLE = 512 * 1024 * 1024
MAX_JSON = 10 * 1024 * 1024

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def request(connection, method, path, payload=None):
    raw, _ = connection.request(method, path, payload=payload, max_bytes=MAX_JSON)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ClientError('服务返回无效 JSON。') from None
    if not isinstance(value, dict):
        raise ClientError('服务返回无效对象。')
    return value

def private_write(path, content):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())

def require_admin(connection):
    meta = request(connection, 'GET', '/api/meta')
    if meta.get('client', {}).get('role') != 'admin':
        raise ClientError('需要此项目专属的 admin 凭据。')
    return meta

def validate_snapshot(snapshot):
    if snapshot.get('format') != 'qiao-ledger-snapshot' or snapshot.get('version') != 1:
        raise ClientError('不支持的账本快照格式。')
    schema_version = snapshot.get('schema_version')
    if type(schema_version) is not int or schema_version not in (2, 3):
        raise ClientError('不支持的账本快照结构版本。')
    for key in ('settings', 'accounts', 'categories', 'entries', 'receipts', 'audit_events'):
        if not isinstance(snapshot.get(key), list) or any(not isinstance(x, dict) for x in snapshot[key]):
            raise ClientError('快照结构无效：' + key)
    if 'credentials' in snapshot:
        raise ClientError('账本快照不能包含客户端凭据。')
    schema_settings = [row.get('value') for row in snapshot['settings'] if row.get('key') == 'schema_version']
    if schema_settings != [str(schema_version)]:
        raise ClientError('快照结构版本与账本设置不一致。')
    entries = snapshot['entries']
    by_id = {e.get('id'): e for e in entries}
    if len(by_id) != len(entries) or None in by_id:
        raise ClientError('快照账单 ID 缺失或重复。')
    accounts = {a['name']: a['opening_balance_cents'] for a in snapshot['accounts']}
    if len(accounts) != len(snapshot['accounts']) or any(type(v) is not int for v in accounts.values()):
        raise ClientError('账户或期初金额无效。')
    receipts = {r.get('filename'): r for r in snapshot['receipts']}
    if len(receipts) != len(snapshot['receipts']):
        raise ClientError('小票文件名重复。')
    for name, r in receipts.items():
        if not isinstance(name, str) or not RECEIPT.fullmatch(name) or not re.fullmatch(r'[a-f0-9]{64}', str(r.get('sha256', ''))) or type(r.get('size')) is not int or not 0 < r['size'] <= 8*1024*1024:
            raise ClientError('小票清单无效。')
    keys = set()
    refunded = {}
    for e in entries:
        if not re.fullmatch(r'[a-f0-9]{32}', str(e['id'])) or e.get('type') not in ('expense','income','refund','transfer'):
            raise ClientError('账单标识或类型无效。')
        if type(e.get('amount_cents')) is not int or not 0 < e['amount_cents'] <= 999999999999:
            raise ClientError('账单金额必须是正整数分。')
        entry_time = e.get('time')
        if 'time' not in e or not (
            schema_version == 3 and entry_time is None
            or isinstance(entry_time, str) and ENTRY_TIME.fullmatch(entry_time)
        ):
            raise ClientError('账单时间必须是 HH:MM；结构版本 3 也允许明确的 null。')
        if e.get('account') not in accounts:
            raise ClientError('账单引用不存在的账户。')
        key = e.get('idempotency_key')
        if key:
            if key in keys:
                raise ClientError('幂等键重复。')
            keys.add(key)
        url = e.get('receipt_url')
        if url and (not url.startswith('/api/receipts/') or url[14:] not in receipts):
            raise ClientError('账单引用的小票缺失。')
        if e['type'] == 'transfer' and (e.get('to_account') not in accounts or e['to_account'] == e['account']):
            raise ClientError('转账账户无效。')
        if e['type'] == 'refund':
            original = by_id.get(e.get('original_id'))
            if not original or original['id'] == e['id']:
                raise ClientError('退款原单关联无效。')
            if not e.get('deleted_at'):
                if original.get('deleted_at') or original['type'] != 'expense' or any(e.get(k) != original.get(k) for k in ('account','category')):
                    raise ClientError('有效退款的原单状态、账户或分类不一致。')
                refunded[original['id']] = refunded.get(original['id'], 0) + e['amount_cents']
    if any(value > by_id[key]['amount_cents'] for key,value in refunded.items()):
        raise ClientError('退款超过原支出金额。')
    return snapshot

def facts(snapshot):
    validate_snapshot(snapshot)
    balances = {a['name']: a['opening_balance_cents'] for a in snapshot['accounts']}
    categories = {}
    for e in snapshot['entries']:
        if e.get('deleted_at'):
            continue
        amount, kind = e['amount_cents'], e['type']
        balances[e['account']] += amount if kind in ('income','refund') else -amount
        if kind == 'transfer':
            balances[e['to_account']] += amount
        if kind in ('expense','refund'):
            categories[e['category']] = categories.get(e['category'],0) + (amount if kind == 'expense' else -amount)
    return {'entry_ids': sorted(e['id'] for e in snapshot['entries']),
            'entry_count': len(snapshot['entries']),
            'active_entry_count': sum(not e.get('deleted_at') for e in snapshot['entries']),
            'refund_links': {e['id']:e['original_id'] for e in snapshot['entries'] if e['type']=='refund'},
            'account_balances_cents': balances, 'category_totals_cents': categories,
            'receipt_hashes': {r['filename']:r['sha256'] for r in snapshot['receipts']}}

def write_bundle(output, snapshot, get_receipt):
    validation = facts(snapshot)
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reserve destination exclusively before network work; incomplete files are removed.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    total = 0
    try:
        with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            files = {}
            items = [('snapshot.json', encoded(snapshot))]
            for name, data in items:
                archive.writestr(name, data)
                files[name] = {'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                total += len(data)
            for receipt in snapshot['receipts']:
                content = get_receipt(receipt['filename'])
                if len(content) != receipt['size'] or hashlib.sha256(content).hexdigest() != receipt['sha256']:
                    raise ClientError('小票与快照校验值不一致，备份未保存。')
                total += len(content)
                if total > MAX_BUNDLE:
                    raise ClientError('备份超过 512 MiB，请使用分卷管理工具。')
                name = 'receipts/' + receipt['filename']
                archive.writestr(name, content)
                files[name] = {'size':len(content),'sha256':receipt['sha256']}
            manifest = {'format':'qiao-cloud-backup','version':1,'files':files,'validation':validation}
            archive.writestr('manifest.json', encoded(manifest))
        read_bundle(output)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return validation

def read_bundle(path):
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()
        if len(info) != len({i.filename for i in info}) or sum(i.file_size for i in info) > MAX_BUNDLE:
            raise ClientError('备份重复文件或解压大小超限。')
        if any(i.is_dir() or (i.filename not in ('manifest.json','snapshot.json') and not (i.filename.startswith('receipts/') and RECEIPT.fullmatch(i.filename[9:]))) for i in info):
            raise ClientError('备份含无效路径。')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('format') != 'qiao-cloud-backup' or manifest.get('version') != 1:
            raise ClientError('备份格式不支持。')
        if set(manifest['files']) != {i.filename for i in info} - {'manifest.json'}:
            raise ClientError('备份文件清单不完整。')
        contents = {}
        for name, expected in manifest['files'].items():
            data = archive.read(name)
            if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ClientError('备份校验失败：' + name)
            contents[name] = data
        snapshot = validate_snapshot(json.loads(contents['snapshot.json']))
        expected_names = {'snapshot.json'} | {'receipts/'+r['filename'] for r in snapshot['receipts']}
        if set(contents) != expected_names or facts(snapshot) != manifest['validation']:
            raise ClientError('备份财务或小票清单核对失败。')
        for r in snapshot['receipts']:
            data = contents['receipts/'+r['filename']]
            if len(data) != r['size'] or hashlib.sha256(data).hexdigest() != r['sha256']:
                raise ClientError('小票内容与账本清单不一致。')
        return snapshot, contents, manifest['validation']

def verify_restored(connection, snapshot, validation):
    validate_snapshot(snapshot)
    actual=request(connection,'GET','/api/export')
    if facts(actual)!=validation:
        raise ClientError('恢复后的财务核对不一致，暂停后续写入并检查。')
    # Restoring an old snapshot upgrades only the deployed schema marker. The
    # original archive and every financial, time, receipt and audit value stay exact.
    upgraded = snapshot['schema_version'] == 2 and actual['schema_version'] == 3
    if snapshot['schema_version'] != actual['schema_version'] and not upgraded:
        raise ClientError('恢复后的账本结构版本不一致。')
    for name in ('entries','accounts','categories','settings','receipts'):
        key={'entries':'id','accounts':'name','categories':'name','settings':'key','receipts':'filename'}[name]
        expected={r[key]:r for r in snapshot[name]}; found={r[key]:r for r in actual[name]}
        if name == 'settings' and upgraded:
            expected['schema_version'] = {**expected['schema_version'], 'value': '3'}
        if expected.keys()!=found.keys() or any(any(f not in found[k] or found[k][f]!=v for f,v in row.items()) for k,row in expected.items()):
            raise ClientError('恢复字段核对不一致：'+name)
    expected_audit={r['sequence']:r for r in snapshot['audit_events']}
    found_audit={r['sequence']:r for r in actual['audit_events']}
    if any(found_audit.get(k)!=v for k,v in expected_audit.items()):
        raise ClientError('恢复后的原始审计历史不一致。')
    maximum=max(expected_audit,default=0)
    extras=[r for k,r in found_audit.items() if k not in expected_audit]
    if any(r['sequence']<=maximum or r['action'] not in ('credential_create','credential_revoke','import') for r in extras):
        raise ClientError('恢复后出现额外财务变更，请核对。')
    return {'verified':True,'validation':validation}

def main(argv=None):
    parser=argparse.ArgumentParser(description='个人 Supabase 初始化、客户端凭据与完整备份')
    parser.add_argument('--config',type=Path,default=default_config('admin'))
    subs=parser.add_subparsers(dest='command',required=True)
    bootstrap=subs.add_parser('bootstrap',help='本地生成管理员配置及只含凭据哈希的初始化 SQL')
    bootstrap.add_argument('--url',required=True)
    bootstrap.add_argument('--sql-output',type=Path,required=True)
    issue=subs.add_parser('issue-client')
    issue.add_argument('--role',choices=['readonly','ai','admin'],required=True)
    issue.add_argument('--name',required=True)
    issue.add_argument('--output',type=Path,required=True)
    subs.add_parser('clients')
    revoke=subs.add_parser('revoke-client'); revoke.add_argument('id')
    subs.add_parser('unused-categories',help='列出没有任何账单使用的分类')
    subs.add_parser('prune-categories',help='删除没有任何账单使用的分类；先备份')
    backup=subs.add_parser('backup');backup.add_argument('--output',type=Path,required=True)
    check=subs.add_parser('verify-backup');check.add_argument('archive',type=Path)
    restore=subs.add_parser('restore');restore.add_argument('archive',type=Path)
    verify_remote=subs.add_parser('verify-remote');verify_remote.add_argument('archive',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='bootstrap':
            url=validate_url(args.url)
            if args.config.exists() or args.config.is_symlink() or args.sql_output.exists() or args.sql_output.is_symlink(): raise ClientError('输出已存在，拒绝覆盖。')
            identifier=uuid.uuid4().hex; token='ql_'+identifier+'.'+secrets.token_urlsafe(32)
            digest=hashlib.sha256(token.encode()).hexdigest()
            sql=("-- Apply only after personal ledger migrations, in YOUR project.\n"
                 "DO $$ BEGIN\nIF EXISTS (SELECT 1 FROM qiao.credentials WHERE role='admin' AND revoked_at IS NULL) THEN RAISE EXCEPTION 'Already initialized'; END IF;\n"
                 "INSERT INTO qiao.credentials(id,name,role,token_hash) VALUES ('"+identifier+"','Owner admin','admin','"+digest+"');\nEND $$;\n")
            private_write(args.sql_output,sql.encode())
            try: save_connection(args.config,url,token,use_keychain=False)
            except BaseException: args.sql_output.unlink(missing_ok=True);raise
            print(json.dumps({'config':str(args.config),'bootstrap_sql':str(args.sql_output),'next':'在本人项目执行 SQL 后验证连接；不要分发管理配置。'},ensure_ascii=False));return 0
        if args.command=='verify-backup':
            print(json.dumps({'verified':True,'validation':read_bundle(args.archive)[2]},ensure_ascii=False));return 0
        connection=load_connection(args.config,allow_environment=False)
        require_admin(connection)
        if args.command=='clients': result=request(connection,'GET','/api/credentials')
        elif args.command=='unused-categories': result=request(connection,'GET','/api/categories/unused')
        elif args.command=='prune-categories': result=request(connection,'DELETE','/api/categories/unused')
        elif args.command=='revoke-client': result=request(connection,'DELETE','/api/credentials/'+args.id)
        elif args.command=='issue-client':
            if args.output.exists() or args.output.is_symlink(): raise ClientError('配置已存在，拒绝覆盖。')
            result=request(connection,'POST','/api/credentials',{'name':args.name,'role':args.role})
            identifier=result['credential']['id']
            try: save_connection(args.output,connection.url,result['token'],use_keychain=False)
            except BaseException:
                request(connection,'DELETE','/api/credentials/'+identifier);raise
            result={'credential':result['credential'],'config':str(args.output)}
        elif args.command=='backup':
            snapshot=request(connection,'GET','/api/export')
            validation=write_bundle(args.output,snapshot,lambda name:connection.request('GET','/api/receipts/'+name,max_bytes=8*1024*1024)[0])
            result={'output':str(args.output),'validation':validation}
        elif args.command=='restore':
            snapshot,contents,validation=read_bundle(args.archive)
            if len(encoded(snapshot))>MAX_JSON: raise ClientError('快照超过本版导入上限 10 MiB。')
            current=request(connection,'GET','/api/export')
            if current.get('entries') or current.get('receipts'): raise ClientError('仅可恢复到空账本，不覆盖现有数据。')
            for receipt in snapshot['receipts']:
                request(connection,'POST','/api/receipts/import',{'filename':receipt['filename'],'sha256':receipt['sha256'],'data_base64':base64.b64encode(contents['receipts/'+receipt['filename']]).decode()})
            request(connection,'POST','/api/import',snapshot)
            verify_restored(connection,snapshot,validation)
            result={'restored':True,'validation':validation,'note':'请为新项目重新签发客户端凭据；备份不包含旧凭据。'}
        elif args.command=='verify-remote':
            snapshot,_,validation=read_bundle(args.archive)
            result=verify_restored(connection,snapshot,validation)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (ClientError,OSError,ValueError,KeyError,zipfile.BadZipFile) as exc:
        print('错误：'+str(exc),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
