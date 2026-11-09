/** GEONAF D5: server-to-server read-only acquisitions over HTTPS, not a public API. */
import pg from 'npm:pg@8.23.1';
import {
  authorized, caCertificate, createGate, databaseConfig, identityAccepted, safeErrorCode, validTokenConfiguration,
} from './security.mjs';

const { Client } = pg;
// One gate per isolate: bounded concurrency and a 60 s memo of the last successful answer (security.mjs).
const gate = createGate({ maxInFlight: 2, ttlMs: 60000 });

function response(status: number, body: Record<string, unknown> | string,
                  extra: Record<string, string> = {}): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
      ...extra,
    },
  });
}

Deno.serve(async (request: Request): Promise<Response> => {
  if (request.method !== 'GET') return response(405, { error: 'method_not_allowed' }, { allow: 'GET' });
  const url = new URL(request.url);
  if (url.pathname.replace(/\/$/, '').split('/').pop() !== 'geonaf-acquisitions' || url.search) {
    return response(400, { error: 'invalid_request' });
  }

  // Authentication happens before any database work: an unauthenticated caller never opens a connection.
  const token = Deno.env.get('GEONAF_EDGE_TOKEN');
  if (!validTokenConfiguration(token)) return response(503, { error: 'not_configured' });
  if (!(await authorized(request.headers.get('authorization'), token))) {
    return response(401, { error: 'unauthorized' }, { 'www-authenticate': 'Bearer' });
  }

  // Served only after authentication; never an error, never older than 60 s.
  const memo = gate.cached();
  if (memo) return response(200, memo.body, { 'x-geonaf-cache': 'hit' });

  let database: ReturnType<typeof databaseConfig>;
  let ca: string;
  try {
    // Never SUPABASE_DB_URL (privileged role injected by the platform): only the dedicated reader.
    database = databaseConfig(Deno.env.get('GEONAF_EDGE_DATABASE_URL'));
    ca = caCertificate(Deno.env.get('GEONAF_EDGE_DB_CA_CERT'));
  } catch {
    return response(503, { error: 'database_not_configured' });
  }

  if (!gate.tryEnter()) return response(429, { error: 'busy' }, { 'retry-after': '2' });
  let client: InstanceType<typeof Client> | undefined;
  let transactionStarted = false;
  try {
    client = new Client({
      ...database,
      // verify-full equivalent: chain checked against the Supabase CA only, host name checked by Node TLS.
      ssl: { ca, rejectUnauthorized: true, minVersion: 'TLSv1.2' },
      connectionTimeoutMillis: 4000,
      query_timeout: 8000,
      application_name: 'geonaf-edge-acquisitions',
    });
    await client.connect();
    // Transaction pooler: settings are LOCAL to this transaction (session state is not kept), and no
    // query uses parameters or a name, so node-postgres never sends a prepared statement.
    await client.query('BEGIN READ ONLY');
    transactionStarted = true;
    await client.query("SET LOCAL statement_timeout = '5s'");
    await client.query("SET LOCAL lock_timeout = '2s'");
    await client.query('SET LOCAL search_path = pg_catalog, extensions, pg_temp');
    const identity = await client.query(
      `SELECT current_user AS role_name, r.rolbypassrls AS bypass, r.rolsuper AS superuser,
              pg_catalog.current_setting('transaction_read_only') AS read_only,
              pg_catalog.obj_description('geonaf'::pg_catalog.regnamespace, 'pg_namespace') AS environment
       FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user`,
    );
    if (!identityAccepted(identity.rows[0])) {
      return response(503, { error: 'database_identity_mismatch' });
    }
    // Explicitly scoped columns, matching the column grants of the role: never raw STAC documents,
    // STAC URLs, import journal or geometries.
    const result = await client.query(`
      SELECT m.manifest_sha256, a.collection, a.product_id, a.acquired_at,
             m.ingested_at, m.grid_crs, m.grid_resolution_m, m.integrity_mode
      FROM geonaf.acquisition_manifests AS m
      JOIN geonaf.acquisitions AS a
        ON a.collection = m.collection AND a.product_id = m.product_id
      ORDER BY a.acquired_at DESC, a.product_id, m.ingested_at DESC
      LIMIT 100
    `);
    const body = { environment: 'test', count: result.rowCount, acquisitions: result.rows };
    gate.store(body);
    return response(200, body, { 'x-geonaf-cache': 'miss' });
  } catch (error) {
    // Only a code (SQLSTATE such as 28P01, or a TLS code): messages may contain hosts or user names.
    console.error(`geonaf-acquisitions database_unavailable code=${safeErrorCode(error)}`);
    return response(503, { error: 'database_unavailable' });
  } finally {
    if (client && transactionStarted) {
      try { await client.query('ROLLBACK'); } catch { /* closing connection */ }
    }
    if (client) {
      try { await client.end(); } catch { /* already closed */ }
    }
    gate.leave();
  }
});
