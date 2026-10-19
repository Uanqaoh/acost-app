// One ledger per Supabase project. Only this server receives the service-role key.
export const MAX_BODY = 12 * 1024 * 1024;
export const MAX_IMAGE = 8 * 1024 * 1024;
const RECEIPT_NAME = /^[a-f0-9]{32}\.(png|jpg|webp)$/;
const TOKEN = /^Bearer (ql_[a-f0-9]{32}\.[A-Za-z0-9_-]{43})$/;
const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
};
const MIME = { png: "image/png", jpg: "image/jpeg", webp: "image/webp" };
const READ_METHODS = new Set(["GET", "HEAD"]);
const ROUTES = [
  [/^\/api\/(health|meta|summary|audit|export|backup)$/, ["GET", "HEAD"]],
  [/^\/api\/entries$/, ["GET", "HEAD", "POST"]],
  [/^\/api\/entries\/[a-f0-9]{32}$/, ["GET", "HEAD", "PATCH", "DELETE"]],
  [/^\/api\/entries\/[a-f0-9]{32}\/(restore|refunds)$/, ["POST"]],
  [/^\/api\/receipts$/, ["POST"]],
  [/^\/api\/receipts\/import$/, ["POST"]],
  [/^\/api\/receipts\/[a-f0-9]{32}\.(png|jpg|webp)$/, ["GET", "HEAD"]],
  [/^\/api\/credentials$/, ["GET", "HEAD", "POST"]],
  [/^\/api\/credentials\/[a-f0-9]{32}$/, ["DELETE"]],
  [/^\/api\/import$/, ["POST"]],
];

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function fail(status, code, message) { throw new ApiError(status, code, message); }
function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function hex(bytes) { return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(""); }

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8", ...extraHeaders },
  });
}

function dispatchResponse(result, extraHeaders = {}) {
  return jsonResponse(result.body, result.status, {
    ...(result.status === 401 ? { "WWW-Authenticate": "Bearer" } : {}), ...extraHeaders,
  });
}

function canonicalRequest(request) {
  const url = new URL(request.url);
  // The gateway may forward over internal HTTP. Public clients must use its HTTPS URL.
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.hash ||
      url.pathname.length > 512 || /[%\\\x00-\x20]/.test(url.pathname) ||
      url.pathname.includes("//") || url.pathname.split("/").some((part) => part === "." || part === "..")) {
    fail(400, "invalid_request", "无效路径");
  }
  let path;
  for (const prefix of ["/functions/v1/ledger", "/ledger"]) {
    if (url.pathname.startsWith(`${prefix}/api/`)) {
      path = url.pathname.slice(prefix.length);
      break;
    }
  }
  if (!path) fail(404, "not_found", "接口不存在");
  if (url.search.length > 4096) fail(400, "invalid_request", "查询参数过长");
  const query = Object.create(null);
  for (const [key, value] of url.searchParams) {
    if (Object.hasOwn(query, key)) fail(400, "invalid_request", "查询参数不能重复");
    query[key] = value;
  }
  return { path, query };
}

async function readBytes(stream, maximum, tooLargeMessage) {
  if (!stream) return new Uint8Array();
  const reader = stream.getReader();
  const parts = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) {
        await reader.cancel().catch(() => {});
        fail(413, "payload_too_large", tooLargeMessage);
      }
      parts.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const content = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    content.set(part, offset);
    offset += part.byteLength;
  }
  return content;
}

async function readPayload(request) {
  const length = request.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || !Number.isSafeInteger(Number(length)))) {
    fail(400, "invalid_request", "Content-Length 无效");
  }
  if (Number(length) > MAX_BODY) fail(413, "payload_too_large", "请求最大 12 MB");
  if (request.headers.has("content-encoding") && request.headers.get("content-encoding") !== "identity") {
    fail(415, "unsupported_media_type", "不接受压缩请求");
  }
  const content = await readBytes(request.body, MAX_BODY, "请求最大 12 MB");
  if (!content.length) return {};
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    fail(415, "unsupported_media_type", "Content-Type 必须为 application/json");
  }
  let payload;
  try { payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(content)); }
  catch { fail(400, "invalid_request", "无法解析 JSON"); }
  if (!object(payload)) fail(400, "invalid_request", "请求内容必须是 JSON 对象");
  return payload;
}

function applyVersion(request, path, payload) {
  if (!/^\/api\/entries\/[a-f0-9]{32}(\/(restore|refunds))?$/.test(path) || READ_METHODS.has(request.method)) return;
  const value = request.headers.get("if-match");
  if (value !== null) {
    if (!/^(?:[1-9]\d*|"[1-9]\d*")$/.test(value)) fail(400, "invalid_request", "If-Match 必须是账单版本号");
    const version = Number(value.replaceAll('"', ""));
    if (!Number.isSafeInteger(version) || (payload.expected_version !== undefined && payload.expected_version !== version)) {
      fail(400, "invalid_request", "expected_version 与 If-Match 不一致或无效");
    }
    payload.expected_version = version;
  }
  if (payload.expected_version === undefined && !path.endsWith("/refunds")) {
    fail(428, "version_required", "请先读取账单，并提供 expected_version");
  }
  if (payload.expected_version !== undefined && (!Number.isSafeInteger(payload.expected_version) || payload.expected_version < 1)) {
    fail(400, "invalid_request", "expected_version 必须为正整数");
  }
}

function imageExtension(bytes) {
  if (bytes.length >= 8 && [137, 80, 78, 71, 13, 10, 26, 10].every((value, i) => bytes[i] === value)) return "png";
  if (bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "jpg";
  if (bytes.length >= 12 && [82, 73, 70, 70].every((value, i) => bytes[i] === value) &&
      [87, 69, 66, 80].every((value, i) => bytes[i + 8] === value)) return "webp";
  fail(400, "invalid_request", "仅支持 PNG、JPEG 或 WebP 图片");
}

function decodeImage(payload) {
  if (typeof payload.filename !== "string" || !payload.filename.length || payload.filename.length > 255 ||
      typeof payload.data_base64 !== "string") fail(400, "invalid_request", "需要 filename 和 data_base64");
  const encoded = payload.data_base64;
  if (encoded.length > Math.ceil(MAX_IMAGE / 3) * 4) fail(413, "payload_too_large", "小票图片最大 8 MB");
  // Avoid a repeated capture-group pattern: V8 can exhaust its regexp stack
  // on legitimate multi-megabyte images. atob also validates padding.
  if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) {
    fail(400, "invalid_request", "图片编码不是有效 Base64");
  }
  let binary;
  try { binary = atob(encoded); } catch { fail(400, "invalid_request", "图片编码不是有效 Base64"); }
  if (!binary.length || binary.length > MAX_IMAGE) fail(413, "payload_too_large", "小票图片为空或超过 8 MB");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return { bytes, extension: imageExtension(bytes) };
}

export function createHandler({ supabaseUrl, serviceRoleKey, fetchImpl = fetch, cryptoImpl = crypto }) {
  const origin = new URL(supabaseUrl);
  if (origin.protocol !== "https:" || origin.username || origin.password || origin.search || origin.hash ||
      origin.pathname !== "/" || !serviceRoleKey || /[\r\n]/.test(serviceRoleKey)) {
    throw new Error("A secure Supabase origin and server-only service-role key are required");
  }
  const baseUrl = origin.origin;
  const authHeaders = { apikey: serviceRoleKey, Authorization: `Bearer ${serviceRoleKey}` };
  const digest = async (value) => hex(new Uint8Array(await cryptoImpl.subtle.digest("SHA-256", value)));
  const secureFetch = (path, init = {}) => fetchImpl(`${baseUrl}${path}`, {
    ...init, redirect: "error", signal: AbortSignal.timeout(25_000),
    headers: { ...authHeaders, ...init.headers },
  });
  const dispatch = async (tokenHash, method, path, query = {}, body = {}) => {
    const response = await secureFetch("/rest/v1/rpc/qiao_dispatch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ p_token_hash: tokenHash, p_method: method, p_path: path, p_query: query, p_body: body }),
    });
    if (!response.ok) fail(502, "upstream_error", "云端暂时无法处理请求");
    const result = await response.json();
    if (!object(result) || !Number.isInteger(result.status) || result.status < 200 || result.status >= 600 ||
        (result.status >= 300 && result.status < 400) || !object(result.body)) {
      fail(502, "upstream_error", "云端返回无效响应");
    }
    if (result.status >= 500) fail(503, "upstream_error", "云端暂时无法处理请求");
    return result;
  };
  const readStored = async (filename) => {
    const response = await secureFetch(`/storage/v1/object/authenticated/qiao-receipts/${filename}`);
    if (!response.ok) fail(502, "storage_error", "无法读取云端小票");
    return readBytes(response.body, MAX_IMAGE, "云端小票超过 8 MB");
  };
  const storeImage = async (filename, bytes, extension, allowIdentical) => {
    const response = await secureFetch(`/storage/v1/object/qiao-receipts/${filename}`, {
      method: "POST", headers: { "Content-Type": MIME[extension], "x-upsert": "false", "Cache-Control": "no-store" }, body: bytes,
    });
    if (response.ok) return true;
    // Storage versions report an immutable-object conflict as either 400 or 409.
    if (allowIdentical && [400, 409].includes(response.status)) {
      const existing = await readStored(filename);
      if (existing.length === bytes.length && await digest(existing) === await digest(bytes)) return false;
      fail(409, "receipt_conflict", "同名小票内容不一致，不能覆盖");
    }
    fail(502, "storage_error", "无法保存云端小票");
  };
  const removeNewImage = async (filename) => {
    // This is called only after THIS request successfully created a random object.
    // Never remove an existing object during a failed import/retry.
    await secureFetch(`/storage/v1/object/qiao-receipts/${filename}`, { method: "DELETE" }).catch(() => {});
  };

  async function handle(request) {
    const { path, query } = canonicalRequest(request);
    const method = request.method;
    if (path === "/api/health" && READ_METHODS.has(method)) return jsonResponse({ ok: true });
    if (request.headers.has("origin") || request.headers.get("sec-fetch-site") === "cross-site") {
      fail(403, "forbidden", "接口不接受浏览器跨站请求");
    }
    const match = TOKEN.exec(request.headers.get("authorization") || "");
    if (!match) fail(401, "unauthorized", "访问凭证无效或已撤销");
    const tokenHash = await digest(new TextEncoder().encode(match[1]));
    const meta = await dispatch(tokenHash, "GET", "/api/meta");
    if (meta.status !== 200) return dispatchResponse(meta);
    const role = meta.body.client?.role;
    if (!["readonly", "ai", "admin"].includes(role)) fail(502, "upstream_error", "云端返回无效权限");
    if (/^\/api\/(credentials(?:\/|$)|audit$|export$|backup$|import$|receipts\/import$)/.test(path) && role !== "admin") {
      fail(403, "forbidden", "此操作需要管理凭证");
    }
    if (!READ_METHODS.has(method) && role === "readonly") fail(403, "readonly", "此凭证仅可查看账本");
    const route = ROUTES.find(([pattern]) => pattern.test(path));
    if (!route) fail(404, "not_found", "接口不存在");
    if (!route[1].includes(method)) fail(405, "method_not_allowed", "此接口不支持该请求方法");
    if (path === "/api/meta") return dispatchResponse(meta);
    if (path === "/api/backup") {
      fail(501, "use_cloud_backup", "请运行 cloud_admin.py backup 下载包含账本与全部小票的完整备份");
    }
    const payload = READ_METHODS.has(method) ? {} : await readPayload(request);
    applyVersion(request, path, payload);

    if (method === "POST" && path === "/api/credentials") {
      const id = hex(cryptoImpl.getRandomValues(new Uint8Array(16)));
      const secret = btoa(String.fromCharCode(...cryptoImpl.getRandomValues(new Uint8Array(32))))
        .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
      const token = `ql_${id}.${secret}`;
      const result = await dispatch(tokenHash, method, path, query, {
        name: payload.name, role: payload.role, id,
        token_hash: await digest(new TextEncoder().encode(token)),
      });
      if (result.status >= 200 && result.status < 300) result.body.token = token;
      return dispatchResponse(result);
    }

    if (method === "POST" && ["/api/receipts", "/api/receipts/import"].includes(path)) {
      const { bytes, extension } = decodeImage(payload);
      const sha256 = await digest(bytes);
      if (path === "/api/receipts/import") {
        if (!RECEIPT_NAME.test(payload.filename) || !payload.filename.endsWith(`.${extension}`) || payload.sha256 !== sha256) {
          fail(400, "invalid_request", "小票文件名、类型或 SHA-256 校验不一致");
        }
        const created = await storeImage(payload.filename, bytes, extension, true);
        return jsonResponse({ filename: payload.filename, sha256, size: bytes.length, uploaded: created }, created ? 201 : 200);
      }
      const filename = `${hex(cryptoImpl.getRandomValues(new Uint8Array(16)))}.${extension}`;
      await storeImage(filename, bytes, extension, false);
      let result;
      try {
        result = await dispatch(tokenHash, "POST", "/api/receipts", {}, { filename, sha256, size: bytes.length });
      } catch (error) {
        // A transport failure can happen AFTER SQL commits. Preserve the object;
        // deleting it could break a committed receipt. An orphan is recoverable.
        throw error;
      }
      if (result.status >= 400) await removeNewImage(filename);
      return dispatchResponse(result);
    }

    if (READ_METHODS.has(method) && path.startsWith("/api/receipts/")) {
      const result = await dispatch(tokenHash, "GET", path, query);
      if (result.status !== 200) return dispatchResponse(result);
      const receipt = result.body.receipt;
      const filename = path.slice("/api/receipts/".length);
      if (!object(receipt) || receipt.filename !== filename || !/^[a-f0-9]{64}$/.test(receipt.sha256) ||
          !Number.isInteger(receipt.size) || receipt.size < 1 || receipt.size > MAX_IMAGE) {
        fail(502, "storage_error", "小票元数据无效");
      }
      const bytes = await readStored(filename);
      if (bytes.length !== receipt.size || await digest(bytes) !== receipt.sha256 ||
          !filename.endsWith(`.${imageExtension(bytes)}`)) fail(502, "storage_error", "小票完整性校验失败");
      return new Response(bytes, { headers: {
        ...SECURITY_HEADERS, "Content-Type": MIME[filename.split(".")[1]],
        "Content-Length": String(bytes.length), "Content-Disposition": `inline; filename="${filename}"`,
      } });
    }
    const result = await dispatch(tokenHash, method === "HEAD" ? "GET" : method, path, query, payload);
    return dispatchResponse(result, path === "/api/export" && result.status === 200 ? {
      "Content-Disposition": 'attachment; filename="acost.json"',
    } : {});
  }

  return async (request) => {
    let response;
    try { response = await handle(request); }
    catch (error) {
      const known = error instanceof ApiError;
      const status = known ? error.status : 503;
      response = jsonResponse({ error: known ? error.code : "internal_error",
        message: known ? error.message : "云端暂时无法处理请求" }, status,
        status === 401 ? { "WWW-Authenticate": "Bearer" } : {});
    }
    return request.method === "HEAD" ? new Response(null, { status: response.status, headers: response.headers }) : response;
  };
}
