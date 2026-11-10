/** Pure helpers shared by the Edge Function and local tests (node --test, no network). */

// Dedicated LOGIN role, distinct from geonaf_reader (NOLOGIN). It holds its own column grants and RLS
// policies (db/supabase/13_edge_reader_role.sql): no privilege comes from role membership.
export const EDGE_ROLE = 'geonaf_edge_reader';
export const EXPECTED_ENVIRONMENT = 'geonaf-environment:test';
const MAX_CA_BYTES = 16384;

export function validTokenConfiguration(token) {
  return typeof token === 'string' && new TextEncoder().encode(token).length >= 32;
}

export async function authorized(header, expected) {
  if (!validTokenConfiguration(expected)) return false;
  if (typeof header !== 'string' || !header.startsWith('Bearer ')) return false;
  const supplied = header.slice(7);
  if (!supplied || supplied.length > 4096) return false;
  const encoder = new TextEncoder();
  // Both sides are hashed first: the loop below compares two fixed 32-byte digests, so its duration
  // does not depend on the length or content of the supplied token.
  const [a, b] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(supplied)),
    crypto.subtle.digest('SHA-256', encoder.encode(expected)),
  ]);
  const left = new Uint8Array(a);
  const right = new Uint8Array(b);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}

/**
 * Parse the connection URL into explicit client fields. The URL itself is never handed to the driver:
 * node-postgres lets query parameters of a connection string (sslmode...) override the ssl object.
 * Only a Supabase host, the transaction pooler port 6543, database postgres and the dedicated role.
 */
export function databaseConfig(value) {
  if (typeof value !== 'string' || !value) throw new Error('Configuration DB absente');
  const url = new URL(value);
  if (url.protocol !== 'postgresql:' && url.protocol !== 'postgres:') throw new Error('Protocole DB invalide');
  if (!url.hostname || !url.username || !url.password) throw new Error('Identifiants DB incomplets');
  if (url.port !== '6543') throw new Error('Le pooler transactionnel 6543 est obligatoire');
  if (url.search || url.hash) throw new Error('Options URL DB interdites');
  const host = url.hostname.toLowerCase();
  if (!/^[a-z0-9.-]+\.supabase\.(com|co)$/.test(host)) throw new Error('Hôte Supabase attendu');
  if (url.pathname !== '/postgres') throw new Error('Base postgres attendue');
  // Shared pooler: <role>.<project-ref>; dedicated pooler: <role> alone.
  const user = decodeURIComponent(url.username);
  if (!(user === EDGE_ROLE || new RegExp(`^${EDGE_ROLE}\\.[a-z0-9]{1,64}$`).test(user))) {
    throw new Error('Connexion réservée au rôle Edge dédié');
  }
  return { host, port: 6543, user, password: decodeURIComponent(url.password), database: 'postgres' };
}

const PEM_BLOCK = /-----BEGIN CERTIFICATE-----([A-Za-z0-9+/=\s]+?)-----END CERTIFICATE-----/g;
const MAX_CA_CERTIFICATES = 4;

/**
 * Supabase root certificate(s) (public, Database Settings > SSL Configuration). Required: TLS trusts only them.
 * Accepted forms, because a dashboard secret field may not keep line breaks: the PEM file as is, the same PEM
 * flattened on one line (breaks turned into spaces or removed), or the base64 body of one certificate.
 * Only CERTIFICATE blocks (1 to 4), each a base64 DER certificate ("MII..."); anything else, a private key
 * included, is refused. Returns a normalized PEM (64-character lines) for the TLS client.
 */
export function caCertificate(value) {
  if (typeof value !== 'string') throw new Error('Certificat CA absent');
  const text = value.trim();
  if (!text) throw new Error('Certificat CA absent');
  if (new TextEncoder().encode(text).length > MAX_CA_BYTES) throw new Error('Certificat CA trop long');
  let bodies;
  if (text.startsWith('-----BEGIN')) {
    bodies = [...text.matchAll(PEM_BLOCK)].map((match) => match[1].replace(/\s+/g, ''));
    if (!bodies.length || text.replace(PEM_BLOCK, '').trim() !== '') throw new Error('Certificat CA PEM invalide');
  } else {
    bodies = [text];
  }
  if (bodies.length > MAX_CA_CERTIFICATES) throw new Error('Trop de certificats CA');
  for (const body of bodies) {
    if (!/^MII[A-Za-z0-9+/]+={0,2}$/.test(body) || body.length % 4 !== 0) {
      throw new Error('Certificat CA PEM invalide');
    }
  }
  return bodies.map((body) => `-----BEGIN CERTIFICATE-----\n${body.match(/.{1,64}/g).join('\n')}\n-----END CERTIFICATE-----`)
    .join('\n');
}

/** The session must be the dedicated role, without RLS bypass, read only, on the marked test project. */
export function identityAccepted(row) {
  return Boolean(row) && row.role_name === EDGE_ROLE && row.bypass === false && row.superuser === false &&
    row.read_only === 'on' && row.environment === EXPECTED_ENVIRONMENT;
}

/**
 * Per-isolate abuse guard (MVP, no shared store): at most maxInFlight database requests at once, and a short
 * memo of the last successful body. Acquisitions change at most daily, so 60 s of staleness is harmless and a
 * burst of authenticated calls costs one database connection per minute and per isolate. The global bound
 * across isolates is the role's CONNECTION LIMIT (db/supabase/13_edge_reader_role.sql).
 */
export function createGate({ maxInFlight = 2, ttlMs = 60000, now = () => Date.now() } = {}) {
  let inFlight = 0;
  let memo = null;
  return {
    cached() {
      return memo && now() - memo.at >= 0 && now() - memo.at < ttlMs ? memo : null;
    },
    tryEnter() {
      if (inFlight >= maxInFlight) return false;
      inFlight += 1;
      return true;
    },
    leave() {
      if (inFlight > 0) inFlight -= 1;
    },
    store(body) {
      memo = { at: now(), body: JSON.stringify(body) };
    },
    get inFlight() {
      return inFlight;
    },
  };
}

/** SQLSTATE or TLS/Node error code only, never the message (it may contain hosts or user names). */
export function safeErrorCode(error) {
  const code = error && typeof error === 'object' ? error.code : undefined;
  return typeof code === 'string' && /^[A-Z0-9_]{1,64}$/.test(code) ? code : 'UNKNOWN';
}
