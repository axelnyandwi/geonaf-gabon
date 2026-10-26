# Confiance TLS et accès réseau GEONAF — 21 octobre 2026

> **Note du 26/10/2026** : les valeurs Sentinel-2 de ce document (empreinte `b0356911…`, couvertures, pixels comparables) ont été obtenues avant la correction du découpage AOI avec perte. Valeurs corrigées : [lossless_aoi_clip.md](lossless_aoi_clip.md).

## Correction

La confiance TLS est construite explicitement avant les pools et avant l'utilisation des clients réseau. Le mode système est le défaut. Aucun `inject_into_ssl()` n'est appelé, aucune autorité n'est récupérée ou installée par GEONAF et aucune vérification TLS n'est désactivée.

`src/network_config.py` centralise les endpoints et paramètres. `src/data/network.py` fournit les contextes TLS, l'adaptateur Requests, le lecteur STAC, l'adaptation botocore, les erreurs structurées et la politique de reprises. Les modules métier ne créent ni contexte TLS ni client à l'import et ne chargent pas le `.env` à l'import de l'ingestion. Le chargement explicite du `.env` à la création des clients respecte les variables déjà définies dans l'environnement.

Les opérations scientifiques, les tableaux, masques SCL, grilles, pondérations, calibrations et contrôles de checksum ne sont pas modifiés. Aucun changement de Streamlit ou MapLibre.

## Configuration

| Variable | Défaut | Règle |
|---|---|---|
| `GEONAF_TLS_MODE` | `system` | `system` ou `bundle`, aucune option sans vérification |
| `GEONAF_CA_BUNDLE` | absent | Fichier local requis uniquement en mode bundle |
| `GEONAF_STAC_ENDPOINT` | `https://stac.dataspace.copernicus.eu/v1` | HTTPS, hôte STAC officiel, aucun identifiant ni query string |
| `GEONAF_S3_ENDPOINT` | `https://eodata.dataspace.copernicus.eu` | Endpoint général ou `https://eodata.ams.dataspace.copernicus.eu` |
| `GEONAF_NETWORK_MAX_ATTEMPTS` | `3` | Total, première tentative comprise ; de 1 à 10 |
| `GEONAF_NETWORK_BACKOFF_SECONDS` | `1` | Base exponentielle, full jitter |
| `GEONAF_NETWORK_MAX_DELAY_SECONDS` | `30` | Maximum d'une pause ; plafond configurable 300 s |
| `GEONAF_NETWORK_WAIT_BUDGET_SECONDS` | `60` | Somme maximale des pauses ; plafond configurable 600 s |
| `GEONAF_NETWORK_CONNECT_TIMEOUT` | `10` | Timeout de connexion, au plus 120 s |
| `GEONAF_NETWORK_READ_TIMEOUT` | `30` | Timeout de lecture, au plus 120 s |

Les valeurs non finies et les réglages invalides sont refusés sans afficher leur valeur. Les credentials restent `CDSE_S3_ACCESS_KEY` et `CDSE_S3_SECRET_KEY`, hors Git et journaux.

Le défaut S3 centralisé reprend l'endpoint général qui était réellement utilisé. Auparavant, `config.py` déclarait AMS alors que la fabrique codait l'endpoint général en dur. Une sélection explicite AMS reste possible et testée.

Les paramètres GEONAF déterminent la confiance des clients : les variables implicites de bundle propres à Requests/botocore ne sont pas ajoutées silencieusement. Les proxies HTTP(S) et `NO_PROXY` restent lus explicitement, sans affichage de leurs valeurs. La lecture automatique de `.netrc` est désactivée pour STAC.

### Mode système

Les clients utilisent des contextes truststore propres à leurs pools. Sous Windows, truststore utilise la confiance native ; sous Linux, celle d'OpenSSL/système. Les politiques natives de construction de chaîne et de révocation restent celles de l'OS. GEONAF n'ajoute aucune autorité au magasin de confiance.

Si truststore ou celui de pip est déjà injecté par un outil tiers, GEONAF réutilise cette implémentation pour créer ses propres contextes, sans l'injecter à nouveau. Cela évite la récursion rencontrée en imbriquant deux implémentations truststore.

Tous les contextes clients imposent `CERT_REQUIRED`, la vérification du nom d'hôte et TLS 1.2 minimum. La classe globale `ssl.SSLContext` reste inchangée.

### Mode bundle

Le bundle est fourni par l'opérateur/IT, jamais téléchargé automatiquement. Il doit contenir des certificats publics PEM, sans clé privée, et faire au plus 8 Mio. Son contenu est lu de façon bornée puis chargé en mémoire, sans seconde lecture du fichier par SSL. Les certificats du système ne sont pas ajoutés à ce mode strict.

**Limite explicite : le mode bundle refuse une classe SSL globalement remplacée par un outil tiers**, notamment `pip-system-certs`, car celle-ci pourrait élargir silencieusement les autorités approuvées. Utiliser un environnement Python propre pour ce mode. Les tests bundle fonctionnent dans des sous-processus `-S` ; cette option est une méthode de diagnostic et ne remplace pas la préparation d'un environnement de déploiement propre.

Ne pas versionner le bundle opérationnel ni une clé. Aucun certificat serveur reçu sur le réseau ne doit devenir automatiquement une autorité approuvée.

## Transport, reprises et erreurs

STAC utilise un `StacApiIO` dédié. Il autorise seulement les requêtes HTTPS GET et POST vers l'origine configurée. POST est utilisé pour la recherche en lecture seule. Les sources locales, les changements d'origine et les redirections HTTP ne sont pas suivis. La session est fermée après matérialisation des résultats ; une erreur de page ne retourne aucun résultat partiel.

L'endpoint S3 est validé avant l'utilisation des credentials. Signature SigV4 et région `default` sont conservées. Le transport refuse l'envoi vers une origine différente ou vers HTTP.

Les retries automatiques Requests sont désactivés. Botocore est configuré avec `total_max_attempts=1`. Les téléchargements GEONAF utilisent s3transfer classique avec `num_download_attempts=1` et sans threads, afin d'éviter des reprises internes concurrentes ou imbriquées. Une opération GET de métadonnées inclut la lecture et la fermeture du flux dans sa propre politique de reprises ; les vérifications de taille/checksum conservent leur contrat métier.

Une seule couche applicative possède les reprises :

- 429, 500, 502, 503 et 504, timeouts et interruptions de transport sont susceptibles d'être repris.
- Les échecs TLS, proxy, 401 et 403 ne sont pas repris automatiquement.
- Backoff exponentiel avec full jitter ; `Retry-After` accepte secondes ou date HTTP.
- Un délai serveur dépassant le plafond ou le budget termine l'opération : il n'est pas raccourci pour réessayer trop tôt.
- Les budgets s'appliquent par requête STAC/page ou opération de téléchargement, pas à toute une ingestion multi-actifs. Les timeouts connexion/lecture ne constituent pas un délai mural global pour un gros transfert.

`download_asset` conserve les paramètres historiques explicites `max_retries` et `retry_delay` ; le premier désigne le nombre total de tentatives, comme auparavant. Sans override, le défaut devient trois tentatives configurables. Le cache local des fichiers non vides est conservé ; les contrôles d'intégrité de l'ingestion restent inchangés.

Un client botocore externe avec retries SDK multiples est refusé par les helpers de téléchargement, afin de ne pas multiplier les budgets. Utiliser `create_cdse_s3_client`. Les utilisateurs avancés qui appellent directement les méthodes du client boto3 reçoivent toujours le contrat SDK ; ils peuvent utiliser `run_with_retry` pour une opération de lecture dédiée.

Les erreurs publiques portent `service`, `category`, `status_code`, `s3_code` autorisé et `attempts`. `STACSearchError` reste disponible, ainsi que `S3DownloadError`. Le statut pystac-client est récupéré depuis `APIError.status_code`, avec repli sur `response.status_code`.

Les messages originaux, URLs, objets S3, chemins, corps XML/JSON et en-têtes ne sont jamais ajoutés au journal GEONAF. À la création explicite des clients, les journaux bruts des transports tiers sont rendus silencieux ; les événements GEONAF conservés contiennent seulement service, catégorie, statut, code autorisé, tentative et décision de reprise. Ne pas réactiver des traces HTTP/SDK brutes contenant des credentials.

### Dépendance interne botocore

Botocore ne fournit pas d'argument public `SSLContext`. L'intégration est isolée dans `_S3TLS` et `install_s3_tls`, qui remplacent le transport d'un client neuf avant sa première requête. Aucun monkeypatch de classe SDK ou SSL globale. Une API de transport incompatible produit une erreur de configuration ; les tests de transport et les accès réels doivent accompagner les mises à jour du SDK. Version vérifiée : boto3/botocore 1.43.108.

## Validation réelle du 21 octobre 2026

Lectures uniquement : un résultat STAC du 25 janvier 2025 autour d'Akanda, HEAD d'un actif B04, puis 1 Ko de cet actif en mémoire. Aucun raster écrit.

| Environnement | Requests sans correction | STAC GEONAF | S3 HEAD | S3 GET partiel |
|---|---|---|---|---|
| Python habituel, truststore injecté par pip | 200 | 1 résultat, 1,640 s | 200, 1,035 s | 206, 1024 octets |
| Python propre `-S`, endpoint général | SSLError de certificat | 1 résultat, 1,443 s | 200, 1,483 s | 206, 1024 octets |
| Python propre `-S`, endpoint AMS | SSLError de certificat | 1 résultat, 1,373 s | 200, 1,314 s | 206, 1024 octets |

Dans les trois cas, TLS est vérifié, `ssl.SSLContext` globale reste identique et le SDK déclare une seule tentative. Ces durées sont ponctuelles ; il ne s'agit pas d'un benchmark de débit.

Commandes reproductibles :

```powershell
.venv/Scripts/python.exe -B scripts/check_network_live.py
.venv/Scripts/python.exe -B scripts/check_network_live.py --clean-process
```

Sur Linux, utiliser l'exécutable Python de l'environnement virtuel ; le script déduit le répertoire de paquets sans chemin Windows codé en dur. Pour AMS, définir `GEONAF_S3_ENDPOINT` avant la commande, sans modifier les fichiers.

## Tests et limites

`trustme` est ajouté uniquement aux dépendances de développement. Les tests créent leurs autorités/certificats et clés éphémères dans le répertoire temporaire pytest. Aucun certificat ou clé de test n'est installé dans le magasin système ni ajouté au dépôt.

Les deux transports sont testés contre un vrai serveur TLS local en processus propre : certificat valide approuvé, autorité inconnue, nom d'hôte incorrect et certificat expiré. Seul le premier cas est accepté. Les tests vérifient aussi le refus de bundles privés/manquants, la séparation des erreurs, les statuts 403/503, les budgets, jitter, Retry-After, proxies, fermeture des flux, absence de fuites et absence d'injection SSL à l'import.

Commandes :

```powershell
.venv/Scripts/python.exe -B -m pytest -q -rs -p no:cacheprovider
.venv/Scripts/python.exe -B scripts/benchmark_map_layer.py
git diff --check
```

Résultat final de la suite : **358 tests réussis, 3 ignorés, 1100 avertissements** en 48,39 s. Les 78 cas supplémentaires couvrent la correction réseau. Les trois tests ignorés recherchent un SCL local du 15 janvier 2025 que leur motif ne trouve pas ; les données Akanda utilisées pour la non-régression scientifique sont disponibles. Les avertissements sont les dépréciations Affine/Rasterio préexistantes. Aucune mesure chiffrée de couverture de lignes n'a été réalisée.

Le contrôle scientifique réel conserve strictement l'empreinte :

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

L'analyse Akanda, les masques et les indicateurs restent identiques. Le benchmark hors interface émet les avertissements Streamlit attendus lorsqu'aucun runtime Streamlit n'est lancé.

Linux : aucun environnement WSL/Docker disponible sur ce poste. Le code et les tests utilisent des interfaces portables ; l'exécution Linux reste à réaliser. La chaîne d'interception rencontrée ici nécessite également une autorité approuvée dans le système ou bundle Linux : les magasins Windows ne sont pas transférés automatiquement.

Aucun 503 distant n'a été observé : les scénarios transitoires sont contrôlés en tests. Les tests de proxy vérifient les contextes et la configuration sans proxy réel authentifié. Le mode bundle réel CDSE n'a pas été essayé avec un bundle opérationnel IT ; son fonctionnement cryptographique strict est validé sur les deux transports locaux.

Régressions intermédiaires corrigées : récursion lors d'une double implémentation truststore, et assertion de test sur le mauvais emplacement du contexte proxy urllib3. Les validations métier existantes sont conservées.

Aucun commit créé pendant cette intervention. Les modifications préexistantes du dépôt sont préservées.

## Sources des interfaces

- [Truststore : contextes explicites et compatibilité](https://truststore.readthedocs.io/en/latest/)
- [Requests : configuration SSL](https://requests.readthedocs.io/en/stable/user/advanced/)
- [Boto3 : retries](https://docs.aws.amazon.com/boto3/latest/guide/retries.html)
- [Boto3 : transferts S3](https://docs.aws.amazon.com/boto3/latest/reference/customizations/s3.html)
- [CDSE : endpoints S3](https://documentation.dataspace.copernicus.eu/APIs/S3.html)


## Fichiers de cette intervention

- `src/network_config.py` : paramètres communs et validation.
- `src/data/network.py` : TLS, erreurs, reprises et transports.
- `src/data/ingestion.py` : raccordement des clients et métadonnées, sans changement des contrôles scientifiques.
- `src/config.py` : endpoints issus des mêmes paramètres.
- `requirements.txt` : minimum Requests 2.32.3 pour le point d'intégration de l'adaptateur.
- `requirements-dev.txt` : trustme pour les tests TLS locaux.
- `tests/test_network.py` : tests de politique et d'ingestion.
- `tests/test_network_tls.py` : tests cryptographiques locaux.
- `tests/tls_probe_worker.py` : serveur/client de test en processus propre.
- `scripts/check_network_live.py` : diagnostic manuel réel, hors pytest.
- `docs/network_tls.md` : configuration, résultats et limites.

`src/data/ingestion.py` et `requirements.txt` comportaient déjà des modifications avant cette intervention ; elles ont été conservées. Les autres fichiers préexistants modifiés ne font pas partie de cette correction.
