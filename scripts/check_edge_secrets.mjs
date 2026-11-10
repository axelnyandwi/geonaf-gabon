// Check the three Edge Function secrets locally, before saving them in the dashboard (D5, manual tool).
//
// Reads GEONAF_EDGE_TOKEN, GEONAF_EDGE_DATABASE_URL and GEONAF_EDGE_DB_CA_CERT from the environment of this
// process (as the function does) and applies the function's own checks (security.mjs). Never prints a value:
// only conformity, the non-secret parts of the URL (host, user, port, database) and the public facts of the CA
// certificates (subject, issuer, validity, CA flag). No network access.
// Usage: node scripts/check_edge_secrets.mjs   (exit code 0 if the three are conformant)
import { X509Certificate } from 'node:crypto';
import {
  caCertificate, databaseConfig, validTokenConfiguration,
} from '../supabase/functions/geonaf-acquisitions/security.mjs';

const env = process.env;
const lines = [];
let ok = true;
const fail = (name, reason) => { ok = false; lines.push(`${name} : REFUSE (${reason})`); };

const token = env.GEONAF_EDGE_TOKEN;
if (!validTokenConfiguration(token)) {
  fail('GEONAF_EDGE_TOKEN', 'absent ou moins de 32 octets');
} else if (token !== token.trim() || !/^[\x21-\x7e]+$/.test(token) || token.length > 4096) {
  fail('GEONAF_EDGE_TOKEN', 'espaces, caracteres non imprimables ou plus de 4096 caracteres');
} else {
  lines.push(`GEONAF_EDGE_TOKEN : conforme (${new TextEncoder().encode(token).length} octets)`);
}

let database;
try {
  database = databaseConfig(env.GEONAF_EDGE_DATABASE_URL);
  if (token && (database.password === token || env.GEONAF_EDGE_DATABASE_URL.includes(token))) {
    fail('GEONAF_EDGE_DATABASE_URL', 'le jeton et le mot de passe doivent etre differents');
  } else if (database.password.length < 32) {
    fail('GEONAF_EDGE_DATABASE_URL', 'mot de passe de moins de 32 caracteres');
  } else {
    const pooler = database.host.endsWith('.pooler.supabase.com') ? 'pooler partage' : 'pooler dedie';
    lines.push(`GEONAF_EDGE_DATABASE_URL : conforme (utilisateur ${database.user}, hote ${database.host}, `
      + `port ${database.port}, base ${database.database}, ${pooler})`);
    if (pooler === 'pooler partage' && !database.user.includes('.')) {
      fail('GEONAF_EDGE_DATABASE_URL', 'le pooler partage exige geonaf_edge_reader.<project-ref>');
    }
  }
} catch (error) {
  fail('GEONAF_EDGE_DATABASE_URL', error.message);     // Messages of databaseConfig never contain the value.
}

try {
  const pem = caCertificate(env.GEONAF_EDGE_DB_CA_CERT);
  const blocks = pem.match(/-----BEGIN CERTIFICATE-----[^-]+-----END CERTIFICATE-----/g);
  lines.push(`GEONAF_EDGE_DB_CA_CERT : ${blocks.length} certificat(s)`);
  for (const block of blocks) {
    const cert = new X509Certificate(block);
    const expired = new Date(cert.validTo) <= new Date();
    lines.push(`  - sujet ${cert.subject.replace(/\n/g, ', ')} ; emetteur ${cert.issuer.replace(/\n/g, ', ')} ; `
      + `valide jusqu'au ${cert.validTo} ; autorite (CA) ${cert.ca ? 'oui' : 'non'}`);
    if (!cert.ca) fail('GEONAF_EDGE_DB_CA_CERT', 'certificat sans drapeau CA');
    if (expired) fail('GEONAF_EDGE_DB_CA_CERT', 'certificat expire');
  }
} catch (error) {
  fail('GEONAF_EDGE_DB_CA_CERT', error.code === 'ERR_OSSL_PEM_NO_START_LINE' ? 'PEM illisible' : error.message);
}

console.log(lines.join('\n'));
console.log(ok ? 'RESULTAT : les trois secrets sont conformes au code de la fonction.' : 'RESULTAT : NON CONFORME.');
process.exit(ok ? 0 : 1);
