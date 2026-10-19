-- Supabase's optional automatic-RLS event trigger needs no client RPC access.
-- Keep the trigger and its SECURITY DEFINER behavior intact; revoke only direct callers.
DO $hardening$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.proname='rls_auto_enable'
              AND p.pronargs=0 AND p.prorettype='event_trigger'::regtype) THEN
  REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM PUBLIC,anon,authenticated;
 END IF;
END $hardening$;
