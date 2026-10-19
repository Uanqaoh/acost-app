# Personal ledger Edge Function

This function serves one personal ledger in one Supabase project. It has no
workspace/tenant selector and uses no third-party runtime dependencies.

- Public endpoint: `https://PROJECT.supabase.co/functions/v1/ledger/api/health`.
- Protected routes use `Authorization: Bearer ql_<32 hex ID>.<43 character secret>`.
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are server-side environment variables.
  Never put the service-role key into a desktop configuration or distribute it.
- `verify_jwt = false` is intentional: the handler hashes an opaque ledger token
  and `public.qiao_dispatch` verifies it against active ledger credentials.
  Supabase Auth JWTs and project API keys are not ledger login credentials.
- Every protected request first checks `/api/meta`. SQL checks authorization
  again inside the transaction for the actual operation. Readonly credentials
  cannot upload files. Credentials, audit, import, export and backups need admin.
- Browser Origin/cross-site requests are rejected and CORS is not enabled.
  The local desktop server is the browser's API proxy.

## Receipts and restore

Normal `POST /api/receipts` takes `{filename, data_base64}`, verifies a PNG/JPEG/WebP
signature, uploads immutable bytes to the private `qiao-receipts` bucket under a
random name, then records `{filename, sha256, size}` through the SQL dispatcher.
The limit is 8 MiB per image and 12 MiB per streamed JSON request. Downloads check
registered metadata and SHA-256 before returning bytes, never a signed URL.

Admin-only `POST /api/receipts/import` takes `{filename, data_base64, sha256}` and
uploads an original 32-hex receipt filename without changing SQL metadata. An
existing object is accepted only if size and SHA-256 match; no object is replaced
or deleted. `cloud_admin.py` uploads all receipt bytes before submitting the
snapshot to `POST /api/import`, which atomically imports metadata into an empty
ledger. An interrupted upload can safely be retried.

`GET /api/export` downloads a canonical JSON snapshot. `GET /api/backup` responds
with `501 use_cloud_backup`, directing the admin to `cloud_admin.py backup`, which
also downloads and verifies all private receipt files.

When an upload succeeds but SQL explicitly rejects metadata, the newly created
random object is removed. A transport failure after dispatch may mean SQL already
committed, so its object is preserved to avoid breaking a committed receipt.
An unreferenced object can consequently remain after an ambiguous failure.

## Verification

From the repository root, with Node.js 22 or newer:

```sh
node --test supabase/functions/ledger/handler.test.mjs
```

The same dependency-free handler runs on Deno in Supabase; `index.ts` only binds
environment variables and starts the listener. The tests cover authentication,
role boundaries, routes, optimistic versions, bounded JSON streams, token
generation, full-size uploads, storage integrity and immutable import retries.
Cloud deployment is a separate step and requires the SQL migrations first.

Supabase's public gateway provides incoming HTTPS. Internal gateway forwarding
may use HTTP, so the handler does not infer public transport from `Request.url`.
All outbound RPC and Storage requests require a configured HTTPS origin and
reject redirects, protecting server credentials from cross-origin forwarding.

References checked for this implementation:
[authorization headers](https://supabase.com/docs/guides/functions/auth-headers),
[function configuration](https://supabase.com/docs/guides/functions/function-configuration),
[Storage HTTP API](https://supabase.com/docs/reference/self-hosting-storage/retrieve-an-object).
