# Problèmes connus

## `tests/test_pair_selection_cache.py` : trois tests ignorés à tort (constaté le 28/10/2026)

**Statut :** ouvert, correction indépendante à faire. Le fichier de test n'est pas versionné
(travail local) ; il n'a pas été modifié.

**Symptôme.** Les trois cas paramétrés sont ignorés avec le motif « Fichier SCL local
indisponible : 20250115 ».

**Cause.** `_find_scl(date)` cherche `*{date}*SCL*.jp2` récursivement sous `data/raw/sentinel2`
et exige exactement une correspondance. Le fichier du 15/01/2025 existe en deux exemplaires :

| Chemin | SHA-256 (préfixe) |
|---|---|
| `data/raw/sentinel2/S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528/T32NNF_20250115T093239_SCL_20m.jp2` | `00097c05b76497f3` |
| `data/raw/sentinel2/T32NNF_20250115T093239_SCL_20m.jp2` (copie isolée à la racine) | `00097c05b76497f3` |

Deux correspondances ⇒ saut du test, avec un motif inexact : le fichier n'est pas absent, il est
ambigu. Le saut masque donc un test exécutable.

**Vérification.** Une copie temporaire du test, écartant la copie à la racine, fait passer les
trois cas (3 réussis). Aucune donnée n'a été supprimée ni déplacée.

**Correction attendue (non réalisée).**

1. Accepter plusieurs fichiers SCL correspondants lorsque leurs SHA-256 sont identiques.
2. Sélectionner une source déterministe (par exemple le fichier situé dans le dossier du produit
   SAFE, sinon le premier chemin dans l'ordre lexicographique).
3. Échouer explicitement — ne pas ignorer — si des doublons ont des SHA-256 différents, en listant
   les chemins et empreintes.
4. Réserver le saut au cas d'absence réelle, avec un motif exact.
5. Ne supprimer aucun fichier Sentinel-2 dans le cadre de cette correction.
