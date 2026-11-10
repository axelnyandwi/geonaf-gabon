"""Dates, sources and limits attached to each indicator, taken from the layer descriptors."""

from __future__ import annotations

from datetime import datetime, timezone

from src.indicators.model import IndicatorDates, IndicatorSource


def parse_utc(text):
    # Python 3.10 does not read the "Z" suffix written by the acquisition manifests.
    return None if not text else datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def indicator_source(descriptor, nature):
    return IndicatorSource(descriptor.source_id, descriptor.product, descriptor.version, nature,
                           descriptor.license.attribution, descriptor.fingerprint)


def landcover_dates(descriptor):
    t = descriptor.temporal
    return IndicatorDates(
        t.start_utc, t.end_utc, f"Images satellitaires de {t.start_utc:%Y} classées par le producteur (carte annuelle).",
        f"{descriptor.product} ({descriptor.version})", None,
        parse_utc(descriptor.provenance.get("retrieved_at_utc")))


def osm_dates(descriptor):
    snapshot = descriptor.temporal.end_utc
    return IndicatorDates(
        None, None, "Date de terrain des objets inconnue : seule la date de la base est connue.",
        f"Base OpenStreetMap au {snapshot:%d/%m/%Y %H:%M} UTC", snapshot,
        parse_utc(descriptor.provenance.get("retrieved_at_utc")))


LANDCOVER_LIMITS = (
    "Carte issue d'un modèle de classification ; exactitude déclarée par le producteur, non vérifiée localement.",
    "Résolution 10 m : les petits objets (jardins, alignements d'arbres) sont absorbés dans la classe dominante.",
    "Nomenclature mondiale non spécifique aux écosystèmes côtiers d'Afrique centrale.",
    "Décrit l'année de la carte, pas l'état actuel du territoire.",
)
SENTINEL2_SOURCE_ID = "copernicus-sentinel-2"
SENTINEL2_NATURE = "Observation satellitaire (indice spectral seuillé)"
# Wording taken from the pack v3 release note; not re-checked against the Copernicus legal notice (PDF).
SENTINEL2_ATTRIBUTION = "Contains modified Copernicus Sentinel data {year}"


def sentinel2_dates(acquired_at_utc, product_ids, ingested_at_utc):
    """One acquisition instant; the reference is the product itself; ingestion from the verified manifest."""
    return IndicatorDates(
        acquired_at_utc, acquired_at_utc, "Instant d'acquisition Sentinel-2 (UTC), pas une période.",
        "Sentinel-2 L2A " + " + ".join(product_ids), None, parse_utc(ingested_at_utc))


VEGETATION_S2_LIMITS = (
    "Indice spectral seuillé (NDVI ≥ seuil) : ce n'est pas une mesure directe de la végétation réelle.",
    "Seuil non calibré pour le climat équatorial ni ces écosystèmes ; résultat très sensible au seuil.",
    "Mangroves : marée haute ou inondation abaissent le NDVI ; le SCL peut les classer en eau ou en zone "
    "sombre, alors exclues de la surface observée.",
    "Zones humides herbacées : les pixels mêlant eau et végétation peuvent passer sous le seuil.",
    "Végétation urbaine : à 10 m, jardins et alignements d'arbres sont mêlés au bâti.",
    "Nuages fins, brumes ou ombres non détectés par le SCL abaissent le NDVI ; la classification SCL "
    "(Sen2Cor) peut elle-même se tromper.",
    "Saison non évaluée : le profil de référence ne contient pas de calendrier phénologique validé.",
    "La surface non observée n'est pas estimée : aucune extrapolation à la zone d'étude.",
    "Non comparable à la surface végétalisée WorldCover 2021 (classification, autre année, autre surface "
    "de référence) ; deux dates ne sont comparables que sur leur surface observée commune.",
)

SENTINEL2_PAIR_NATURE = "Observation satellitaire (comparaison de deux acquisitions, indice spectral seuillé)"


def sentinel2_pair_dates(earlier, later):
    """Two acquisition instants (never a continuous period); latest ingestion of the inputs."""
    ingested = max((e.ingested_at_utc for e in (earlier, later) if e.ingested_at_utc), default=None)
    return IndicatorDates(
        earlier.acquired_at_utc, later.acquired_at_utc,
        "Deux instants d'acquisition Sentinel-2 (UTC) comparés ; rien n'est observé entre les deux.",
        "Sentinel-2 L2A " + " + ".join(earlier.product_ids) + " → " + " + ".join(later.product_ids), None,
        parse_utc(ingested))


TRANSITION_S2_LIMITS = (
    "Transitions de classe spectrale autour d'un seuil NDVI : ni déforestation, ni reboisement, ni changement "
    "d'occupation du sol ne sont établis.",
    "Seuil de classe NDVI non calibré pour ce climat ni ces écosystèmes, distinct du seuil de variation ΔNDVI des "
    "classes de changement historiques ; résultats très sensibles au seuil.",
    "Calcul limité à la surface observée commune aux deux dates : la surface non commune n'est ni estimée ni "
    "extrapolée, et la couverture commune ne garantit pas la représentativité spatiale.",
    "Deux acquisitions isolées : atmosphère, humidité, marée et géométrie de visée ne sont pas neutralisées ; "
    "aucune cause n'est attribuée.",
    "Nuages fins, brumes ou ombres non détectés par le SCL peuvent créer de fausses transitions ; la "
    "classification SCL (Sen2Cor) peut elle-même se tromper.",
    "Non comparable aux indicateurs WorldCover, OpenStreetMap ni à l'estimation Sentinel-2 d'une seule date "
    "(autres sources ou autre surface de référence).",
)

OSM_LIMITS = (
    "Base collaborative : complétude inconnue ; absence d'objet ≠ absence sur le terrain.",
    "Un objet présent dans la base peut avoir été cartographié longtemps après sa construction.",
)
