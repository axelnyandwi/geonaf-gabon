# Comparaison temporelle intégrée à Streamlit — étape 4

## Utilisation

```powershell
.venv/Scripts/python.exe -B -m streamlit run app.py --server.address localhost --server.port 8501 --server.headless true --browser.gatherUsageStats false --theme.base dark
```

Ouvrir `http://localhost:8501`. Les acquisitions strictement vérifiées Akanda
des 15 et 25 janvier 2025 sont utilisées, avec l'AOI locale d'acquisition
12 × 12 km, distincte d'une limite administrative. L'option « Comparaison
temporelle interactive » est cochée par défaut. Les trois vues conservent
leurs masques et se synchronisent ; la troisième bascule localement entre
classes et delta continu. La provenance publique se déplie dans le composant.

Décocher cette option retrouve l'ancienne carte NDVI de la date 2 ; décocher
aussi l'ancienne carte retrouve Matplotlib. Une erreur du moteur temporel
déclenche automatiquement Matplotlib et conserve les indicateurs. Après une
erreur, désactiver puis réactiver la comparaison permet de recréer les cartes.

## Intégration et invalidation

`src/components/temporal_map.py` est le seul adaptateur Streamlit du module
temporel autonome. Il charge le moteur et les CSS locaux existants, transmet
uniquement `geonaf.temporal-map/v1`, et appelle `dispose()` au démontage.
Une référence locale du rendu sur son élément parent facilite les tests
navigateur ; elle n'est pas une passerelle scientifique.

Le cache existant `TemporalMapCache` est conservé dans une seule ressource
Streamlit partagée : 4 entrées, 64 Mio de clés et JSON publics sérialisés.
Il n'y a pas de deuxième cache `st.cache_data` de ces charges utiles. Les
réponses sont isolées ; aucun tableau scientifique n'est partagé par ce cache.
`max_size=1536` est transmis explicitement au préparateur. La clé scientifique
reste celle de `local_preview_key` : acquisitions, révisions des fichiers,
manifestes, AOI, SCL et seuil. Le cache ajoute ses paramètres de rendu et
géométrie existants. La clé du composant dépend de `result_id`, sans chemin
privé ; une nouvelle charge utile possède un nouvel état de rendu/erreur.

Le résultat scientifique reste dans la session existante. Les interactions
MapLibre ne déclenchent aucune mise à jour de l'état Streamlit. Seule une
erreur générique remonte via le composant pour activer le repli. La sélection
des dates reste soumise au formulaire. En cas de paire identique ou inversée,
l'application s'arrête sur l'erreur chronologique et retire cartes/indicateurs
anciens. Revenir à la paire valide peut réutiliser le résultat en session.
Changer d'acquisition valide invalide le résultat et les couches ; ce dernier
cas est testé avec une troisième acquisition synthétique, pas avec de nouvelles
données satellites ajoutées au dépôt.

Le moteur scientifique, l'ingestion, les ressources MapLibre, le module JS
temporel et le repli Matplotlib ne sont pas modifiés par cette étape. Les
modifications antérieures de `app.py` et des tests sont conservées.

## Tests et reproduction des mesures

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider
node --test tests/frontend/test_ndvi_map.mjs tests/frontend/test_temporal_map.mjs
```

Résultats : 390 tests Python réussis, 3 ignorés (recherche de fichiers SCL
locaux indisponibles selon leur nommage), 1 579 avertissements Affine/Rasterio.
Les 7 nouveaux tests d'intégration couvrent réutilisation, bornage, isolation,
taille, dates invalides, changement valide d'acquisition, révision AOI, exception
de préparation et replis. Les 9 tests JavaScript existants réussissent.

Pour instrumenter le véritable `app.py`, arrêter son serveur local, puis :

```powershell
.venv/Scripts/python.exe -B scripts/benchmark_temporal_streamlit.py
```

Ce script manuel utilise `psutil`, déjà présent dans l'environnement de test ;
l'application normale et l'adaptateur ne dépendent pas de cet outil. Il enveloppe
les fonctions appelées sans modifier leurs fichiers et écrit uniquement
`outputs/temporal-streamlit/server.json`, ignoré par Git. Il contrôle aussi
l'empreinte scientifique de référence. Aucun endpoint de diagnostic n'est créé.

Dans un contexte Chrome neuf, injecter
`tests/frontend/streamlit_temporal_probe.js` avant la navigation (par exemple
via CDP `Page.addScriptToEvaluateOnNewDocument`), puis ouvrir localhost:8501.
`await geonafProbe.ready()` attend les cartes et `geonafProbe.read()` expose
seulement tailles de messages, compteurs, caméra, mémoire et hôtes des ressources.
La sonde ne conserve jamais le contenu des messages WebSocket.

Répéter trois fois : rechargement normal, désactivation/réactivation de la
comparaison, resoumission de la même paire. Comparer les compteurs serveur
avant/après navigation et bascule. Tester aussi les dates identiques, leur
récupération et une perte de contexte WebGL contrôlée. Les valeurs brutes
obtenues figurent dans `benchmarks/streamlit_temporal_2026-10-21.json`.

## Mesures réelles Windows / Chrome 155

| Scénario | Mesure |
|---|---:|
| Première ouverture → cartes interactives, une observation | 8,514 s |
| Analyse initiale, vérification d'empreinte incluse | 3,499 s |
| Préparation initiale des quatre couches | 0,999 s |
| Montage JS initial → stabilité | 0,551 s |
| Rechargement navigateur, médiane de 3 | 4,204 s |
| Réaffichage après ancienne carte, médiane de 3 | 0,773 s |
| Resoumission même paire, stabilité observée, médiane de 3 | 0,451 s |
| Navigation complète de caméra → stabilité | 0,342 s |
| Bascule locale vers delta → stabilité observée | 0,422 s |

La mesure de resoumission inclut une fenêtre de calme WebSocket de 150 ms et
le polling. La première réponse arrive en 11,8 à 13,5 ms ; cette première
réponse n'est pas la fin du rafraîchissement UI. La détection du premier état
stable est échantillonnée toutes les 20 ms. Les durées de carte n'attendent pas
nécessairement l'insertion du dernier indicateur dans le DOM Streamlit ; leur
présence et leurs valeurs ont été vérifiées après achèvement du script.

Les trois rechargements prennent 3,511 / 4,204 / 4,224 s. Ils créent de nouvelles
sessions : une analyse scientifique est exécutée par session, conformément au
stockage actuel. Le cache public reste chaud : une seule préparation sur
l'ensemble de ces scénarios. Trois resoumissions de la même paire et trois
réaffichages n'ajoutent aucune analyse ni préparation ; les accès chauds à la
charge utile prennent environ 8 à 12 ms. La navigation et la bascule ne génèrent
aucun message WebSocket ni appel à ces fonctions. La synchronisation vérifiée
porte sur centre [9.4, 0.52], zoom 12.2, orientation 23° et inclinaison 40°.

### Transfert et mémoire

Première ouverture : 7 408 579 octets reçus dans les messages applicatifs
WebSocket et 5 204 722 octets de ressources HTTP mesurées par Resource Timing.
Les octets WebSocket sont ceux des messages décodés, pas une mesure des octets
compressés sur le réseau. Ces deux catégories ne doivent pas être présentées
comme un total réseau exhaustif. La charge JSON publique conservée par le cache
occupe 6 255 277 octets avec sa clé. Une resoumission inchangée reçoit seulement
8 270 octets applicatifs supplémentaires ; le même rendu MapLibre est conservé.
Chaque rechargement reçoit environ 7,40 Mo applicatifs, avec 396 623 octets de
ressources HTTP mesurées (cache navigateur actif).

Le cache conserve une entrée pour cette paire, sous ses plafonds 4 / 64 Mio.
La RSS serveur observée est de 285,14 Mo après le premier résultat, 432,28 Mo
après les rechargements/resoumissions, puis 378,02 Mo à une mesure ultérieure.
Ces nombres incluent Streamlit, GDAL, les tableaux et les sessions ; ils ne
mesurent pas seulement le cache. Les sessions déconnectées sont conservées
120 secondes par la configuration existante. Aucun changement de cette
politique ou des caches scientifiques n'est introduit ici.

Le tas JS observé varie notamment de 43 à 96 Mo lors des rechargements et de
72 à 83 Mo lors des réaffichages, selon le passage du GC. Aucun total précis
de mémoire native ou GPU n'est mesuré. Les ressources WebGL sont au nombre de
3 en bureau et 1 en mobile ; les anciens rendus sont démontés avec compteurs
création/retrait équilibrés. La perte de contexte injectée retire les 3 cartes,
laisse 0 contexte actif et affiche Matplotlib. Réactiver restaure 3 contextes.
Ces contrôles ne constituent pas une preuve d'absence de fuite à long terme.

## Validation fonctionnelle et scientifique

Les trois cartes et leur bascule ont été visualisées dans l'application réelle,
pas dans la page de démonstration. Une capture a été affichée dans la conversation.
La synchronisation reste stable, sans boucle observée. Le mode mobile 390 × 844
conserve une seule carte et la caméra lors du passage à la vue changement.
Les 4 indicateurs et les 2 tableaux restent présents après le rendu.

Valeurs Akanda affichées : couverture comparable 57,40 %, surface comparable
8 271,51 ha, delta moyen -0,0328, 827 865 pixels comparables. Les surfaces de
baisse / sous le seuil / hausse sont 1 138,80 / 6 783,95 / 348,76 ha, avec le
seuil existant 0,10. Les calculs ne sont ni dupliqués ni arrondis ; seul le
formatage d'affichage est repris. Empreinte strictement identique :

```text
b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41
```

Aucune requête vers un hôte externe ni erreur/avertissement navigateur nouveau
n'a été observé dans le parcours normal. Les ressources applicatives viennent
exclusivement de localhost ; le moteur et les images sont embarqués. Après
chargement, l'émulation Offline conserve navigation, synchronisation et bascule,
avec 0 message envoyé à Python. Ouvrir/recharger Streamlit nécessite toujours
que localhost soit joignable : l'émulation Offline totale bloque aussi localhost.
Le démarrage sans Internet mais avec serveur local repose sur les ressources
locales vérifiées ; aucune déconnexion physique du réseau n'a été réalisée.

## Limites

Validation limitée à Chrome/Windows et à cette paire réelle Akanda. Le cas de
changement vers une autre acquisition valide est synthétique. Pas de mesure
FPS, de mémoire GPU précise, de test multiutilisateur ou de grand AOI. Le cache
disque système n'a pas été purgé. La première ouverture n'a qu'une observation ;
les mesures répétées portent sur les scénarios chauds indiqués. Le rechargement
continue à recalculer la science pour la nouvelle session : une éventuelle
persistance scientifique serait une décision distincte de cette intégration.

Après les mesures, le serveur instrumenté a été arrêté et remplacé par le
lancement normal indiqué en tête de document. Le contrôle final sur
localhost:8501 confirme 3 cartes prêtes, 4 indicateurs, 2 tableaux et aucun
hôte externe ou message console d'erreur/avertissement. Ce serveur normal
reste disponible pour la validation utilisateur.
