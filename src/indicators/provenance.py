"""Dates, sources and limits attached to each indicator, taken from the layer descriptors."""

from __future__ import annotations

from datetime import datetime, timezone

from src.indicators.model import IndicatorDates, IndicatorSource


def parse_utc(text):
    return None if not text else datetime.fromisoformat(str(text)).astimezone(timezone.utc)


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
OSM_LIMITS = (
    "Base collaborative : complétude inconnue ; absence d'objet ≠ absence sur le terrain.",
    "Un objet présent dans la base peut avoir été cartographié longtemps après sa construction.",
)
