// Pure security helpers of the geonaf-acquisitions Edge Function. No network, no secret: every
// value below is a dummy built for the test. Run: node --test tests/frontend/
import test from "node:test";
import assert from "node:assert/strict";
import {
  EDGE_ROLE, authorized, caCertificate, databaseConfig, identityAccepted, safeErrorCode,
  validTokenConfiguration,
} from "../../supabase/functions/geonaf-acquisitions/security.mjs";
import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { fileURLToPath } from "node:url";
import { TEST_CA, TEST_CA_2, TEST_LEAF } from "./edge_test_certificates.mjs";

const TOKEN = "t".repeat(40);
const POOLER = "aws-0-eu-west-3.pooler.supabase.com";
const url = (user, extra = "") => `postgresql://${user}:p%40ss@${POOLER}:6543/postgres${extra}`;

test("token configuration requires at least 32 bytes", () => {
  assert.equal(validTokenConfiguration(undefined), false);
  assert.equal(validTokenConfiguration("x".repeat(31)), false);
  assert.equal(validTokenConfiguration("x".repeat(32)), true);
});

test("bearer authorization accepts only the exact token", async () => {
  assert.equal(await authorized(`Bearer ${TOKEN}`, TOKEN), true);
  for (const header of [null, "", TOKEN, `bearer ${TOKEN}`, `Bearer ${TOKEN}x`, `Bearer ${TOKEN.slice(1)}`,
    "Bearer ", `Bearer ${"a".repeat(5000)}`]) {
    assert.equal(await authorized(header, TOKEN), false, String(header).slice(0, 20));
  }
  assert.equal(await authorized(`Bearer ${"s".repeat(10)}`, "s".repeat(10)), false, "weak server token");
});

test("database config accepts the dedicated role on the transaction pooler", () => {
  const shared = databaseConfig(url(`${EDGE_ROLE}.abcdefghijklmnopqrst`));
  assert.deepEqual(shared, { host: POOLER, port: 6543, user: `${EDGE_ROLE}.abcdefghijklmnopqrst`,
    password: "p@ss", database: "postgres" });
  assert.equal(databaseConfig(url(EDGE_ROLE)).user, EDGE_ROLE);
});

test("database config refuses other roles, ports, hosts and URL options", () => {
  const refused = [
    undefined, "", "not a url",
    url("geonaf_reader.abcdefghijklmnopqrst"),            // the NOLOGIN reader is not the Edge role
    url("postgres.abcdefghijklmnopqrst"),                  // privileged platform role
    url(`${EDGE_ROLE}x.abcdefghijklmnopqrst`),
    url(`${EDGE_ROLE}.ABC`),
    url(EDGE_ROLE, "?sslmode=disable"),                   // would override TLS in node-postgres
    url(EDGE_ROLE, "#x"),
    `postgresql://${EDGE_ROLE}:p@${POOLER}:5432/postgres`,
    `postgresql://${EDGE_ROLE}:p@${POOLER}/postgres`,
    `postgresql://${EDGE_ROLE}:p@evil.example.com:6543/postgres`,
    `postgresql://${EDGE_ROLE}:p@supabase.com.evil.example:6543/postgres`,
    `postgresql://${EDGE_ROLE}:p@10.0.0.1:6543/postgres`,
    `postgresql://${EDGE_ROLE}:p@${POOLER}:6543/other`,
    `postgresql://${EDGE_ROLE}@${POOLER}:6543/postgres`,
    `mysql://${EDGE_ROLE}:p@${POOLER}:6543/postgres`,
  ];
  for (const value of refused) assert.throws(() => databaseConfig(value), undefined, String(value));
});

test("database config errors never echo the password", () => {
  try {
    databaseConfig(`postgresql://geonaf_reader:SECRET-VALUE@${POOLER}:6543/postgres`);
    assert.fail("expected refusal");
  } catch (error) {
    assert.doesNotMatch(String(error.message), /SECRET-VALUE|pooler/);
  }
});

const body = (pem) => pem.replace(/-----(BEGIN|END) CERTIFICATE-----/g, "").replace(/\s+/g, "");
const fingerprint = (pem) => new X509Certificate(pem).fingerprint256;

test("CA certificate: file, one-line and base64 forms give the same certificate", () => {
  const expected = fingerprint(TEST_CA);
  for (const form of [TEST_CA, `\n${TEST_CA}\n`, TEST_CA.replaceAll("\n", "\r\n"),
    TEST_CA.replace(/\r?\n/g, " "),                                   // dashboard turning breaks into spaces
    TEST_CA.replace(/\r?\n/g, ""),                                    // ...or removing them
    body(TEST_CA)]) {                                                 // base64 body alone
    const pem = caCertificate(form);
    assert.match(pem, /^-----BEGIN CERTIFICATE-----\n([A-Za-z0-9+/=]{1,64}\n)+-----END CERTIFICATE-----$/);
    assert.equal(fingerprint(pem), expected);
  }
});

test("CA certificate: bundles of up to 4 certificates are kept whole", () => {
  const pem = caCertificate(`${TEST_CA}\n${TEST_CA_2}`.replace(/\r?\n/g, " "));
  const blocks = pem.match(/-----BEGIN CERTIFICATE-----[^-]+-----END CERTIFICATE-----/g);
  assert.deepEqual(blocks.map(fingerprint), [fingerprint(TEST_CA), fingerprint(TEST_CA_2)]);
  assert.throws(() => caCertificate(Array(5).fill(TEST_CA).join("\n")), /Trop/);
});

test("CA certificate: anything but certificates is refused", () => {
  // Dummy, truncated block; the marker is split so that secret scanners do not report a false key.
  const marker = ["PRIVATE", "KEY"].join(" ");
  const key = `-----BEGIN ${marker}-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END ${marker}-----`;
  for (const value of [undefined, "", "   ", "QUJDRA==", key, `${TEST_CA}\n${key}`, `${TEST_CA}\nextra`,
    `garbage ${TEST_CA}`, TEST_CA.replace("BEGIN CERTIFICATE", "BEGIN TRUSTED CERTIFICATE"),
    `-----BEGIN CERTIFICATE-----\nQUJDRA==\n-----END CERTIFICATE-----`,          // not a DER certificate
    `${body(TEST_CA)}A`,                                                          // broken base64 length
    `-----BEGIN CERTIFICATE-----\nMII${"A".repeat(17000)}\n-----END CERTIFICATE-----`]) {
    assert.throws(() => caCertificate(value), undefined, String(value).slice(0, 30));
  }
});

// --- scripts/check_edge_secrets.mjs: validates the three values without ever printing them --------------

const CHECKER = fileURLToPath(new URL("../../scripts/check_edge_secrets.mjs", import.meta.url));
const SECRET_PASSWORD = "Pw_" + "q".repeat(37);
const SECRET_TOKEN = "Tk-" + "z".repeat(40);
const goodUrl = `postgresql://${EDGE_ROLE}.abcdefghijklmnopqrst:${SECRET_PASSWORD}@${POOLER}:6543/postgres`;

function check(values) {
  const env = { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, ...values };
  const run = spawnSync(process.execPath, [CHECKER], { env, encoding: "utf8", timeout: 30000 });
  return { status: run.status, out: run.stdout + run.stderr };
}

test("secret checker accepts conformant values and prints no secret", () => {
  const { status, out } = check({ GEONAF_EDGE_TOKEN: SECRET_TOKEN, GEONAF_EDGE_DATABASE_URL: goodUrl,
    GEONAF_EDGE_DB_CA_CERT: TEST_CA.replace(/\r?\n/g, " ") });
  assert.equal(status, 0, out);
  assert.match(out, /RESULTAT : les trois secrets sont conformes/);
  assert.match(out, new RegExp(`utilisateur ${EDGE_ROLE}\\.abcdefghijklmnopqrst, hote ${POOLER}, port 6543`));
  assert.match(out, /autorite \(CA\) oui/);
  for (const secret of [SECRET_TOKEN, SECRET_PASSWORD, body(TEST_CA).slice(10, 60)]) {
    assert.equal(out.includes(secret), false);
  }
});

test("secret checker refuses each defect without printing values", () => {
  const cases = [
    [{ GEONAF_EDGE_TOKEN: "short" }, /GEONAF_EDGE_TOKEN : REFUSE/],
    [{ GEONAF_EDGE_DATABASE_URL: goodUrl.replace(`${EDGE_ROLE}.`, "postgres.") }, /REFUSE \(Connexion réservée au rôle Edge dédié\)/],
    [{ GEONAF_EDGE_DATABASE_URL: goodUrl.replace(":6543", ":5432") }, /6543/],
    [{ GEONAF_EDGE_DATABASE_URL: goodUrl.replace(`${EDGE_ROLE}.abcdefghijklmnopqrst`, EDGE_ROLE) },
      /pooler partage exige/],
    [{ GEONAF_EDGE_DATABASE_URL: goodUrl.replace(SECRET_PASSWORD, "short") }, /moins de 32/],
    [{ GEONAF_EDGE_DATABASE_URL: goodUrl.replace(SECRET_PASSWORD, SECRET_TOKEN) }, /differents/],
    [{ GEONAF_EDGE_DB_CA_CERT: TEST_LEAF }, /sans drapeau CA/],
    [{ GEONAF_EDGE_DB_CA_CERT: undefined }, /GEONAF_EDGE_DB_CA_CERT : REFUSE/],
  ];
  for (const [change, expected] of cases) {
    const values = { GEONAF_EDGE_TOKEN: SECRET_TOKEN, GEONAF_EDGE_DATABASE_URL: goodUrl,
      GEONAF_EDGE_DB_CA_CERT: TEST_CA, ...change };
    for (const key of Object.keys(values)) if (values[key] === undefined) delete values[key];
    const { status, out } = check(values);
    assert.equal(status, 1, out);
    assert.match(out, expected);
    assert.match(out, /NON CONFORME/);
    for (const secret of [SECRET_TOKEN, SECRET_PASSWORD]) assert.equal(out.includes(secret), false, out);
  }
});

test("identity check requires role, no RLS bypass, read only and the test mark", () => {
  const good = { role_name: EDGE_ROLE, bypass: false, superuser: false, read_only: "on",
    environment: "geonaf-environment:test" };
  assert.equal(identityAccepted(good), true);
  for (const change of [{ role_name: "geonaf_reader" }, { role_name: "postgres" }, { bypass: true },
    { bypass: null }, { superuser: true }, { read_only: "off" }, { environment: "geonaf-environment:dev" },
    { environment: null }]) {
    assert.equal(identityAccepted({ ...good, ...change }), false, JSON.stringify(change));
  }
  assert.equal(identityAccepted(undefined), false);
});

test("safe error code keeps codes only", () => {
  assert.equal(safeErrorCode({ code: "28P01", message: "password authentication failed for user x" }), "28P01");
  assert.equal(safeErrorCode({ code: "SELF_SIGNED_CERT_IN_CHAIN" }), "SELF_SIGNED_CERT_IN_CHAIN");
  assert.equal(safeErrorCode({ code: "host db.x.supabase.co" }), "UNKNOWN");
  assert.equal(safeErrorCode(new Error("db.x.supabase.co")), "UNKNOWN");
  assert.equal(safeErrorCode(null), "UNKNOWN");
});

test("gate bounds concurrency and releases slots", async () => {
  const { createGate } = await import("../../supabase/functions/geonaf-acquisitions/security.mjs");
  const gate = createGate({ maxInFlight: 2 });
  assert.equal(gate.tryEnter(), true);
  assert.equal(gate.tryEnter(), true);
  assert.equal(gate.tryEnter(), false, "third concurrent request refused (429)");
  gate.leave();
  assert.equal(gate.tryEnter(), true);
  gate.leave(); gate.leave(); gate.leave();
  assert.equal(gate.inFlight, 0, "never negative");
});

test("gate memo serves the last success for ttl only, as an immutable string", async () => {
  const { createGate } = await import("../../supabase/functions/geonaf-acquisitions/security.mjs");
  let clock = 1000;
  const gate = createGate({ ttlMs: 60000, now: () => clock });
  assert.equal(gate.cached(), null);
  const body = { environment: "test", count: 1, acquisitions: [{ product_id: "S2_A" }] };
  gate.store(body);
  body.acquisitions[0].product_id = "mutated";
  assert.equal(JSON.parse(gate.cached().body).acquisitions[0].product_id, "S2_A");
  clock += 59999;
  assert.notEqual(gate.cached(), null);
  clock += 1;
  assert.equal(gate.cached(), null, "expired after ttl");
  gate.store(body);
  clock -= 10;
  assert.equal(gate.cached(), null, "clock going backwards never serves a memo");
});
