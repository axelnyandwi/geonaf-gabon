# Mode démonstration en lecture seule (C6a-bis)

Mis en place le 02/11/2026 à partir de l'audit C6a (risque R1). Aucun algorithme scientifique ni
le paquet v3 ne sont modifiés.

## Pourquoi

Sur un déploiement public, toutes les sessions partagent un seul dossier de données : le paquet de
démonstration installé. Sans ce mode, un visiteur peut y télécharger des acquisitions, récupérer
des couches externes, charger des référentiels ou enregistrer une zone : ces écritures sont vues
par tous les visiteurs et rendent le paquet invalide (`verify_pack` refuse tout fichier non
déclaré ; il est alors retéléchargé au redémarrage suivant).

## Activation

`GEONAF_DEMO_READ_ONLY=1` (variable d'environnement, ou clé racine des secrets Streamlit Cloud).
Désactivé par défaut : le fonctionnement local est inchangé. Vide, `0`, `false`, `no` ou `off`
désactivent le mode ; toute autre valeur l'active (une valeur inattendue est traitée comme
« activé »). La variable est relue à chaque appel.

## Effets

| Autorisé | Refusé (`DemoReadOnlyError`, message public) |
|---|---|
| Consultation du paquet v3, accueil, séries NDVI, multisource, territoires du paquet | Téléchargements Sentinel-2 (estimation, lancement, annulation) |
| Cartes, changements de seuils, analyses L3/L4, chiffres clés (calculs en mémoire) | Récupération WorldCover, OpenStreetMap, Sentinel-1 |
| Recherche de métadonnées dans le catalogue STAC (sans téléchargement ni écriture) | Chargement geoBoundaries |
| Chargement et vérification initiale du paquet v3 (`src/data/demo_pack.py`) | Import de zone GeoJSON, enregistrement dans le registre des territoires |

Les refus sont posés à trois niveaux (`src/demo_mode.py`) :

1. Interface : les commandes interdites sont remplacées par un message ; aucun fichier n'est reçu
   (pas de champ d'import) et aucune file de téléchargement n'est créée.
2. Services appelés par l'interface, avant tout accès réseau : `external_retrieval.retrieve_*`,
   `territory_catalog.load_referential` / `import_custom_zone`, `DownloadQueue` (création,
   `prepare`, `start`, `cancel`), et les fonctions de récupération des modules `src/data`.
3. Primitives d'écriture des dépôts partagés : `LayerFileStore.temporary` / `publish` (couches
   externes et registre), `TerritoryRegistry.register`, `LocalDownloadStore`, `atomic_json`.

Le chargeur du paquet n'utilise pas ces dépôts : son installation vérifiée reste possible. Les
calculs écrivent seulement des fichiers temporaires dans des dossiers temporaires du système,
supprimés après usage.

## Tests

`tests/test_demo_read_only.py` : appels directs des services et primitives (doubles réseau qui
échouent s'ils sont utilisés, aucun fichier créé), file de téléchargement créée avant
l'activation, 8 fils simultanés, message public, mode désactivé inchangé, et les cinq pages sur une
copie du paquet v3 (fichiers seulement, comme l'archive) : aucune écriture, paquet revérifié,
commandes remplacées par le message.

## Limites

- La recherche de métadonnées STAC reste possible : requêtes réseau bornées depuis le serveur,
  sans écriture.
- Le mode protège les dossiers partagés du processus ; il ne remplace pas un contrôle d'accès.
