# D5 - Lecture HTTPS des acquisitions via une Edge Function

Le stockage fichiers reste le mode par défaut. Le client HTTPS n'est branché que sur le panneau explicite de l'interface (voir « Interface Streamlit ») : ni sur `src/data/storage.py`, ni sur les lecteurs PostGIS. Le mode PostGIS direct (`GEONAF_DB_BACKEND=postgres`) n'est **pas** utilisable depuis le poste Windows, dont les ports 5432 et 6543 sont fermés.

## État de validation (09/11/2026)

| Niveau | Contenu |
|---|---|
| Implémenté dans le code | Edge Function `geonaf-acquisitions` (`index.ts`, `security.mjs`) ; scripts SQL `13_edge_reader_role*.sql` ; outils `edge_role_credential.py` et `check_edge_secrets.mjs` ; client `src/data/edge_acquisitions.py` ; service et composant `supabase_acquisitions.py` ; générateur `scripts/generate_observations_sql.py` |
| Testé automatiquement | Tests Node des fonctions de sécurité et du vérificateur de secrets ; pytest du client, du service, de la page Streamlit (session TLS simulée) et du générateur ; scripts 13 exécutés sur un PostgreSQL 16 local sans PostGIS (tables de substitution, connexion SCRAM réelle) |
| Testé réellement sur Supabase `geonaf-test` (constats de l'exploitant) | Rôle `geonaf_edge_reader` en place (`LOGIN`, sans contournement RLS) ; trois secrets enregistrés ; fonction déployée ; HTTPS : 401 sans jeton, 200 avec jeton (`environment = test`, `count = 4`) |
| Testé réellement dans Streamlit (poste local) | Panneau *Analyses temporelles › Données et acquisitions* : 4 acquisitions affichées après consultation explicite |
| Non encore validé | Tests 405 et 400 et la réponse au mauvais jeton sur la fonction déployée, s'ils n'ont pas été relevés ; journaux de la fonction (seuls des codes) ; comportement de la limite de concurrence sous charge ; secrets Streamlit Cloud ; Deno et typage TypeScript en local ; générateur SQL exécuté avec PostGIS ; scripts sur PostgreSQL 17 en local |

Ces 4 lignes sont des manifestes d'acquisition Sentinel-2, pas des résultats d'indicateurs. Leur coexistence à plusieurs dates (y compris une acquisition ancienne) ne mesure aucun changement territorial : aucune comparaison temporelle n'est faite à partir de ce panneau.

## Pourquoi

Les ports PostgreSQL 5432 et 6543 sont fermés depuis le poste, et la Data API (PostgREST) reste désactivée. Le chemin proposé :

```
GEONAF (poste) --HTTPS + jeton--> Edge Function geonaf-acquisitions --TLS vérifié, port 6543--> Supavisor --> PostgreSQL
```

La fonction tourne chez Supabase. Seule la sortie HTTPS 443 est nécessaire côté poste.

## Rôle dédié `geonaf_edge_reader`

- `geonaf_reader` reste `NOLOGIN`. La ligne 21 de `10_security_precheck` reste donc conforme.
- Un rôle `LOGIN` distinct est nécessaire, car le pooler authentifie un rôle réel : l'utilisateur est `geonaf_edge_reader.<project-ref>` sur le pooler partagé.
- **Aucun héritage.** Une politique RLS `TO geonaf_reader` ne s'applique à un autre rôle que si celui-ci possède les privilèges de `geonaf_reader` : `INHERIT`, et à partir de PostgreSQL 16 l'option `INHERIT` de chaque `GRANT`. Pour ne pas en dépendre, le rôle Edge reçoit ses propres droits et ses propres politiques. Il est `NOINHERIT`, et le script refuse un rôle Edge membre d'un autre rôle.
- **Moindre privilège** : `USAGE` sur `geonaf`, puis `SELECT` sur 10 colonnes seulement :
  - `acquisitions` : `collection`, `product_id`, `acquired_at` ;
  - `acquisition_manifests` : `manifest_sha256`, `collection`, `product_id`, `ingested_at`, `grid_crs`, `grid_resolution_m`, `integrity_mode`.
- **Exclus** : `manifest_document`, `stac_source`, `footprint`, `import_id`, le journal d'import, les indicateurs, les territoires, les fonctions et PostGIS.
- **Barrières secondaires** posées comme défauts du rôle : `CONNECTION LIMIT 5`, `statement_timeout=5s`, `idle_in_transaction_session_timeout=10s`, `default_transaction_read_only=on`. La fonction fixe aussi ces réglages dans chaque transaction.

## Compatibilité avec le pooler transactionnel (Supavisor, port 6543)

| Contrainte du mode transaction | Traitement |
|---|---|
| Pas de requêtes préparées | Aucune requête paramétrée ni nommée : node-postgres utilise le protocole simple (vérifié par test statique) |
| État de session perdu entre transactions | Tous les réglages sont des `SET LOCAL` dans `BEGIN READ ONLY` |
| Utilisateur `<rôle>.<project-ref>` | Accepté par `databaseConfig`, ainsi que `<rôle>` seul pour le pooler dédié |
| Défauts du rôle (`ALTER ROLE ... SET`) | Appliqués à l'ouverture de la connexion serveur. Leur effet derrière Supavisor reste **à vérifier** (étape 6) ; ils ne sont qu'une seconde barrière |
| `CONNECTION LIMIT 5` | Si la taille du pool Supavisor dépasse 5, les connexions en trop échouent en 503. C'est voulu : cette limite plafonne la charge |

## Sécurité de la fonction

- **Authentification** :
  - en-tête `Authorization: Bearer <jeton>` ;
  - jeton serveur d'au moins 32 octets ;
  - comparaison des empreintes SHA-256 en temps constant ;
  - authentification **avant** toute connexion à la base.
- **Passerelle Supabase** : elle vérifie un JWT par défaut. Comme le jeton GEONAF n'est pas un JWT, il faut déployer avec `--no-verify-jwt`. Le jeton GEONAF est alors la seule barrière : à confirmer au déploiement.
- **TLS** :
  - certificat racine Supabase obligatoire (`GEONAF_EDGE_DB_CA_CERT`, PEM public téléchargé dans Database Settings > SSL Configuration) ;
  - `rejectUnauthorized: true`, nom d'hôte vérifié par Node, TLS 1.2 minimum.
- **URL de connexion** : l'URL n'est jamais transmise au pilote. node-postgres laisserait un `?sslmode=` d'URL écraser l'objet `ssl` ; l'URL est donc décomposée et les options sont refusées.
- **Contrôle d'environnement**, après connexion. La fonction vérifie :
  - `current_user = geonaf_edge_reader` ;
  - `rolbypassrls = false` et `rolsuper = false` ;
  - `transaction_read_only = on` ;
  - schéma marqué `geonaf-environment:test`.

  Sinon elle répond 503 `database_identity_mismatch`. `SUPABASE_DB_URL`, qui pointe vers un rôle privilégié, n'est jamais lu.
- **Limites** :
  - connexion 4 s ;
  - `statement_timeout` 5 s, `lock_timeout` 2 s, `query_timeout` client 8 s ;
  - `LIMIT 100` ;
  - une connexion par requête, fermée dans `finally`.
- **Abus et coûts (MVP, sans stockage partagé)**, dans l'ordre d'exécution :
  1. authentification ;
  2. mémo de 60 s de la dernière réponse 200 (`x-geonaf-cache: hit|miss`) ;
  3. au plus 2 requêtes simultanées vers la base par instance (au-delà : 429, `Retry-After: 2`) ;
  4. plafond global de 5 connexions (`CONNECTION LIMIT` du rôle).

  Une rafale avec un jeton valide coûte donc au plus une connexion par minute et par instance. Les appels sans jeton n'atteignent jamais la base, mais restent des invocations facturées : seule la plateforme peut les bloquer.
- **Non couvert** : aucune limite de débit globale par jeton, faute de stockage partagé (une table ou un service externe) ; à prévoir avant toute ouverture au-delà d'un appelant serveur. Le jeton est unique : en cas de fuite, il faut le remplacer dans les deux secrets.

## Droits hérités de `PUBLIC` (non révocables pour ce seul rôle)

PostgreSQL n'a pas de refus par rôle : le rôle Edge reçoit aussi tout ce qui est accordé à `PUBLIC`. Les lignes 30 à 34 de `13_edge_reader_role_verify.sql` rendent cette surface visible :
- ligne 30, bloquante : aucun schéma où le rôle peut créer des objets ;
- ligne 31 : `TEMP` sur la base. Il est bloqué tant que la lecture seule par défaut reste en place, mais quelqu'un qui possède le mot de passe peut la lever (les privilèges refusent quand même toute écriture sur `geonaf`, ce qui est testé) ;
- ligne 32 : schémas utilisables hors `geonaf` ;
- ligne 33 : fonctions `SECURITY DEFINER` exécutables ;
- ligne 34 : expiration du mot de passe.

La lecture des catalogues (`pg_class`, `pg_proc`, `pg_policies`...) reste toujours possible : la structure de `geonaf` n'est pas secrète vis-à-vis de ce rôle.
- **Réponses** : JSON, `no-store`, `nosniff`, `Allow: GET` sur 405, `WWW-Authenticate: Bearer` sur 401. Aucun CORS. Les erreurs ne contiennent ni hôte, ni utilisateur, ni message SQL. Le journal ne garde qu'un code (SQLSTATE ou code TLS).

## Client Python (`src/data/edge_acquisitions.py`)

- **URL acceptée** : `https://<ref>.supabase.co/functions/v1/geonaf-acquisitions` uniquement.
- **Transport** :
  - TLS via `secure_session`, avec le magasin système ou le bundle d'entreprise (`GEONAF_TLS_MODE`) ;
  - HTTP refusé, aucune redirection suivie ;
  - corps limité à 1 Mio.
- **Validation stricte de la réponse** :
  - champs exacts ;
  - SHA-256 en hexadécimal minuscule ;
  - dates avec fuseau, et `ingested_at >= acquired_at` ;
  - résolution > 0, au plus 100 lignes, environnement `test`.
- **Dates** : `acquired_at` (prise de vue) et `ingested_at` (ingestion GEONAF) restent distinctes.

## Interface Streamlit

Le panneau « Acquisitions enregistrées dans Supabase geonaf-test » apparaît dans *Analyses temporelles › Données et acquisitions*, sous le catalogue Copernicus et avec son propre garde d'erreur. Le code suit les couches pages > composants > services > données : `views/analyses_temporelles.py` > `src/components/supabase_acquisitions.py` > `src/services/supabase_acquisitions.py` > `src/data/edge_acquisitions.py`.

- **Aucune requête au chargement.** Le bouton « Consulter Supabase geonaf-test » envoie un seul GET HTTPS. Le résultat reste dans la session du visiteur ; le jeton n'y est jamais stocké.
- **Configuration côté serveur uniquement** : `GEONAF_EDGE_ACQUISITIONS_URL` et `GEONAF_EDGE_TOKEN`, lus dans `st.secrets` (`.streamlit/secrets.toml` en local, ignoré par Git, ou les secrets de l'application en production), sinon dans l'environnement. Les deux valeurs doivent venir de la même source.
  - Sans configuration : un message d'information, et le mode fichiers reste inchangé.
  - Configuration incomplète : un avertissement.
- **Contenu affiché** : produit, collection, date d'acquisition, date d'ingestion, ancienneté comptée depuis l'acquisition, grille, intégrité du manifeste, empreinte abrégée. Les mentions de portée sont toujours présentes : aucune preuve de couverture effective, aucun résultat d'indicateur, aucun changement territorial, aucun rattachement à une collectivité.
- **Erreurs** : `EdgeAcquisitionsError` est une erreur de domaine (`src/components/errors.py`). Les messages sont sobres pour 401, 429, 503, délai dépassé, TLS et JSON non conforme. Le navigateur ne reçoit jamais d'hôte, de code HTTP ni de détail SQL. Le journal serveur garde l'hôte, avec le jeton masqué.

## Procédure (chaque étape ✋ exige votre autorisation)

1. `11_state_diagnostic.sql` : confirmer l'état S5 et la marque `geonaf-environment:test`.
2. ✋ `13_edge_reader_role.sql` **tel quel**. Il s'exécute en simulation et doit finir par `GEONAF: SIMULATION REUSSIE`.
3. ✋ Même script avec `application constant boolean := true;`, puis `13_edge_reader_role_verify.sql` :
   - toutes les lignes `conforme = true` ;
   - ligne 2 (LOGIN) = `false`.
4. **Identifiant, sur le poste** :
   - créer un mot de passe aléatoire de 32 à 128 caractères `[A-Za-z0-9_-]` dans le gestionnaire de mots de passe ;
   - lancer `python scripts/edge_role_credential.py` : la saisie est masquée et l'outil n'affiche que `ALTER ROLE ... PASSWORD 'SCRAM-SHA-256$...'` ;
   - ✋ coller cette instruction dans le SQL Editor. L'historique ne contient alors qu'un vérificateur salé, jamais le mot de passe ;
   - relancer `13_..._verify` : ligne 2 = `true`.
5. ✋ **Secrets de la fonction**, à définir dans le dashboard ou avec `supabase secrets set`, jamais dans Git :
   - `GEONAF_EDGE_TOKEN` : jeton aléatoire d'au moins 32 octets, imprimable, sans espace (43 caractères base64url pour 32 octets aléatoires) ;
   - `GEONAF_EDGE_DATABASE_URL` : `postgresql://geonaf_edge_reader.<ref>:<mot-de-passe>@<hôte-pooler>:6543/postgres`. Aucun paramètre `?...` ni `#...`, base `postgres`, hôte en `.supabase.com` ou `.supabase.co`. Jamais `postgres.<ref>`, que le dashboard propose par défaut ;
   - `GEONAF_EDGE_DB_CA_CERT` : certificat téléchargé dans Database Settings > SSL Configuration. Formes acceptées : le PEM tel quel, ou le PEM aplati sur une ligne (si le champ du dashboard ne conserve pas les retours à la ligne), avec 1 à 4 certificats.

   Avant l'enregistrement, contrôler les trois valeurs avec `node scripts/check_edge_secrets.mjs`. Il lit les variables d'environnement du processus, applique les fonctions de `security.mjs` et n'affiche aucune valeur.
6. ✋ Déploiement, depuis la racine du worktree, avec la CLI épinglée lancée par `npx` (sans installation globale ni Docker) et un jeton d'accès personnel lu en saisie masquée :
   `npx --yes supabase@2.122.0 functions deploy geonaf-acquisitions --project-ref <ref> --no-verify-jwt --use-api`.
   - Ne pas utiliser `supabase link` ni `--debug` : ce dernier peut afficher des en-têtes HTTP.
   - Avant de déployer, noter l'empreinte SHA-256 des deux fichiers, car ils ne sont pas encore versionnés.
   - Révoquer le jeton d'accès après le déploiement.
7. **Vérification HTTPS** depuis le poste :
   - sans jeton → 401 ; avec un mauvais jeton → 401 ;
   - `POST` → 405 ; `?x=1` → 400 ;
   - avec le jeton → 200, `environment: test`, au plus 100 lignes ;
   - en cas de 503, lire le code dans les journaux de la fonction (`28P01` = mot de passe refusé, `SELF_SIGNED_CERT_IN_CHAIN` = mauvais certificat CA, `53300` = limite de connexions atteinte).
8. **Retour arrière** : `13_edge_reader_role_rollback.sql`, d'abord en simulation puis en application. Supprimer ensuite les secrets et la fonction.

## Tests

- `node --test tests/frontend/test_edge_acquisitions_security.mjs` : fonctions de sécurité pures (jeton, URL de connexion, certificat, identité, codes d'erreur).
- `pytest tests/test_edge_acquisitions_d5.py` :
  - vérificateur SCRAM contre l'échange de la RFC 7677 ;
  - client HTTP simulé ;
  - cohérence entre les colonnes accordées et la requête de `index.ts`.
- Avec `GEONAF_TEST_DATABASE_URL` (PostgreSQL local, PostGIS non requis), sur des tables de substitution :
  - scripts 13 envoyés en une requête ;
  - connexion SCRAM réelle ;
  - requêtes extraites de `index.ts` ;
  - refus des colonnes et tables hors périmètre ;
  - RLS par la politique propre du rôle ;
  - retour arrière.
- `pytest tests/test_supabase_acquisitions_streamlit.py` :
  - aucune requête sans clic, ni dans les autres sections ;
  - 4 acquisitions affichées avec leur source et les mentions de portée ;
  - 401, 429, 503, 500, délai dépassé, TLS, JSON invalide ;
  - aucune fuite du jeton (interface, session, journal masqué).
- `pytest tests/test_generate_observations_sql.py` : générateur hors ligne (injection, simulation, ordre des garde-fous, aucun pilote chargé) ; avec une base locale, compilation PL/pgSQL et arrêt au premier garde-fou.

**Non testé localement** :
- Deno, `npm:pg` sous Deno et le typage TypeScript (contrôle de syntaxe Node seulement) ;
- PostgreSQL 17 et PostGIS en local ;
- Supavisor, le certificat du pooler et `--no-verify-jwt`, observés seulement à travers l'appel réel réussi (voir « État de validation »).
