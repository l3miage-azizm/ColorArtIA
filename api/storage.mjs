/**
 * Private server-to-server bridge to the official Blob SDK.
 *
 * The Python atelier never receives a store-wide Blob credential. This route
 * accepts its own service credential and returns only scoped, short-lived URLs.
 * Mutable heads are read from origin and written with an actual ETag condition.
 */
import { timingSafeEqual } from 'node:crypto';
import { get, head, issueSignedToken, presignUrl, put } from '@vercel/blob';

const MAX_REQUEST_BYTES = 3 * 1024 * 1024;
const MAX_SMALL_BYTES = 2 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;
const URL_LIFETIME_MS = 5 * 60 * 1000;

class RequestError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

function reply(res, status, body) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'private, no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.end(JSON.stringify(body));
}

function authorize(req) {
  const secret = process.env.COLORART_STORAGE_BRIDGE_SECRET;
  if (typeof secret !== 'string' || secret.length < 32) {
    throw new RequestError(503, 'storage_unavailable');
  }
  const authorization = req.headers.authorization;
  const actual = Buffer.from(typeof authorization === 'string' ? authorization : '');
  const expected = Buffer.from(`Bearer ${secret}`);
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw new RequestError(401, 'unauthorized');
  }
}

async function payload(req) {
  const contentType = req.headers['content-type'];
  if (typeof contentType !== 'string' || contentType.split(';', 1)[0].trim().toLowerCase() !== 'application/json') {
    throw new RequestError(415, 'json_required');
  }
  const statedLength = req.headers['content-length'];
  if (statedLength !== undefined) {
    if (typeof statedLength !== 'string' || !/^\d+$/.test(statedLength)) {
      throw new RequestError(400, 'invalid_request');
    }
    if (Number(statedLength) > MAX_REQUEST_BYTES) {
      throw new RequestError(413, 'request_too_large');
    }
  }
  let parsed;
  if (req.body !== undefined) {
    if (Buffer.isBuffer(req.body) || typeof req.body === 'string') {
      const bytes = Buffer.isBuffer(req.body) ? req.body : Buffer.from(req.body);
      if (bytes.length > MAX_REQUEST_BYTES) throw new RequestError(413, 'request_too_large');
      try { parsed = JSON.parse(bytes.toString('utf8')); }
      catch { throw new RequestError(400, 'invalid_json'); }
    } else {
      parsed = req.body;
      let size;
      try { size = Buffer.byteLength(JSON.stringify(parsed)); }
      catch { throw new RequestError(400, 'invalid_json'); }
      if (size > MAX_REQUEST_BYTES) throw new RequestError(413, 'request_too_large');
    }
  } else {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += bytes.length;
      if (size > MAX_REQUEST_BYTES) throw new RequestError(413, 'request_too_large');
      chunks.push(bytes);
    }
    try { parsed = JSON.parse(Buffer.concat(chunks, size).toString('utf8')); }
    catch { throw new RequestError(400, 'invalid_json'); }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new RequestError(400, 'invalid_request');
  }
  return parsed;
}

function pathname(value) {
  // There are no user-supplied names in storage paths: only owner hashes,
  // content hashes, generated IDs and fixed application filenames.
  if (typeof value !== 'string' || value.length > 512
      || !/^owners\/[a-f0-9]{64}\/[A-Za-z0-9_./-]+$/.test(value)
      || value.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new RequestError(400, 'invalid_path');
  }
  return value;
}

function isHead(path) {
  return /^owners\/[a-f0-9]{64}\/head\.json$/.test(path);
}

function condition(data) {
  if (data.createOnly !== true && data.createOnly !== false) {
    throw new RequestError(400, 'conditional_write_required');
  }
  const etag = data.ifMatch;
  if (etag !== undefined && etag !== null
      && (typeof etag !== 'string' || !etag || etag.length > 512 || /[^\x20-\x7e]/.test(etag))) {
    throw new RequestError(400, 'invalid_etag');
  }
  if (data.createOnly && etag) throw new RequestError(400, 'invalid_condition');
  if (!data.createOnly && !etag) throw new RequestError(400, 'conditional_write_required');
  return { allowOverwrite: !data.createOnly, ...(etag ? { ifMatch: etag } : {}) };
}

function mediaType(value) {
  if (typeof value !== 'string' || value.length > 160
      || !/^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+$/.test(value)) {
    throw new RequestError(400, 'invalid_content_type');
  }
  return value;
}

function resultETag(value) {
  if (typeof value !== 'string' || !value || value.length > 512) {
    throw new RequestError(503, 'storage_unavailable');
  }
  return value;
}

function decodeBody(value) {
  if (typeof value !== 'string' || value.length > Math.ceil(MAX_SMALL_BYTES / 3) * 4
      || value.length % 4 !== 0 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    throw new RequestError(400, 'invalid_body');
  }
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length > MAX_SMALL_BYTES) throw new RequestError(413, 'body_too_large');
  if (bytes.toString('base64') !== value) throw new RequestError(400, 'invalid_body');
  return bytes;
}

async function limitedBytes(stream) {
  const chunks = [];
  let size = 0;
  // The official SDK returns a Web ReadableStream. Cancel it as soon as an
  // oversized manifest is detected rather than buffering an unbounded value.
  const reader = stream.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_SMALL_BYTES) {
        await reader.cancel();
        throw new RequestError(503, 'manifest_too_large');
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, size);
}

async function signedGet(path) {
  const metadata = await head(path);
  const validUntil = Date.now() + URL_LIFETIME_MS;
  const token = await issueSignedToken({ pathname: path, operations: ['get'], validUntil });
  const { presignedUrl } = await presignUrl(token, {
    operation: 'get', pathname: path, access: 'private', validUntil,
    ...(isHead(path) ? { useCache: false } : {}),
  });
  return { url: presignedUrl, etag: resultETag(metadata.etag), size: metadata.size };
}

function sameOwner(paths) {
  const owner = paths[0].split('/').slice(0, 2).join('/');
  if (!paths.every(path => path.startsWith(owner + '/'))) {
    throw new RequestError(400, 'mixed_owner_batch');
  }
}

async function mapBounded(values, limit, action) {
  const out = new Array(values.length);
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(limit, values.length) }, async () => {
    while (cursor < values.length) {
      const index = cursor++;
      out[index] = await action(values[index], index);
    }
  }));
  return out;
}

async function signManyGet(data) {
  if (!Array.isArray(data.paths) || data.paths.length < 1 || data.paths.length > 120) {
    throw new RequestError(400, 'invalid_batch');
  }
  const paths = [...new Set(data.paths.map(pathname))];
  sameOwner(paths);
  if (paths.some(isHead)) throw new RequestError(400, 'mutable_head_not_allowed');
  const validUntil = Date.now() + URL_LIFETIME_MS;
  // This wildcard delegation is held only by this authenticated server. The
  // client receives an HMAC bound to each exact path, never the signing key.
  const token = await issueSignedToken({ pathname: '*', operations: ['get'], validUntil });
  const pairs = await Promise.all(paths.map(async path => {
    const { presignedUrl } = await presignUrl(token, {
      operation: 'get', pathname: path, access: 'private', validUntil,
    });
    return [path, presignedUrl];
  }));
  return { urls: Object.fromEntries(pairs), expiresAt: validUntil };
}

async function signManyPut(data) {
  if (!Array.isArray(data.files) || data.files.length < 1 || data.files.length > 8) {
    throw new RequestError(400, 'invalid_batch');
  }
  // Validate every destination before issuing any token.
  const files = data.files.map(file => {
    if (!file || typeof file !== 'object' || Array.isArray(file)) throw new RequestError(400, 'invalid_upload');
    const path = pathname(file.path);
    if (isHead(path) || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > 20 * 1024 * 1024) {
      throw new RequestError(400, 'invalid_upload');
    }
    const contentType = mediaType(file.contentType);
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(contentType)) throw new RequestError(400, 'invalid_content_type');
    return { path, size: file.size, contentType };
  });
  sameOwner(files.map(file => file.path));
  const uploads = await mapBounded(files, 4, async file => {
    const validUntil = Date.now() + URL_LIFETIME_MS;
    const scope = { pathname: file.path, operations: ['put'], validUntil,
      allowedContentTypes: [file.contentType], maximumSizeInBytes: file.size };
    const token = await issueSignedToken(scope);
    const { presignedUrl } = await presignUrl(token, {
      operation: 'put', pathname: file.path, access: 'private', validUntil,
      allowedContentTypes: [file.contentType], maximumSizeInBytes: file.size,
      addRandomSuffix: false, allowOverwrite: false, cacheControlMaxAge: 31536000,
    });
    return { path: file.path, url: presignedUrl, headers: { 'Content-Type': file.contentType } };
  });
  return { uploads };
}

async function operate(data) {
  if (data.op === 'sign_many_get') return signManyGet(data);
  if (data.op === 'sign_many_put') return signManyPut(data);
  const path = pathname(data.path);
  switch (data.op) {
    case 'read': {
      if (!isHead(path)) return signedGet(path);
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const value = await get(path, {
          access: 'private', useCache: false,
          headers: { 'Accept-Encoding': 'identity' },
        });
        if (!value) throw new RequestError(404, 'not_found');
        if (value.statusCode !== 200 || !value.stream || !value.blob) {
          throw new RequestError(503, 'storage_unavailable');
        }
        const bytes = await limitedBytes(value.stream);
        // Delivery can weaken an ETag when compressing JSON. Conditional PUT
        // requires the canonical metadata ETag. Confirm the body still refers
        // to that exact version; never pair old bytes with a newer head token.
        const metadata = await head(path);
        const delivered = resultETag(value.blob.etag).replace(/^W\//, '');
        const canonical = resultETag(metadata.etag);
        if (!canonical.startsWith('W/') && delivered === canonical) {
          return { body: bytes.toString('base64'), etag: canonical };
        }
      }
      throw new RequestError(409, 'head_changed_during_read');
    }
    case 'put': {
      const conditional = condition(data);
      const body = decodeBody(data.body);
      const contentType = mediaType(data.contentType || 'application/octet-stream');
      let value;
      try {
        value = await put(path, body, {
          access: 'private', addRandomSuffix: false, contentType,
          cacheControlMaxAge: isHead(path) ? 60 : 31536000,
          ...conditional,
        });
      } catch (error) {
        // SDK versions report duplicate create-only writes as a generic error.
        // Confirm existence without overwriting; Python verifies immutable bytes
        // or resolves its exact head operation marker before accepting a retry.
        if (data.createOnly) {
          let exists = false;
          try { await head(path); exists = true; } catch {}
          if (exists) throw new RequestError(409, 'already_exists');
        }
        throw error;
      }
      return { etag: resultETag(value.etag) };
    }
    case 'sign_put': {
      const conditional = condition(data);
      if (!Number.isSafeInteger(data.size) || data.size < 1 || data.size > MAX_UPLOAD_BYTES) {
        throw new RequestError(400, 'invalid_size');
      }
      // Heads must use `put`: its response carries the ETag of that exact CAS.
      if (isHead(path)) throw new RequestError(400, 'head_requires_atomic_put');
      const contentType = mediaType(data.contentType || 'application/octet-stream');
      const validUntil = Date.now() + URL_LIFETIME_MS;
      const scope = {
        pathname: path, operations: ['put'], validUntil,
        allowedContentTypes: [contentType], maximumSizeInBytes: data.size,
      };
      const token = await issueSignedToken(scope);
      const { presignedUrl } = await presignUrl(token, {
        operation: 'put', pathname: path, access: 'private', validUntil,
        allowedContentTypes: [contentType], maximumSizeInBytes: data.size,
        addRandomSuffix: false, cacheControlMaxAge: 31536000, ...conditional,
      });
      return { url: presignedUrl, headers: { 'Content-Type': contentType } };
    }
    case 'sign_get': {
      if (data.downloadName !== undefined && data.downloadName !== null
          && (typeof data.downloadName !== 'string' || data.downloadName.length > 180
              || /[\x00-\x1f\x7f/\\]/.test(data.downloadName))) {
        throw new RequestError(400, 'invalid_download_name');
      }
      // The documented presignUrl API does not support a disposition override.
      // Do not append unsigned query parameters to an authenticated URL.
      return signedGet(path);
    }
    default:
      throw new RequestError(400, 'invalid_operation');
  }
}

function errorResponse(error) {
  if (error instanceof RequestError) return [error.status, error.code];
  const status = error?.statusCode ?? error?.status;
  const code = error?.code;
  const names = new Set([error?.name, error?.constructor?.name]);
  if (status === 404 || code === 'blob_not_found' || names.has('BlobNotFoundError')) return [404, 'not_found'];
  if (status === 412 || code === 'precondition_failed' || names.has('BlobPreconditionFailedError')) {
    return [412, 'precondition_failed'];
  }
  if (status === 409 || code === 'blob_already_exists' || names.has('BlobAlreadyExistsError')) {
    return [409, 'already_exists'];
  }
  return [503, 'storage_unavailable'];
}

export default async function storageBridge(req, res) {
  try {
    authorize(req);
    if (req.method !== 'POST') {
      res.setHeader('Allow', 'POST');
      throw new RequestError(405, 'method_not_allowed');
    }
    const data = await payload(req);
    reply(res, 200, await operate(data));
  } catch (error) {
    const [status, code] = errorResponse(error);
    if (status === 401) res.setHeader('WWW-Authenticate', 'Bearer');
    // Never log credentials, request bodies, signing material or signed URLs.
    reply(res, status, { error: code });
  }
}
