# Stockage fichiers ou PostGIS (D3-D4)

GEONAF lit ses territoires, ses acquisitions et ses résultats d'indicateurs dans l'un de deux
stockages. Le mode **fichiers** reste le mode par défaut tant que PostGIS n'est pas validé sur
`geonaf-test`.

## 1. Choisir le mode

| Variable | Valeurs | Défaut |
|---|---|---|
| `GEONAF_DB_BACKEND` | `file` ou `postgres` | `file` |
| `GEONAF_DATABASE_URL` | `postgresql+psycopg://<rôle>:<mot de passe>@<hôte>:<port>/<base>` | aucune (mode `postgres` seulement) |
| `GEONAF_DB_ENV` | `dev` ou `test` | aucune (mode `postgres` seulement) |

Les secrets restent hors dépôt : variables d'environnement, ou secrets Streamlit Cloud. Une URL vers un
hôte distant exige TLS (`sslmode=require` par défaut). Le mode `postgres` exige aussi
`requirements-db.txt` (psycopg 3).

- **Mode fichiers.** Le comportement actuel est inchangé : `data/territories`, les manifestes stricts
  de `data/raw/sentinel2`, les résultats de `data/indicators`. Aucun pilote de base n'est chargé, ce
  que vérifie un test.
- **Mode PostGIS.** Les mêmes lectures passent par le schéma `geonaf` (migrations 0001 et 0002).
  Chaque lecture est une transaction protégée (`src/data/postgis_session.py`) :
  1. `READ ONLY`, avec un délai maximal de 15 s par requête ;
  2. vérification de la marque `geonaf-environment:<env>` du schéma, qui doit correspondre à
     `GEONAF_DB_ENV`, avant toute lecture ;
  3. chemin R5 bis `pg_catalog, <schéma de PostGIS>, pg_temp`, fixé localement à la transaction ;
  4. annulation et fermeture à la sortie.

  L'application n'écrit jamais dans la base. En mode PostGIS, l'import de zone et le chargement de
  référentiel sont désactivés avec un message explicatif.

**Aucun repli silencieux.** Si PostGIS est choisi mais inaccessible, la page affiche « Base PostGIS
indisponible (<type d'erreur>) ». Elle n'affiche pas de données fichiers à la place : cela tromperait
sur ce qui est montré. Le message ne contient ni hôte, ni utilisateur, ni mot de passe, car l'hôte et
l'utilisateur Supabase identifient le projet. Le détail reste dans le journal serveur masqué.

## 2. Architecture

```
src/data/storage.py            open_store(data_dir) -> DataStore(mode, territories, acquisitions, indicators)
  mode file      TerritoryRegistry        FileAcquisitionCatalog      IndicatorResultStore
  mode postgres  PostgisTerritoryRegistry PostgisAcquisitionCatalog   PostgisIndicatorStore
src/data/postgis_session.py    transactions protégées (R5 bis, marque, lecture seule, délai)
src/data/observations.py       manifestes -> AcquisitionRecord ; indicateurs -> documents, empreintes, stockage
src/data/postgis_observations.py  import idempotent et lecture (migration 0002)
src/components/data_source.py  active_store() : seul point d'entrée de l'interface (test d'architecture)
```

Les lecteurs des deux modes ont les mêmes méthodes : `territories`, `versions`, `get`, `geometry`,
`intersecting` ; `scan`, `latest`, `intersecting` ; `results`. En mode PostGIS, chaque ligne est
reconstruite depuis son document d'origine et son empreinte est recalculée. Une ligne modifiée hors
de GEONAF est refusée.

Dans l'interface, le catalogue de territoires obtient la géométrie par `registry.geometry(record)`.
Le contrôle porte sur l'empreinte du fichier en mode fichiers et sur l'empreinte de géométrie dans les
deux modes. Les clés de cache utilisent `record.file_sha256`, identique à l'empreinte du fichier
vérifiée à l'écriture. Elles sont donc identiques à celles d'avant D4, et le mode PostGIS n'a besoin
d'aucun fichier de géométrie.

## 3. Données de D3 (migration 0002)

| Table | Contenu | Identité |
|---|---|---|
| `data_imports` | Journal : périmètre, libellé, version du code, comptes, empreinte agrégée ; clôturé une seule fois | `import_id` |
| `acquisitions` | Produit satellite et date d'acquisition | (`collection`, `product_id`) |
| `acquisition_manifests` | Une ligne par ingestion : date d'ingestion, source STAC, grille, emprise, document complet | SHA-256 du manifeste canonique |
| `indicator_definitions` | Identifiant, libellé, unité, méthode, nature, statut scientifique, empreinte du profil | SHA-256 de la définition |
| `indicator_results` | Valeur, statut, dates, source, couverture, fiabilité, date de calcul, version du code, document | SHA-256 (document de l'indicateur + géométrie) |

Règles :

- **Dates distinctes.** Date d'acquisition, date d'ingestion et date de calcul ne sont jamais
  fusionnées. Une contrainte refuse une ingestion antérieure à l'acquisition, et un déclencheur
  impose qu'un manifeste reprenne exactement la date d'acquisition de son produit.
- **Emprise.** C'est la grille raster de la bande rouge reprojetée en WGS84 (bords densifiés). Ce
  n'est **pas** l'emprise des pixels valides : une acquisition listée peut ne pas couvrir la zone.
- **Rattachement géométrique.** Un résultat est rattaché à la **géométrie normalisée** sur laquelle il
  a été calculé (clé étrangère vers `geometries`), jamais à un territoire ni à une collectivité. À
  l'enregistrement, les octets du fichier de zone doivent être ceux que l'indicateur désigne
  (`aoi_sha256`). La page Territoires ne montre que les résultats de la géométrie exacte du
  territoire choisi.
- **Pas de doublon.** L'empreinte d'un résultat exclut sa date de calcul : un recalcul identique
  n'est pas un nouveau résultat, et le premier enregistrement est conservé.
- **Valeurs finies.** NaN et infinis sont refusés en Python et en SQL. En PostgreSQL, `NaN = NaN` est
  vrai : la contrainte les nomme donc explicitement.
- **Immuabilité.** Mêmes déclencheurs que 0001 sur toutes les tables, RLS partout, aucun droit pour
  `anon`, `authenticated`, `service_role` ni `PUBLIC`. `geonaf_reader` lit,
  `geonaf_importer` insère.

## 4. Imports (outils manuels, jamais l'application)

```powershell
# Vérification seule (aucune connexion)
python scripts/import_territories_postgis.py --data-dir <data> --source-label "poste local"
python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all
# Base LOCALE de test : transaction annulée (simulation), puis application
$env:GEONAF_DATABASE_URL = (Get-Content "$env:USERPROFILE\.geonaf\importer.url" -Raw).Trim()
python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all
python scripts/import_observations_postgis.py --data-dir <data> --source-label "poste local" --scope all --apply
```

Ordre : territoires d'abord, car les résultats exigent leurs géométries. Le comportement est
identique pour les deux outils :

1. une seule transaction par périmètre, sous verrou consultatif ;
2. ce qui manque est inséré, ce qui est déjà identique est compté ;
3. toute divergence (même identité, autre contenu) arrête tout ;
4. un manifeste refusé arrête tout : aucun import partiel.

Supabase n'est pas joignable par psycopg depuis le poste (ports fermés). L'import vers Supabase passe
donc par des scripts SQL Editor générés, comme pour les territoires (05/06). Pour les observations,
ce générateur reste à écrire : voir le plan de convergence.

## 5. Tests et limites

| Vérifié | Où |
|---|---|
| Choix du mode, configuration refusée, aucun pilote en mode fichiers | `tests/test_storage_d4.py` |
| Transactions protégées : ordre délai, marque puis chemin ; annulation et fermeture ; message sans secret | `tests/test_storage_d4.py` (faux pilote), `tests/test_postgis_storage_integration.py` (serveur réel) |
| Catalogue identique dans les deux modes : clés, libellés, surfaces, géométries, cartes | `tests/test_storage_d4.py` (émulation des requêtes) |
| Manifestes, empreintes, emprise ; documents d'indicateurs, aller-retour exact, absence de doublon, falsification refusée | `tests/test_observations.py` |
| SQL de 0002 sur PostgreSQL 16 : contraintes, déclencheurs, vue, RLS, droits, retour arrière | `tests/test_postgis_storage_integration.py` (substituts explicites pour le seul type `geometry`) |
| Page Territoires : écritures désactivées en mode PostGIS, panneau « enregistré » chargé à la demande | `tests/test_storage_d4.py` (AppTest) |

Points **non vérifiés sur le poste**, faute de PostGIS local :
- migrations 0001 et 0002 réelles et Alembic `upgrade` / `downgrade` ;
- imports réels ;
- parité entre fichiers et PostGIS sur une vraie base ;
- protection R5 bis face à une fonction d'ombrage ;
- utilisation des index GiST.

Les tests correspondants existent et s'exécutent dès qu'un PostgreSQL avec PostGIS est disponible
(`-m postgis`). Rien n'a été exécuté sur Supabase.

Mesures du mode fichiers sur le registre réel (97 territoires, 3 manifestes), avec
`scripts/benchmark_storage_queries.py` :
- liste des territoires : environ 240 ms ;
- catalogue gbHumanitarian ADM2 (48 unités) : environ 250 ms ;
- 48 géométries vérifiées : environ 270 ms ;
- manifestes : environ 7 ms.

Le contrôle d'empreinte de géométrie ajouté par D4 coûte environ 3 ms pour 48 géométries. En mode
PostGIS, chaque lecture ouvre une connexion (pas de pool côté client) : la latence vers Supabase est à
mesurer sur `geonaf-test` avant la bêta.
