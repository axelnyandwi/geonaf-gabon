# Téléchargements multiples Sentinel-2 — lot 2

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

## Usage

Depuis « Catalogue des acquisitions Sentinel-2 » : rechercher, sélectionner une
ou plusieurs acquisitions (16 maximum), ouvrir « Téléchargements sécurisés »,
**estimer** (lecture des seules fiches STAC, aucun transfert de bande), puis
**lancer** explicitement. Le tableau « File de téléchargements » se rafraîchit
chaque seconde ; l’annulation est disponible pendant les opérations.

Un rerun Streamlit, un rechargement du navigateur ou un redémarrage du serveur
ne lance jamais de transfert. Après redémarrage, le journal est relu comme
historique à revérifier : il faut réestimer, puis relancer.

## Architecture

- `src/data/download_control.py` : budgets (stockage, réseau, mémoire, réserve
  disque, 1–2 transferts), annulation, écriture atomique `.part` → nom final.
- `src/data/download_store.py` : staging confiné, verrous OS par produit,
  quarantaine des octets inconnus, validation et publication atomique.
- `src/services/download_plan.py` : plan immuable par identité STAC (B04, B08,
  SCL, XML de calibration, preuve SAFE si nécessaire), tailles et checksums.
- `src/services/download_queue.py` : orchestration en arrière-plan, sans Streamlit.
- `src/components/acquisition_downloads.py` : commandes et suivi Streamlit.

Le transport réutilise `ingestion.ingest_sentinel2_product` en mode `strict`,
avec la politique TLS et de retries existante.

## États et validation scientifique

`completed` n’est attribué qu’après : taille reçue égale à `file:size` STAC ;
checksums STAC de B04, B08 et SCL ; XML de calibration lié soit par son
checksum STAC, soit par une preuve `manifest.safe` vérifiée par checksum ;
manifeste `strict` dont toutes les sources sont `verified` ; relecture SHA-256
complète et chargement de l’acquisition (calibration incluse) par le
consommateur scientifique existant. Sans checksum XML ni preuve SAFE, la
publication est refusée. `skipped` signifie qu’un produit local existant a été
entièrement revalidé ; un produit local invalide n’est jamais écrasé.

Les dates sont distinctes : `acquired_at_utc` (capteur), `ingested_at_utc`
(manifeste), `resolved_at_utc` (plan, dans `download_provenance.json`).

## Stockage local et Windows

Staging : `data/downloads/staging/<sha256(id)[:16]>/<id>/`. Le préfixe court
remplace l’ancien répertoire `<id>/<id>` : avec un dépôt sous OneDrive, l’ancien
chemin temporaire (`…jp2.<uuid>.part.<suffixe s3transfer>`) atteignait
284 caractères, au-delà de MAX_PATH (260) avec `LongPathsEnabled = 0`. Le
transfert échouait alors de façon déterministe après 512 Kio, signalé à tort
comme « Réseau protocol ». Le maximum est maintenant de 236 caractères pour ce
dépôt. L’estimation refuse désormais, avant tout transfert, un chemin
temporaire ou final dépassant 259 caractères. Les erreurs d’écriture locale
(chemin, verrou OneDrive/antivirus, droits) sont signalées comme telles et ne
sont pas retentées comme des erreurs réseau.

Le répertoire de l’ancien layout (`staging/<id>/<id>/manifest.safe`, 68 902
octets) est conservé, non supprimé ; il compte dans le budget de staging.

## Validation du 22 octobre 2026

Commandes :

```powershell
.venv\Scripts\python.exe -m pytest tests/test_downloads.py tests/test_downloads_streamlit.py tests/test_catalogue_streamlit.py tests/test_navigation_streamlit.py -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/frontend/test_ndvi_map.mjs tests/frontend/test_temporal_map.mjs
```

Résultats : 39 tests ciblés réussis ; suite Python 518 réussis, 3 ignorés (SCL
local absent pour la fixture du cache de sélection, préexistant), environ 3 200
avertissements de dépréciation Affine ; 10 tests JavaScript réussis. Les tests
de téléchargement sont hors réseau (transport simulé).

Transfert réel, Chrome, serveur local : Akanda, acquisition
`S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148`. Estimation
227 142 005 octets (216,6 Mio, budget 320 Mio) ; reçu 227 142 005 octets, sans
nouvelle tentative, en 185,4 s (≈ 1,2 Mio/s, débit dépendant du réseau) ; état
`completed`. Revalidation indépendante : B04, B08, SCL conformes aux checksums
SHA3-256 STAC, XML lié par la preuve SAFE, manifeste strict. Ré-estimation :
`skipped`, aucun octet transféré. L’acquisition apparaît dans le catalogue
local de l’accueil.

Non-régression Akanda 15/25 janvier 2025 (profil gabonais expérimental),
après publication de l’acquisition du 20 janvier : empreinte inchangée
`b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41`,
couverture 57,3977831312455 %.

## Limites et risques résiduels

- La latence d’annulation dépend des blocs de 256 Kio et des délais réseau ;
  ce n’est pas un arrêt temps réel.
- Le contrôle de budget de staging parcourt le répertoire à chaque bloc : coût
  négligeable ici, à revoir si le staging contient beaucoup de fichiers.
- La garde de 259 caractères est conservatrice ; elle ne détecte pas
  l’activation éventuelle des chemins longs.
- OneDrive peut verrouiller ou synchroniser les gros fichiers de `data/` ;
  un dossier de données hors OneDrive reste préférable.
- Le rafraîchissement automatique des commandes à la fin d’une opération est
  vérifié dans Chrome ; AppTest ne simule pas un rerun limité au fragment.
- Un seul transfert réel a été effectué ; l’annulation et la reprise sont
  validées uniquement hors réseau.
