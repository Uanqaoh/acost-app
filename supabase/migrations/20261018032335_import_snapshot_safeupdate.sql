-- Preserve pg-safeupdate and all existing empty-target, role and transaction guards.
CREATE OR REPLACE FUNCTION qiao.import_snapshot(snapshot jsonb, actor text) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, qiao AS $$
DECLARE field text; item jsonb; row_entry qiao.entries; extra_audit jsonb; maximum bigint;
BEGIN
 IF snapshot->>'format' IS DISTINCT FROM 'qiao-ledger-snapshot' OR snapshot->'version' IS DISTINCT FROM '1'::jsonb OR snapshot->'schema_version' IS DISTINCT FROM '2'::jsonb THEN PERFORM qiao.fail('未知备份格式'); END IF;
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
 IF (SELECT value FROM qiao.settings WHERE key='currency') IS DISTINCT FROM 'CAD' OR (SELECT value FROM qiao.settings WHERE key='schema_version') IS DISTINCT FROM '2' OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='book' AND length(value) BETWEEN 1 AND 100) OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='demo' AND value IN ('true','false')) OR NOT EXISTS(SELECT 1 FROM qiao.settings WHERE key='opening_balances_initialized' AND value IN ('true','false')) THEN PERFORM qiao.fail('备份账本设置无效'); END IF;
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
  IF jsonb_typeof(item->'date') IS DISTINCT FROM 'string' OR jsonb_typeof(item->'time') IS DISTINCT FROM 'string' THEN PERFORM qiao.fail('备份日期和时间无效'); END IF;
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

