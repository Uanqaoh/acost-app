import { createHandler } from "./handler.mjs";

// Supabase injects these server-side environment variables. Neither is accepted
// from a request or returned to the client. Opaque ledger tokens are checked in
// qiao_dispatch, so this function intentionally uses verify_jwt = false.
Deno.serve(createHandler({
  supabaseUrl: Deno.env.get("SUPABASE_URL"),
  serviceRoleKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY"),
}));
