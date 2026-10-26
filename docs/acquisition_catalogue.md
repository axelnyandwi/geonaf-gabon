# Catalogue des acquisitions Sentinel-2 — lot 1

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

## Usage et périmètre

Depuis l’application, ouvrir « Catalogue des acquisitions Sentinel-2 ». Choisir
une AOI GeoJSON configurée, deux dates UTC et éventuellement un plafond de
couverture nuageuse globale. Lancer explicitement la recherche, puis sélectionner
plusieurs acquisitions. Aucun asset satellite n’est téléchargé et aucune paire
scientifique ni série temporelle n’est construite par cette page.

Navigation : lancer `.venv\Scripts\python.exe -m streamlit run app.py` depuis
la racine du dépôt. Le dossier `pages` utilise exclusivement la découverte
multipage automatique ; aucun appel applicatif à `st.navigation` n’est ajouté.
Le catalogue possède un lien « Accueil » vers `app.py`.

Après création du dossier `pages` sur un serveur déjà lancé, **redémarrer le
processus Streamlit**, puis recharger le navigateur. Streamlit 1.65 mémorise
au niveau du processus la présence du dossier lors de sa première session
(`PagesManager.uses_pages_directory`). Un simple rerun ou rechargement du
navigateur ne réinitialise pas cet état : un `st.page_link` vers un fichier
existant mais non enregistré déclenche alors `StreamlitPageNotFoundError`.
Ne pas contourner ce problème en ajoutant un second mécanisme de navigation.
AppTest réinitialise ce drapeau à chaque exécution ; le redémarrage et
l’aller-retour accueil/catalogue doivent donc aussi être vérifiés dans un
navigateur connecté à un vrai serveur.

Vérification du correctif de navigation : Chrome sur le vrai serveur local,
clic accueil → catalogue → accueil et ouverture directe du catalogue dans une
nouvelle session, sans exception ni boucle ; indicateurs Akanda inchangés.
`tests/test_navigation_streamlit.py` vérifie avec le registre Streamlit réel
l’enregistrement depuis `app.py`, les deux liens, les transitions et la
conservation du résultat scientifique sans recalcul. Les tests catalogue
démarrent maintenant depuis cet entrypoint, pas depuis le script de page isolé.
Les neuf tests ciblés passent ; les 82 tests Python concernés (navigation,
catalogue, interface temporelle, profils et sélection) passent, avec 1 841
avertissements de dépréciation Affine. Aucun code scientifique n’est modifié.

Les bornes couvrent les journées UTC, de 00:00:00 à 23:59:59.999999. L’intersection
utilise la géométrie réelle de l’AOI, normalisée en WGS84. La recherche complète
lit toutes les pages ; les identités répétées sont dédupliquées. Une erreur sur
une page ultérieure fait échouer la recherche entière, sans résultat partiel
réutilisable ni ancienne sélection affichée comme actuelle.

## Réemploi et séparation des responsabilités

L’état initial sur `b7f9b26` comportait sept fichiers modifiés et 35 non suivis.
L’inventaire a distingué les travaux d’intégrité d’ingestion (`ingestion.py`,
tests associés), l’ouverture du catalogue local (`local_preview.py`), les
notebooks/documentation et les diagnostics/cartographie/benchmarks existants.
Le calendrier UTC de `observation_selection.py` reste utilisé par l’analyse
scientifique existante. Les scripts exploratoires multi-AOI comportant des
téléchargements ne sont pas appelés par cette nouvelle page.

La suppression locale préexistante du filtre des 15 et 25 janvier est conservée :
le catalogue local expose toutes les observations à manifeste strict, notamment
celle du 3 février 2025. Le test historique conserve toutes ses assertions.

- `src/data/catalogue.py` : contrats immuables, identité `(collection, stac_id)`,
  interface `CatalogueRepository.search` remplaçable par un adaptateur PostGIS.
- `src/data/stac_catalogue.py` : normalisation des métadonnées Copernicus.
- `src/data/ingestion.py` : ajout explicite `complete=True` et `intersects` à la
  recherche existante ; le mode plafonné historique reste le défaut. Le nouveau
  mode couvre aussi les fractions de seconde du dernier jour UTC.
- `src/services/acquisition_catalogue.py` : validation AOI, rapprochement local,
  sélection multiple et projection des seules métadonnées publiques.
- `src/components/acquisition_catalogue.py` et `pages/1_Acquisitions_Sentinel2.py` :
  présentation Streamlit ; aucun calcul métier ne dépend de Streamlit.

Aucune dépendance PostgreSQL/PostGIS ni migration n’est ajoutée. Les données
satellites, scripts antérieurs et modifications locales restent conservés.

## Provenance et interprétation

La sélection conserve la requête, l’AOI et son empreinte, la géométrie WGS84,
la période, le filtre nuageux, la source, la date de recherche UTC et la
complétude de la recherche parente. Cette dernière ne signifie pas que la
sélection contient toutes les acquisitions. Les dates d’acquisition et
d’ingestion sont distinctes.

Un profil V1 est proposé uniquement pour une AOI dont l’identifiant correspond
à celui du profil. Son identifiant, sa version et son empreinte complète font
partie de l’identité de requête. Aucune validité scientifique n’est attribuée
par le catalogue. Les calculs, statuts et contrats de provenance scientifiques
existants restent inchangés.

Le pourcentage nuageux est celui du produit entier ; une valeur absente ou
invalide reste inconnue, jamais zéro. La disponibilité B04/B08/SCL indique un
lien STAC déclaré, sans vérification du contenu ou promesse d’exploitabilité.
La résolution SCL n’est pas modifiée ni présentée comme une précision de 10 m.

Les états locaux distinguent absence, manifeste strict avec fichiers présents,
présence partielle, présence non vérifiée et identité ambiguë. Les manifestes
sont bornés en taille, les chemins confinés et les fichiers contrôlés par
métadonnées ; ni raster ni empreinte volumineuse n’est lu. Une identité issue
d’un manifeste rejeté reste une simple indication non vérifiée. Une validation
complète des fichiers reste obligatoire avant toute analyse scientifique.

## Sécurité, cache et limites

Le transport `SecureStacIO` existant conserve la vérification TLS, la restriction
des hôtes Copernicus, les délais et la politique de retries. Aucun contournement
TLS ni identifiant n’est ajouté. Les erreurs publiques n’exposent ni URL privée,
ni chemin de données, ni corps de réponse. L’interface ne reçoit aucun href.

Les résultats sont conservés dans la session, sans cache STAC global : modifier
AOI, contenu de l’AOI, dates, filtre ou profil invalide leur affichage et la
sélection courante. Une sélection ou actualisation de l’état local ne relance
pas la recherche distante. Un nouveau clic de recherche la relance explicitement.

La recherche complète et le tableau sont en mémoire, avec un coût proportionnel
au nombre d’acquisitions. Les longues périodes/grandes AOI peuvent augmenter
latence réseau, mémoire et coût d’affichage navigateur. La pagination serveur
n’est pas une pagination virtuelle du tableau. L’annulation, la persistance,
la pagination UI et les téléchargements multiples restent hors de ce lot.

## Validation reproductible

Tests automatisés sans réseau :

```powershell
.venv\Scripts\python.exe -B -m pytest tests/test_acquisition_catalogue.py tests/test_catalogue_streamlit.py tests/test_ingestion.py tests/test_observation_selection.py tests/test_network.py tests/test_network_tls.py -q --tb=short -p no:cacheprovider
.venv\Scripts\python.exe -B -m pytest -q --tb=short -rs -p no:cacheprovider
node --test tests/frontend/test_ndvi_map.mjs tests/frontend/test_temporal_map.mjs
git diff --check
```

Les nouveaux tests couvrent pagination/déduplication/échec tardif, transport
sécurisé, période libre, changement AOI/profil, UTC, nuages inconnus, contrats de
sélection/provenance, états locaux, chemins sortants, absence de lecture raster,
sélection multiple, invalidation de session et erreurs d’actualisation locale.

Résultats du lot : 159 tests ciblés réussis ; suite Python complète,
487 réussis et trois ignorés ; dix tests JavaScript réussis. Les 3 002
avertissements Python sont des PendingDeprecationWarning sur les opérations
Affine. Les trois tests ignorés du cache de sélection exigent un SCL local
absent à l’emplacement attendu par leur fixture. Le test du 3 février réussit.
`git diff --check` réussit, avec des avertissements Git LF/CRLF sur deux fichiers
préexistants. Aucun commit ni push n’est réalisé.

Diagnostics manuels, distincts de pytest :

```powershell
.venv\Scripts\python.exe -B scripts/benchmark_acquisition_catalogue.py
.venv\Scripts\python.exe -B scripts/check_acquisition_catalogue_live.py
```

Mesure hors réseau : 10 000 acquisitions synthétiques, trois répétitions,
médiane 348 ms ; pic Python mesuré par tracemalloc environ 10,5 Mio, projection
JSON environ 2,14 Mo, aucune lecture raster. La construction initiale des
enregistrements, le réseau et le rendu navigateur sont exclus de cette mesure.

Contrôle réel du 22 octobre 2026 : Akanda, janvier 2025, pages forcées à deux
éléments, cinq acquisitions uniques sur trois pages, aucun téléchargement
d’asset. Ces résultats dépendent de l’état distant du catalogue.
La dernière exécution a reçu un HTTP 429 puis réussi grâce à la reprise existante.

Non-régression Akanda 15/25 janvier 2025 avec profil expérimental gabonais :
empreinte identique des deux NDVI, delta, masque comparable, classes,
statistiques, surfaces et couverture :
`b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41`.
827 865 pixels comparables, couverture 57,3977831312455 %, delta moyen
−0,03283473837832234. Cette identité prouve la non-régression de cette paire,
pas une validation des seuils expérimentaux ou de toutes les AOI.

Références : [Copernicus STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)
et [API pystac-client](https://pystac-client.readthedocs.io/en/latest/api.html).
