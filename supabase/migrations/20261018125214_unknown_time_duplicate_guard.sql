-- Upgrade an existing personal ledger without rewriting historical times or entries.
-- Serialize with every live ledger mutation; permissions and safeupdate stay intact.
SELECT pg_advisory_xact_lock(713419267236593::bigint);
ALTER TABLE qiao.entries ALTER COLUMN time DROP NOT NULL;
CREATE INDEX entries_duplicate_candidates ON qiao.entries(type,date,amount_cents,account) WHERE deleted_at IS NULL;
UPDATE qiao.settings SET value='3' WHERE key='schema_version';


CREATE OR REPLACE FUNCTION qiao.normalize(value jsonb, original qiao.entries DEFAULT NULL) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, qiao AS $$
DECLARE kind text; acct text; dest text; cat text; receipt text; day text; hhmm text; merchant text; note text; cents bigint;
 zone text; local_now timestamp;
BEGIN
 IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN PERFORM qiao.fail('请求内容必须是 JSON 对象'); END IF;
 kind := coalesce(value->>'type','expense');
 IF original.id IS NOT NULL THEN kind := 'refund';
 ELSIF kind NOT IN ('expense','income','transfer') THEN PERFORM qiao.fail('type 必须是 expense、income 或 transfer；退款请使用 refunds 接口'); END IF;
 IF value ? 'currency' AND value->>'currency' IS DISTINCT FROM 'CAD' THEN PERFORM qiao.fail('本账本仅支持 CAD，请先把外币转换成加元'); END IF;
 SELECT coalesce((SELECT s.value FROM qiao.settings s WHERE s.key='timezone'),'America/Toronto') INTO zone;
 IF NOT value ? 'date' THEN local_now := clock_timestamp() AT TIME ZONE zone; END IF;
 day := CASE WHEN value ? 'date' THEN qiao.str(value->'date','date',10) ELSE to_char(local_now,'YYYY-MM-DD') END;
 IF NOT qiao.valid_date(day) THEN PERFORM qiao.fail('date 必须使用有效的 YYYY-MM-DD 日期'); END IF;
 -- Missing or explicit null means the transaction time was not recorded.
 hhmm := CASE WHEN value ? 'time' AND value->'time' <> 'null'::jsonb THEN qiao.str(value->'time','time',5) ELSE NULL END;
 IF hhmm !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$' THEN PERFORM qiao.fail('time 必须使用 HH:MM 格式'); END IF;
 cents := qiao.integer(value->'amount_cents','amount_cents',1,999999999999);
 acct := qiao.str(coalesce(value->'account','"Spending"'::jsonb),'account',100);
 IF NOT EXISTS (SELECT 1 FROM qiao.accounts WHERE name=acct) THEN PERFORM qiao.fail('账户不存在：' || acct); END IF;
 cat := qiao.str(coalesce(value->'category','"其他"'::jsonb),'category',100,true);
 merchant := qiao.str(value->'merchant','merchant',300,true);
 note := qiao.str(value->'note','note',10000,true);
 IF kind='transfer' THEN
  dest := qiao.str(value->'to_account','to_account',100);
  IF NOT EXISTS (SELECT 1 FROM qiao.accounts WHERE name=dest) THEN PERFORM qiao.fail('转入账户不存在'); END IF;
  IF acct=dest THEN PERFORM qiao.fail('转出与转入账户不能相同'); END IF;
  cat := '';
 END IF;
 IF original.id IS NOT NULL THEN acct:=original.account; cat:=original.category; merchant:=original.merchant; END IF;
 IF value ? 'receipt_url' AND value->'receipt_url' NOT IN ('null'::jsonb,'""'::jsonb) THEN
  receipt := qiao.str(value->'receipt_url','receipt_url',150);
  IF receipt !~ '^/api/receipts/[a-f0-9]{32}\.(png|jpg|webp)$' OR NOT EXISTS(SELECT 1 FROM qiao.receipts WHERE filename=substr(receipt,15)) THEN PERFORM qiao.fail('receipt_url 必须引用已上传的小票'); END IF;
 END IF;
 IF cat <> '' THEN INSERT INTO qiao.categories(name,color) VALUES(cat,'#aa9d89') ON CONFLICT(name) DO NOTHING; END IF;
 RETURN jsonb_build_object('type',kind,'amount_cents',cents,'date',day,'time',hhmm,'merchant',merchant,'note',note,'category',cat,'account',acct,'to_account',dest,'original_id',original.id,'receipt_url',receipt);
END $$;

-- A confirmation is for the exact set of current matches, never a blanket force flag.
-- Called under the dispatcher's exclusive transaction lock, after idempotency replay.
CREATE FUNCTION qiao.check_duplicates(value jsonb, acknowledgements jsonb DEFAULT NULL)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, qiao AS $$
DECLARE candidates jsonb; ids jsonb; acknowledged jsonb; total bigint;
BEGIN
 IF acknowledgements IS NULL THEN acknowledgements := '[]'::jsonb; END IF;
 IF jsonb_typeof(acknowledgements) IS DISTINCT FROM 'array' THEN
  PERFORM qiao.fail('duplicate_acknowledgements 必须是已核对账单 ID 的数组');
 END IF;
 IF jsonb_array_length(acknowledgements)>100 OR EXISTS(
  SELECT 1 FROM jsonb_array_elements(acknowledgements) x
  WHERE jsonb_typeof(x) IS DISTINCT FROM 'string' OR (x#>>'{}') !~ '^[a-f0-9]{32}$'
 ) OR (SELECT count(DISTINCT x) FROM jsonb_array_elements(acknowledgements) x)<>jsonb_array_length(acknowledgements) THEN
  PERFORM qiao.fail('确认清单包含重复、无效或过多账单 ID');
 END IF;
 SELECT coalesce(jsonb_agg(x ORDER BY x),'[]'::jsonb) INTO acknowledged FROM jsonb_array_elements(acknowledgements) x;
 WITH matches AS (
  SELECT e.* FROM qiao.entries e WHERE e.deleted_at IS NULL
   AND e.type=value->>'type' AND e.date=value->>'date'
   AND e.amount_cents=(value->>'amount_cents')::bigint AND e.account=value->>'account'
   AND e.to_account IS NOT DISTINCT FROM (value->>'to_account')
   AND e.original_id IS NOT DISTINCT FROM (value->>'original_id')
 ), preview AS (SELECT * FROM matches ORDER BY id LIMIT 100)
 SELECT (SELECT count(*) FROM matches),
  (SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'version',version,'type',type,'date',date,'time',time,
    'amount_cents',amount_cents,'currency','CAD','account',account,'merchant',merchant,'to_account',to_account,
    'original_id',original_id) ORDER BY id),'[]'::jsonb) FROM preview),
  (SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM preview)
 INTO total,candidates,ids;
 IF total<=100 AND acknowledged=ids THEN RETURN; END IF;
 RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='发现疑似重复账目，请先核对，未保存新账目',
 DETAIL=jsonb_build_object('status',409,'body',jsonb_build_object(
  'error','possible_duplicate','message','发现疑似重复账目，请先核对；同一笔沿用原记录，确认是不同消费后再提交完整确认清单。未保存新账目。',
  'candidates',candidates,'candidate_count',total,'candidates_truncated',total>100,
  'duplicate_acknowledgements',ids))::text;
END $$;
REVOKE ALL ON FUNCTION qiao.check_duplicates(jsonb,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION qiao.check_duplicates(jsonb,jsonb) TO service_role;


CREATE OR REPLACE FUNCTION qiao.snapshot() RETURNS jsonb LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, qiao AS $$
 SELECT jsonb_build_object('format','qiao-ledger-snapshot','version',1,'schema_version',3,'exported_at',qiao.stamp(),
 'settings',(SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY key),'[]'::jsonb) FROM qiao.settings s),
 'accounts',(SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY sort_order,name),'[]'::jsonb) FROM qiao.accounts a),
 'categories',(SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY sort_order,name),'[]'::jsonb) FROM qiao.categories c),
 'entries',(SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY date,time,id),'[]'::jsonb) FROM qiao.entries e),
 'receipts',(SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY filename),'[]'::jsonb) FROM qiao.receipts r),
 'audit_events',(SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY sequence),'[]'::jsonb) FROM qiao.audit_events a))
$$;

-- Preserve pg-safeupdate and all existing empty-target, role and transaction guards.
CREATE OR REPLACE FUNCTION qiao.import_snapshot(snapshot jsonb, actor text) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, qiao AS $$
DECLARE field text; item jsonb; row_entry qiao.entries; extra_audit jsonb; maximum bigint;
BEGIN
 IF snapshot->>'format' IS DISTINCT FROM 'qiao-ledger-snapshot' OR snapshot->'version' IS DISTINCT FROM '1'::jsonb OR (snapshot->'schema_version' IS DISTINCT FROM '2'::jsonb AND snapshot->'schema_version' IS DISTINCT FROM '3'::jsonb) THEN PERFORM qiao.fail('未知备份格式'); END IF;
 FOREACH field IN ARRAY ARRAY['settings','accounts','categories','entries','receipts','audit_events'] LOOP
  IF jsonb_typeof(snapshot->field) IS DISTINCT FROM 'array' THEN PERFORM qiao.fail('备份缺少数组：' || field); END IF;
  IF jsonb_array_length(snapshot->field)>100000 THEN PERFORM qiao.fail('备份记录过多',413,'payload_too_large'); END IF;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot->field) x WHERE jsonb_typeof(x)<>'object') THEN PERFORM qiao.fail('备份记录必须是对象'); END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM qiao.entries) OR EXISTS(SELECT 1 FROM qiao.receipts) OR EXISTS(SELECT 1 FROM qiao.audit_events WHERE action NOT IN ('credential_create','credential_revoke')) THEN PERFORM qiao.fail('只能导入到尚无账单和小票的新账本',409,'import_not_empty'); END IF;
 -- Preserve pre-import credential audit separately; incoming audit sequence IDs are exact.
 SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY sequence),'[]'::jsonb) INTO extra_audit FROM qiao.audit_events a;
 -- Hosted API sessions require a WHERE clause (pg-safeupdate).
 -- Only credential audit rows may exist; financial emptiness remains mandatory.
 DELETE FROM qiao.audit_events WHERE action IN ('credential_create','credential_revoke');
 DELETE FROM qiao.accounts WHERE NOT EXISTS(SELECT 1 FROM qiao.entries) AND NOT EXISTS(SELECT 1 FROM qiao.receipts);
 DELETE FROM qiao.categories WHERE NOT EXISTS(SELECT 1 FROM qiao.entries) AND NOT EXISTS(SELECT 1 FROM qiao.receipts);
 DELETE FROM qiao.settings WHERE NOT EXISTS(SELECT 1 FROM qiao.entries) AND NOT EXISTS(SELECT 1 FROM qiao.receipts);
 FOR item IN SELECT * FROM jsonb_array_elements(snapshot->'settings') LOOP
  INSERT INTO qiao.settings VALUES(qiao.str(item->'key','settings.key',100),qiao.str(item->'value','settings.value',1000,true));
 END LOOP;
 IF (SELECT value FROM qiao.settings WHERE key='currency') IS DISTINCT FROM 'CAD' OR (SELECT value FROM qiao.settings WHERE key='schema_version') IS DISTINCT FROM (snapshot->>'schema_version') OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='book' AND length(value) BETWEEN 1 AND 100) OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='demo' AND value IN ('true','false')) OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='opening_balances_initialized' AND value IN ('true','false')) THEN PERFORM qiao.fail('备份账本设置无效'); END IF;
 UPDATE qiao.settings SET value='3' WHERE key='schema_version';
 INSERT INTO qiao.settings VALUES('timezone','America/Toronto') ON CONFLICT(key) DO NOTHING;
 IF NOT EXISTS(SELECT 1 FROM pg_timezone_names WHERE name=(SELECT value FROM qiao.settings WHERE key='timezone')) THEN PERFORM qiao.fail('备份时区无效'); END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(snapshot->'accounts') LOOP
  INSERT INTO qiao.accounts(name,opening_balance_cents,sort_order) VALUES(qiao.str(item->'name','account',100),qiao.integer(item->'opening_balance_cents','opening_balance_cents',-999999999999,999999999999),coalesce(qiao.integer(coalesce(item->'sort_order',to_jsonb((SELECT count(*)+1 FROM qiao.accounts))),'sort_order',1,9223372036854775807),1));
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM qiao.accounts) THEN PERFORM qiao.fail('备份缺少账户'); END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(snapshot->'categories') LOOP
  INSERT INTO qiao.categories(name,color,sort_order) VALUES(qiao.str(item->'name','category',100),qiao.str(item->'color','color',7),qiao.integer(coalesce(item->'sort_order',to_jsonb((SELECT count(*)+1 FROM qiao.categories))),'sort_order',1,9223372036854775807));
 END LOOP;
 FOR item IN SELECT * FROM jsonb_array_elements(snapshot->'receipts') LOOP
  PERFORM qiao.integer(item->'size','size',1,8388608);
  INSERT INTO qiao.receipts SELECT (jsonb_populate_record(NULL::qiao.receipts,item)).*;
 END LOOP;
 -- Originals first, so every foreign key is checked inside this function's rollback boundary.
 FOR item IN SELECT x FROM jsonb_array_elements(snapshot->'entries') x ORDER BY (x->>'type'='refund'),x->>'id' LOOP
  PERFORM qiao.integer(item->'amount_cents','amount_cents',1,999999999999);
  PERFORM qiao.integer(item->'version','version',1,9223372036854775807);
  IF jsonb_typeof(item->'date') IS DISTINCT FROM 'string' OR NOT item ? 'time'
   OR (snapshot->'schema_version'='2'::jsonb AND jsonb_typeof(item->'time') IS DISTINCT FROM 'string')
   OR (snapshot->'schema_version'='3'::jsonb AND jsonb_typeof(item->'time') NOT IN ('string','null'))
   THEN PERFORM qiao.fail('备份日期和时间无效'); END IF;
  INSERT INTO qiao.entries SELECT (jsonb_populate_record(NULL::qiao.entries,item)).*;
 END LOOP;
 IF EXISTS(SELECT 1 FROM qiao.entries e WHERE (e.category<>'' AND NOT EXISTS(SELECT 1 FROM qiao.categories c WHERE c.name=e.category)) OR (e.receipt_url IS NOT NULL AND NOT EXISTS(SELECT 1 FROM qiao.receipts r WHERE r.filename=substr(e.receipt_url,15)))) THEN PERFORM qiao.fail('备份引用了不存在的分类或小票'); END IF;
 IF EXISTS(SELECT 1 FROM qiao.entries r JOIN qiao.entries o ON o.id=r.original_id WHERE r.type='refund' AND (r.id=o.id OR (r.deleted_at IS NULL AND (o.type<>'expense' OR o.deleted_at IS NOT NULL OR r.account<>o.account OR r.category<>o.category)))) OR EXISTS(SELECT 1 FROM qiao.entries e WHERE e.type='expense' AND qiao.refunded(e.id)>e.amount_cents) THEN PERFORM qiao.fail('备份退款关系或金额无效',409,'refund_conflict'); END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(snapshot->'audit_events') LOOP
  PERFORM qiao.integer(item->'sequence','sequence',1,9223372036854775806);
  IF length(item->>'action') NOT BETWEEN 1 AND 100 OR length(item->>'entity_id') NOT BETWEEN 1 AND 200 THEN PERFORM qiao.fail('备份审计记录无效'); END IF;
  INSERT INTO qiao.audit_events SELECT (jsonb_populate_record(NULL::qiao.audit_events,item)).*;
 END LOOP;
 SELECT coalesce(max(sequence),0) INTO maximum FROM qiao.audit_events;
 PERFORM setval(pg_get_serial_sequence('qiao.audit_events','sequence'),greatest(maximum,1),maximum>0);
 FOR item IN SELECT * FROM jsonb_array_elements(extra_audit) LOOP
  PERFORM qiao.audit(item->>'actor',item->>'action',item->>'entity_id',item->'before_json',item->'after_json');
 END LOOP;
 -- An import marker prevents an empty snapshot from being repeatedly applied.
 PERFORM qiao.audit(actor,'import','snapshot',NULL,jsonb_build_object('entries',jsonb_array_length(snapshot->'entries'),'receipts',jsonb_array_length(snapshot->'receipts')));
 SELECT coalesce(max(sort_order),0) INTO maximum FROM qiao.accounts;
 PERFORM setval(pg_get_serial_sequence('qiao.accounts','sort_order'),greatest(maximum,1),maximum>0);
 SELECT coalesce(max(sort_order),0) INTO maximum FROM qiao.categories;
 PERFORM setval(pg_get_serial_sequence('qiao.categories','sort_order'),greatest(maximum,1),maximum>0);
 RETURN jsonb_build_object('imported',true,'entries',jsonb_array_length(snapshot->'entries'),'receipts',jsonb_array_length(snapshot->'receipts'),'revision',qiao.revision());
END $$;



CREATE OR REPLACE FUNCTION public.qiao_dispatch(p_token_hash text, p_method text, p_path text, p_query jsonb DEFAULT '{}'::jsonb, p_body jsonb DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog, qiao AS $$
DECLARE principal qiao.credentials; cred qiao.credentials; row_entry qiao.entries; original qiao.entries; existing qiao.entries;
 actor text; method text := upper(p_method); path text := p_path; payload jsonb := p_body; result jsonb; before_value jsonb; original_before jsonb; normalized jsonb;
 entry_id text; action text; key text; digest text; expected jsonb; stamp text; receipt_filename text; selected_month text; search text;
 page_limit integer; page_offset bigint; total bigint; data jsonb; public_cred jsonb; status integer := 200; error_detail text;
BEGIN
 -- Shared read locks and exclusive write locks give every response one coherent
 -- financial revision, including revocations, balances, refunds and pagination.
 -- All accesses below occur after lock acquisition (VOLATILE query snapshots).
 IF method IN ('GET','HEAD') THEN PERFORM pg_advisory_xact_lock_shared(713419267236593::bigint);
 ELSE PERFORM pg_advisory_xact_lock(713419267236593::bigint); END IF;
 IF path='/api/health' AND method IN ('GET','HEAD') THEN RETURN jsonb_build_object('status',200,'body',jsonb_build_object('ok',true)); END IF;
 SELECT * INTO principal FROM qiao.credentials WHERE token_hash=p_token_hash AND revoked_at IS NULL;
 IF NOT FOUND OR p_token_hash IS NULL OR p_token_hash !~ '^[a-f0-9]{64}$' THEN PERFORM qiao.fail('访问凭证无效或已撤销',401,'unauthorized'); END IF;
 actor := principal.id;
 IF path IN ('/api/credentials','/api/audit','/api/export','/api/backup','/api/import') OR path LIKE '/api/credentials/%' THEN
  IF principal.role<>'admin' THEN PERFORM qiao.fail('此操作需要管理凭证',403,'forbidden'); END IF;
 END IF;
 IF method NOT IN ('GET','HEAD') AND principal.role='readonly' THEN PERFORM qiao.fail('此凭证仅可查看账本',403,'readonly'); END IF;
 IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR jsonb_typeof(p_query) IS DISTINCT FROM 'object' THEN PERFORM qiao.fail('请求内容必须是 JSON 对象'); END IF;
 IF path IS NULL OR path !~ '^/api/[A-Za-z0-9_./-]+$' OR path ~ '(^|/)\.\.(/|$)' THEN PERFORM qiao.fail('无效路径'); END IF;
 IF method='HEAD' THEN method := 'GET'; END IF;
 IF path='/api/meta' AND method='GET' THEN result := qiao.meta() || jsonb_build_object('client',jsonb_build_object('id',principal.id,'name',principal.name,'role',principal.role));
 ELSIF path IN ('/api/export','/api/backup') AND method='GET' THEN result := qiao.snapshot();
 ELSIF path='/api/import' AND method='POST' THEN result := qiao.import_snapshot(payload,actor); status:=201;
 ELSIF path IN ('/api/entries','/api/audit') AND method='GET' THEN
  IF coalesce(p_query->>'limit','100') !~ '^[0-9]{1,10}$' OR coalesce(p_query->>'offset','0') !~ '^[0-9]{1,10}$' THEN PERFORM qiao.fail('limit 和 offset 必须为整数'); END IF;
  IF jsonb_typeof(p_query->'limit') NOT IN ('string','number') OR jsonb_typeof(p_query->'offset') NOT IN ('string','number') THEN PERFORM qiao.fail('limit 和 offset 必须为整数'); END IF;
  IF (coalesce(p_query->>'limit','100'))::bigint NOT BETWEEN 1 AND 500 THEN PERFORM qiao.fail('limit 必须在 1 到 500 之间'); END IF;
  page_limit := coalesce(p_query->>'limit','100')::integer; page_offset := coalesce(p_query->>'offset','0')::bigint;
  IF path='/api/audit' THEN
   SELECT count(*) INTO total FROM qiao.audit_events WHERE nullif(p_query->>'entity_id','') IS NULL OR entity_id=p_query->>'entity_id';
   SELECT coalesce(jsonb_agg((to_jsonb(a)-'before_json'-'after_json') || jsonb_build_object('before',before_json,'after',after_json) ORDER BY sequence DESC),'[]'::jsonb) INTO data FROM (SELECT * FROM qiao.audit_events WHERE nullif(p_query->>'entity_id','') IS NULL OR entity_id=p_query->>'entity_id' ORDER BY sequence DESC LIMIT page_limit OFFSET page_offset) a;
   result := jsonb_build_object('events',data);
  ELSE
   selected_month := nullif(p_query->>'month','');
   IF selected_month IS NOT NULL AND selected_month !~ '^[0-9]{4}-(0[1-9]|1[0-2])$' THEN PERFORM qiao.fail('month 必须使用 YYYY-MM 格式'); END IF;
   IF nullif(p_query->>'type','') IS NOT NULL AND p_query->>'type' NOT IN ('expense','income','transfer','refund') THEN PERFORM qiao.fail('未知账单类型'); END IF;
   search := qiao.str(p_query->'q','q',300,true);
   WITH filtered AS (SELECT * FROM qiao.entries WHERE deleted_at IS NULL AND (selected_month IS NULL OR left(date,7)=selected_month) AND (nullif(p_query->>'category','') IS NULL OR category=p_query->>'category') AND (nullif(p_query->>'account','') IS NULL OR account=p_query->>'account') AND (nullif(p_query->>'type','') IS NULL OR type=p_query->>'type') AND (search='' OR strpos(lower(merchant),lower(search))>0 OR strpos(lower(note),lower(search))>0 OR strpos(lower(category),lower(search))>0))
   SELECT (SELECT count(*) FROM filtered),(SELECT coalesce(jsonb_agg(qiao.entry_json(f.id) ORDER BY f.date DESC,f.time DESC NULLS LAST,f.created_at DESC,f.id DESC),'[]'::jsonb) FROM (SELECT * FROM filtered ORDER BY date DESC,time DESC NULLS LAST,created_at DESC,id DESC LIMIT page_limit OFFSET page_offset) f) INTO total,data;
   result := jsonb_build_object('entries',data);
  END IF;
  result := result || jsonb_build_object('total',total,'limit',page_limit,'offset',page_offset,'revision',qiao.revision(),'next_offset',CASE WHEN page_offset+jsonb_array_length(data)<total THEN page_offset+jsonb_array_length(data) END);
 ELSIF path='/api/summary' AND method='GET' THEN
  selected_month := nullif(p_query->>'month','');
  IF selected_month IS NOT NULL AND selected_month !~ '^[0-9]{4}-(0[1-9]|1[0-2])$' THEN PERFORM qiao.fail('month 必须使用 YYYY-MM 格式'); END IF;
  result := qiao.summary(selected_month);
 ELSIF path='/api/credentials' AND method='GET' THEN
  SELECT jsonb_build_object('credentials',coalesce(jsonb_agg(qiao.credential_json(c) ORDER BY created_at,id),'[]'::jsonb)) INTO result FROM qiao.credentials c;
 ELSIF path='/api/credentials' AND method='POST' THEN
  INSERT INTO qiao.credentials(id,name,role,token_hash) VALUES(qiao.str(payload->'id','id',32),qiao.str(payload->'name','name',100),qiao.str(payload->'role','role',20),qiao.str(payload->'token_hash','token_hash',64)) RETURNING * INTO cred;
  public_cred := qiao.credential_json(cred);
  PERFORM qiao.audit(actor,'credential_create',cred.id,NULL,public_cred);
  result := jsonb_build_object('credential',public_cred); status := 201;
 ELSIF path ~ '^/api/credentials/[a-f0-9]{32}$' AND method='DELETE' THEN
  SELECT * INTO cred FROM qiao.credentials WHERE id=split_part(path,'/',4);
  IF NOT FOUND THEN PERFORM qiao.fail('凭证不存在',404,'not_found'); END IF;
  before_value := qiao.credential_json(cred);
  IF cred.revoked_at IS NULL THEN
   IF cred.role='admin' AND (SELECT count(*) FROM qiao.credentials WHERE role='admin' AND revoked_at IS NULL)<=1 THEN PERFORM qiao.fail('不能撤销最后一个管理凭证；请先创建替代凭证',409,'last_admin'); END IF;
   UPDATE qiao.credentials SET revoked_at=qiao.stamp() WHERE id=cred.id RETURNING * INTO cred;
   PERFORM qiao.audit(actor,'credential_revoke',cred.id,before_value,qiao.credential_json(cred));
  END IF;
  result := jsonb_build_object('credential',qiao.credential_json(cred));
 ELSIF path='/api/receipts' AND method='POST' THEN
  receipt_filename:=qiao.str(payload->'filename','filename',40);
  INSERT INTO qiao.receipts(filename,sha256,size,created_by) VALUES(receipt_filename,qiao.str(payload->'sha256','sha256',64),qiao.integer(payload->'size','size',1,8388608),actor);
  SELECT to_jsonb(r) INTO data FROM qiao.receipts r WHERE r.filename=receipt_filename;
  PERFORM qiao.audit(actor,'receipt_upload',receipt_filename,NULL,data);
  result := jsonb_build_object('receipt_url','/api/receipts/'||receipt_filename); status:=201;
 ELSIF path ~ '^/api/receipts/[a-f0-9]{32}\.(png|jpg|webp)$' AND method='GET' THEN
  SELECT to_jsonb(r) INTO data FROM qiao.receipts r WHERE r.filename=split_part(path,'/',4);
  IF NOT FOUND THEN PERFORM qiao.fail('小票不存在',404,'not_found'); END IF;
  result:=jsonb_build_object('receipt',data);
 ELSIF (path='/api/entries' AND method='POST') OR (path ~ '^/api/entries/[a-f0-9]{32}/refunds$' AND method='POST') THEN
  expected:=payload->'expected_version'; payload:=payload-'expected_version';
  IF path='/api/entries' THEN action:='create'; ELSE entry_id:=split_part(path,'/',4); action:='refund:'||entry_id; END IF;
  key:=qiao.str(payload->'idempotency_key','idempotency_key',200);
  digest:=encode(sha256(convert_to(qiao.canonical(jsonb_build_object('action',action,'entry',payload-'idempotency_key'-'duplicate_acknowledgements')),'UTF8')),'hex');
  IF key IS NOT NULL THEN
   SELECT * INTO existing FROM qiao.entries WHERE idempotency_key=key;
   IF FOUND THEN
    IF existing.request_hash IS DISTINCT FROM digest THEN PERFORM qiao.fail('该 idempotency_key 已用于另一项操作',409,'idempotency_conflict'); END IF;
    RETURN jsonb_build_object('status',201,'body',jsonb_build_object('entry',qiao.entry_json(existing.id)));
   END IF;
  END IF;
  IF action<>'create' THEN
   SELECT * INTO original FROM qiao.entries WHERE id=entry_id AND deleted_at IS NULL;
   IF NOT FOUND THEN PERFORM qiao.fail('账单不存在',404,'not_found'); END IF;
   PERFORM qiao.check_version(original,expected,false);
   IF original.type<>'expense' THEN PERFORM qiao.fail('退款必须关联一笔支出',409,'refund_conflict'); END IF;
   original_before:=qiao.entry_json(original.id);
  END IF;
  normalized:=qiao.normalize(payload,original);
  IF original.id IS NOT NULL AND qiao.refunded(original.id)+(normalized->>'amount_cents')::bigint>original.amount_cents THEN PERFORM qiao.fail('累计退款不能超过原支出金额',409,'refund_conflict'); END IF;
  PERFORM qiao.check_duplicates(normalized,payload->'duplicate_acknowledgements');
  entry_id:=replace(gen_random_uuid()::text,'-',''); stamp:=qiao.stamp();
  normalized:=normalized || jsonb_build_object('id',entry_id,'idempotency_key',key,'request_hash',digest,'created_at',stamp,'updated_at',stamp,'version',1,'created_by',actor,'updated_by',actor);
  INSERT INTO qiao.entries SELECT (jsonb_populate_record(NULL::qiao.entries,normalized)).*;
  data:=qiao.entry_json(entry_id); PERFORM qiao.audit(actor,CASE WHEN original.id IS NULL THEN 'create' ELSE 'refund' END,entry_id,NULL,data);
  IF original.id IS NOT NULL THEN PERFORM qiao.touch_original(original.id,actor,original_before); END IF;
  result:=jsonb_build_object('entry',data); status:=201;
 ELSIF path ~ '^/api/entries/[a-f0-9]{32}(/restore)?$' THEN
  entry_id:=split_part(path,'/',4); action:=nullif(split_part(path,'/',5),'');
  IF (action='restore' AND method<>'POST') OR (action IS NULL AND method NOT IN ('GET','PATCH','DELETE')) THEN PERFORM qiao.fail('接口不存在',404,'not_found'); END IF;
  IF method='GET' AND coalesce(p_query->>'include_deleted','false') NOT IN ('true','false') THEN PERFORM qiao.fail('include_deleted 必须是 true 或 false'); END IF;
  SELECT * INTO row_entry FROM qiao.entries WHERE id=entry_id;
  IF NOT FOUND OR (row_entry.deleted_at IS NOT NULL AND (method='PATCH' OR (method='GET' AND coalesce(p_query->>'include_deleted','false')<>'true'))) THEN PERFORM qiao.fail('账单不存在',404,'not_found'); END IF;
  IF method='GET' THEN result:=jsonb_build_object('entry',qiao.entry_json(entry_id));
  ELSE
   PERFORM qiao.check_version(row_entry,payload->'expected_version'); payload:=payload-'expected_version'; before_value:=qiao.entry_json(entry_id);
   IF method='PATCH' THEN
    IF (payload-ARRAY['type','amount_cents','date','time','merchant','note','category','account','to_account','receipt_url'])<>'{}'::jsonb THEN PERFORM qiao.fail('包含不支持修改的字段'); END IF;
    IF row_entry.type='refund' THEN PERFORM qiao.fail('退款请先撤销，再重新登记',409,'refund_conflict'); END IF;
    normalized:=qiao.normalize(to_jsonb(row_entry)||payload);
    IF qiao.refunded(entry_id)>0 AND (normalized->>'type'<>'expense' OR (normalized->>'amount_cents')::bigint<qiao.refunded(entry_id) OR normalized->>'account'<>row_entry.account OR normalized->>'category'<>row_entry.category) THEN PERFORM qiao.fail('已有退款：不能改变类型、账户、分类或把金额改为小于已退金额',409,'refund_conflict'); END IF;
    UPDATE qiao.entries SET type=normalized->>'type',amount_cents=(normalized->>'amount_cents')::bigint,date=normalized->>'date',time=normalized->>'time',merchant=normalized->>'merchant',note=normalized->>'note',category=normalized->>'category',account=normalized->>'account',to_account=normalized->>'to_account',receipt_url=normalized->>'receipt_url',updated_at=qiao.stamp(),updated_by=actor,version=version+1 WHERE id=entry_id;
    PERFORM qiao.audit(actor,'edit',entry_id,before_value,qiao.entry_json(entry_id));
   ELSE
    IF payload<>'{}'::jsonb THEN PERFORM qiao.fail('删除或恢复请求只接受 expected_version'); END IF;
    IF (method='DELETE' AND row_entry.deleted_at IS NULL) OR (action='restore' AND row_entry.deleted_at IS NOT NULL) THEN
     IF method='DELETE' AND row_entry.type='expense' AND qiao.refunded(entry_id)>0 THEN PERFORM qiao.fail('请先撤销相关退款，再删除原支出',409,'refund_conflict'); END IF;
     IF row_entry.type='refund' THEN
      SELECT * INTO original FROM qiao.entries WHERE id=row_entry.original_id;
      original_before:=qiao.entry_json(original.id);
      IF action='restore' THEN
       IF original.deleted_at IS NOT NULL THEN PERFORM qiao.fail('原支出不存在',404,'not_found'); END IF;
       IF original.type<>'expense' OR qiao.refunded(original.id)+row_entry.amount_cents>original.amount_cents OR row_entry.account<>original.account OR row_entry.category<>original.category THEN PERFORM qiao.fail('原支出已改变或恢复后会超额，无法恢复退款',409,'refund_conflict'); END IF;
      END IF;
     END IF;
     stamp:=qiao.stamp();
     UPDATE qiao.entries SET deleted_at=CASE WHEN method='DELETE' THEN stamp ELSE NULL END,updated_at=stamp,updated_by=actor,version=version+1 WHERE id=entry_id;
     PERFORM qiao.audit(actor,CASE WHEN method='DELETE' THEN 'delete' ELSE 'restore' END,entry_id,before_value,qiao.entry_json(entry_id));
     IF original.id IS NOT NULL THEN PERFORM qiao.touch_original(original.id,actor,original_before); END IF;
    END IF;
   END IF;
   result:=jsonb_build_object('entry',qiao.entry_json(entry_id));
   IF method='DELETE' THEN result:=result||jsonb_build_object('deleted',true); END IF;
  END IF;
 ELSE PERFORM qiao.fail('接口不存在',404,'not_found'); END IF;
 RETURN jsonb_build_object('status',status,'body',result);
EXCEPTION
 WHEN SQLSTATE 'P0001' THEN GET STACKED DIAGNOSTICS error_detail=PG_EXCEPTION_DETAIL; RETURN error_detail::jsonb;
 WHEN integrity_constraint_violation OR invalid_text_representation OR numeric_value_out_of_range OR invalid_datetime_format OR datetime_field_overflow THEN
  RETURN jsonb_build_object('status',400,'body',jsonb_build_object('error','invalid_request','message','请求数据格式、引用或数值不正确'));
END $$;
